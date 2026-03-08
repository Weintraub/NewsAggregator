#!/usr/bin/env python3
"""
Tech Digest Generator
Scrapes HN, Lobste.rs, TechCrunch → Claude API → digest JSON + email
"""

import os
import re
import json
import email.utils
import datetime
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import anthropic
import requests
import resend  # pip install resend

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
RESEND_API_KEY    = os.environ["RESEND_API_KEY"]
EMAIL_FROM        = os.environ.get("EMAIL_FROM", "digest@yourdomain.com")
EMAIL_TO          = os.environ["EMAIL_TO"]          # comma-separated list OK
OUTPUT_DIR        = Path(__file__).parent.parent / "digest-output"
MAX_ARTICLES      = 30   # per source (full day of articles)

# ── Date helpers ───────────────────────────────────────────────────────────────

def yesterday_range() -> tuple[datetime.datetime, datetime.datetime]:
    """Return (start_of_yesterday, start_of_today) as naive UTC datetimes."""
    today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return today - datetime.timedelta(days=1), today


def parse_rss_date(date_str: str) -> datetime.datetime | None:
    """Parse an RSS pubDate string to a naive UTC datetime."""
    try:
        t = email.utils.parsedate_to_datetime(date_str)
        return t.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_hackernews() -> list[dict]:
    """Front-page HN stories published yesterday (UTC), via Algolia date filter."""
    print("  Fetching Hacker News...")
    yday, today = yesterday_range()
    url = (
        "https://hn.algolia.com/api/v1/search"
        f"?tags=front_page&hitsPerPage=50"
        f"&numericFilters=created_at_i>{int(yday.timestamp())},created_at_i<{int(today.timestamp())}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    hits = r.json().get("hits", [])
    articles = []
    for h in hits:
        if not h.get("title"):
            continue
        articles.append({
            "title":  h["title"],
            "url":    h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "points": h.get("points", 0),
            "source": "Hacker News",
            "tag":    "HN",
        })
    return articles[:MAX_ARTICLES]


def fetch_lobsters() -> list[dict]:
    """Lobste.rs stories from yesterday (UTC). Newest endpoint returns ~1 page (~25 stories)."""
    print("  Fetching Lobste.rs...")
    yday, today = yesterday_range()
    r = requests.get("https://lobste.rs/newest.json", timeout=15)
    r.raise_for_status()
    articles = []
    for h in r.json():
        created = h.get("created_at", "")
        try:
            dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            dt_utc = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            if not (yday <= dt_utc < today):
                continue
        except Exception:
            pass  # include if date unparseable
        articles.append({
            "title":  h["title"],
            "url":    h.get("url") or h["short_id_url"],
            "points": h.get("score", 0),
            "tags":   h.get("tags", []),
            "source": "Lobste.rs",
            "tag":    "LB",
        })
    return articles[:MAX_ARTICLES]


def fetch_techcrunch() -> list[dict]:
    """TechCrunch articles from yesterday (UTC) via RSS."""
    print("  Fetching TechCrunch...")
    yday, today = yesterday_range()
    r = requests.get("https://techcrunch.com/feed/", timeout=15,
                     headers={"User-Agent": "Mozilla/5.0 (compatible; TechDigestBot/1.0)"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    articles = []
    for item in root.findall(".//item"):
        title   = item.findtext("title", "").strip()
        link    = item.findtext("link", "").strip()
        desc    = item.findtext("description", "").strip()
        pub_raw = item.findtext("pubDate", "").strip()
        if not (title and link):
            continue
        dt = parse_rss_date(pub_raw)
        if dt and not (yday <= dt < today):
            continue
        desc = re.sub(r"<[^>]+>", "", desc)[:300]
        articles.append({
            "title":       title,
            "url":         link,
            "description": desc,
            "source":      "TechCrunch",
            "tag":         "TC",
        })
    return articles[:MAX_ARTICLES]


def fetch_morning_brew() -> list[dict]:
    """Morning Brew articles from yesterday (UTC) via RSS."""
    print("  Fetching Morning Brew...")
    yday, today = yesterday_range()
    r = requests.get(
        "https://www.morningbrew.com/daily/stories.rss",
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; TechDigestBot/1.0)"},
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    articles = []
    for item in root.findall(".//item"):
        title   = item.findtext("title", "").strip()
        link    = item.findtext("link", "").strip()
        desc    = item.findtext("description", "").strip()
        pub_raw = item.findtext("pubDate", "").strip()
        if not (title and link):
            continue
        dt = parse_rss_date(pub_raw)
        if dt and not (yday <= dt < today):
            continue
        desc = re.sub(r"<[^>]+>", "", desc)[:300]
        articles.append({
            "title":       title,
            "url":         link,
            "description": desc,
            "source":      "Morning Brew",
            "tag":         "MB",
        })
    return articles[:MAX_ARTICLES]


def fetch_all() -> list[dict]:
    articles = []
    sources  = [fetch_hackernews, fetch_lobsters, fetch_techcrunch, fetch_morning_brew]
    for fn in sources:
        try:
            items = fn()
            articles.extend(items)
            print(f"    ✓ {len(items)} articles")
        except Exception as e:
            print(f"    ✗ {fn.__name__} failed: {e}")
    return articles


# ── Claude ────────────────────────────────────────────────────────────────────

DIGEST_SYSTEM = """You are a sharp tech analyst preparing a morning briefing for a 
private equity professional at a special situations fund. Be direct, precise, and 
analytical. Think Goldman morning note meets engineering briefing."""

def build_prompt(articles: list[dict]) -> str:
    lines = []
    for i, a in enumerate(articles, 1):
        desc = f"\n   {a['description']}" if a.get("description") else ""
        tags = f"\n   Tags: {', '.join(a['tags'])}" if a.get("tags") else ""
        lines.append(f"{i}. [{a['source']}] {a['title']}\n   URL: {a['url']}{desc}{tags}")

    return f"""Here are today's top tech articles:

{chr(10).join(lines)}

Write a structured digest with EXACTLY these four sections:

## 🔬 Major Technical Developments
## 💼 Business & Market Signals
## 📋 Case Studies & Deep Dives
## ⚡ Signal vs. Noise

For the first three sections, select 2-4 articles each and write each entry in this EXACT format:

### Article Title Here
URL: https://the-exact-url-from-the-list-above.com
One-line subtitle — the single most important takeaway
2-3 sentences of analysis. Include specific numbers, company names, and impact. Why does this matter?

Leave a blank line between articles.

For ## ⚡ Signal vs. Noise, write 2-3 sentences of prose on where tech is heading — no article entries.

Rules: only use URLs from the article list above. No bold, no extra markdown. Be direct, specific, analytical."""


def generate_digest(articles: list[dict]) -> str:
    print("  Calling Claude API...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2500,
        system=DIGEST_SYSTEM,
        messages=[{"role": "user", "content": build_prompt(articles)}],
    )
    return msg.content[0].text


# ── Output ────────────────────────────────────────────────────────────────────

def save_output(articles: list[dict], digest_text: str) -> dict:
    today  = datetime.date.today().isoformat()
    output = {
        "date":     today,
        "articles": articles,
        "digest":   digest_text,
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Save dated file + overwrite latest.json (used by dashboard)
    (OUTPUT_DIR / f"{today}.json").write_text(json.dumps(output, indent=2))
    (OUTPUT_DIR / "latest.json").write_text(json.dumps(output, indent=2))
    print(f"  ✓ Saved digest-output/{today}.json + latest.json")
    return output


# ── Email ─────────────────────────────────────────────────────────────────────

def digest_to_html(digest_text: str, date_str: str, article_count: int) -> str:
    """Convert digest text to clean HTML email with per-article cards."""
    lines = digest_text.split("\n")
    blocks = []

    # Article card state
    art_title    = None
    art_url      = None
    art_subtitle = None
    art_body     = []
    art_state    = None  # None | 'url' | 'subtitle' | 'body'

    def flush_article():
        nonlocal art_title, art_url, art_subtitle, art_body, art_state
        if not art_title:
            return
        if art_url:
            title_html = (
                f'<a href="{art_url}" style="color:#1e293b;text-decoration:none;'
                f'font-family:Georgia,serif;font-size:15px;font-weight:700;line-height:1.4">'
                f'{art_title}</a>'
            )
            read_more = (
                f'&nbsp;<a href="{art_url}" style="color:#3b82f6;font-family:monospace;'
                f'font-size:11px;text-decoration:none;white-space:nowrap">Read more →</a>'
            )
        else:
            title_html = (
                f'<span style="color:#1e293b;font-family:Georgia,serif;'
                f'font-size:15px;font-weight:700;line-height:1.4">{art_title}</span>'
            )
            read_more = ''

        subtitle_html = ''
        if art_subtitle:
            subtitle_html = (
                f'<div style="font-family:Georgia,serif;font-size:13px;color:#3b82f6;'
                f'font-style:italic;margin:4px 0 7px">{art_subtitle}</div>'
            )

        body_html = ''
        if art_body:
            body_html = (
                f'<p style="font-family:Georgia,serif;font-size:14px;color:#334155;'
                f'line-height:1.65;margin:0">{" ".join(art_body)}{read_more}</p>'
            )

        blocks.append(
            f'<div style="margin:14px 0 20px;padding-left:14px;border-left:3px solid #3b82f6">'
            f'<div style="margin-bottom:2px">{title_html}</div>'
            f'{subtitle_html}'
            f'{body_html}'
            f'</div>'
        )
        art_title = None
        art_url = None
        art_subtitle = None
        art_body.clear()
        art_state = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if line.startswith('## '):
            flush_article()
            heading = line[3:].strip()
            blocks.append(
                f'<h2 style="font-family:monospace;font-size:13px;color:#334155;'
                f'text-transform:uppercase;letter-spacing:0.08em;border-bottom:'
                f'1px solid #e2e8f0;padding-bottom:6px;margin:28px 0 12px">'
                f'{heading}</h2>'
            )
            art_state = None

        elif line.startswith('### '):
            flush_article()
            art_title = line[4:].strip()
            art_state = 'url'

        elif art_state == 'url':
            m = re.match(r'^(?:URL:\s*)?(https?://\S+)', line)
            if m:
                art_url = m.group(1)
                art_state = 'subtitle'
            else:
                # No URL line — treat as subtitle
                art_subtitle = re.sub(r'^(?:Subtitle:\s*)', '', line)
                art_state = 'body'

        elif art_state == 'subtitle':
            art_subtitle = re.sub(r'^(?:Subtitle:\s*)', '', line)
            art_state = 'body'

        elif art_state == 'body':
            art_body.append(line)

        else:
            # Signal vs Noise prose or stray text
            text = re.sub(r'(https?://\S+)', r'<a href="\1" style="color:#3b82f6">\1</a>', line)
            blocks.append(
                f'<p style="font-family:Georgia,serif;font-size:14px;color:#334155;'
                f'line-height:1.65;margin:8px 0">{text}</p>'
            )

    flush_article()

    body = "\n".join(blocks)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f8fafc">
  <div style="max-width:640px;margin:32px auto;background:#fff;border:1px solid #e2e8f0;
              border-radius:8px;overflow:hidden">
    <!-- Header -->
    <div style="background:#020617;padding:24px 32px">
      <div style="font-family:monospace;font-size:10px;letter-spacing:0.2em;
                  color:#3b82f6;text-transform:uppercase;margin-bottom:4px">
        ◈ Tech Signal
      </div>
      <div style="font-family:monospace;font-size:20px;font-weight:700;color:#f1f5f9">
        Daily Intelligence Digest
      </div>
      <div style="font-family:monospace;font-size:11px;color:#475569;margin-top:4px">
        {date_str} · {article_count} articles · HN · Lobste.rs · TechCrunch · Morning Brew
      </div>
    </div>
    <!-- Body -->
    <div style="padding:24px 32px 32px">
      {body}
    </div>
    <!-- Footer -->
    <div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:16px 32px;
                font-family:monospace;font-size:11px;color:#94a3b8;text-align:center">
      Generated by Claude
    </div>
  </div>
</body>
</html>"""


def send_email(digest_text: str, date_str: str, article_count: int):
    print("  Sending email via Resend...")
    resend.api_key = RESEND_API_KEY

    recipients = [e.strip() for e in EMAIL_TO.split(",") if e.strip()]
    html       = digest_to_html(digest_text, date_str, article_count)

    resend.Emails.send({
        "from":    EMAIL_FROM,
        "to":      recipients,
        "subject": f"Daniels Tech & Business Digest - {date_str}",
        "html":    html,
    })
    print(f"  ✓ Email sent to {', '.join(recipients)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    today = datetime.date.today().isoformat()
    print(f"\n{'='*50}")
    print(f"  Tech Digest · {today}")
    print(f"{'='*50}\n")

    print("📡 Fetching articles...")
    articles = fetch_all()
    if not articles:
        raise RuntimeError("No articles fetched — all sources failed.")
    print(f"  Total: {len(articles)} articles\n")

    print("🤖 Generating digest with Claude...")
    digest_text = generate_digest(articles)
    print("  ✓ Digest generated\n")

    print("💾 Saving output...")
    output = save_output(articles, digest_text)

    print("📧 Sending email...")
    send_email(digest_text, today, len(articles))

    print(f"\n✅ Done! Digest for {today} complete.\n")
    return output


if __name__ == "__main__":
    main()
