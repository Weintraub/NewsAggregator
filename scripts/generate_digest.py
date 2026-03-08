#!/usr/bin/env python3
"""
Tech Digest Generator
Scrapes HN, Lobste.rs, TechCrunch → Claude API → digest JSON + email
"""

import os
import json
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
MAX_ARTICLES      = 15   # per source

# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_hackernews() -> list[dict]:
    """Top stories from HN Algolia API."""
    print("  Fetching Hacker News...")
    url = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30"
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
    """Newest stories from Lobste.rs JSON API."""
    print("  Fetching Lobste.rs...")
    r = requests.get("https://lobste.rs/newest.json", timeout=15)
    r.raise_for_status()
    articles = []
    for h in r.json():
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
    """Latest articles from TechCrunch RSS feed."""
    print("  Fetching TechCrunch...")
    r = requests.get("https://techcrunch.com/feed/", timeout=15,
                     headers={"User-Agent": "Mozilla/5.0 (compatible; TechDigestBot/1.0)"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    ns   = {"content": "http://purl.org/rss/1.0/modules/content/"}
    articles = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "").strip()
        link  = item.findtext("link", "").strip()
        desc  = item.findtext("description", "").strip()
        # strip any HTML tags from description
        import re
        desc = re.sub(r"<[^>]+>", "", desc)[:300]
        if title and link:
            articles.append({
                "title":       title,
                "url":         link,
                "description": desc,
                "source":      "TechCrunch",
                "tag":         "TC",
            })
    return articles[:MAX_ARTICLES]


def fetch_all() -> list[dict]:
    articles = []
    sources  = [fetch_hackernews, fetch_lobsters, fetch_techcrunch]
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
        lines.append(f"{i}. [{a['source']}] {a['title']}{desc}{tags}")

    return f"""Here are today's top tech articles:

{chr(10).join(lines)}

Write a structured digest with EXACTLY these four sections. Use plain text bullets (- ), no bold, no extra formatting:

## 🔬 Major Technical Developments
3-5 bullets on significant technical stories: AI/ML, infra, security, open source, dev tooling.

## 💼 Business & Market Signals  
3-5 bullets on funding, acquisitions, strategic pivots, market dynamics with investment implications.

## 📋 Case Studies & Deep Dives
2-3 bullets on postmortems, real-world implementations, or engineering lessons.

## ⚡ Signal vs. Noise
1-2 sentences on what today's news suggests about where tech is heading.

No fluff. Be specific. Name companies, numbers, and technologies where relevant."""


def generate_digest(articles: list[dict]) -> str:
    print("  Calling Claude API...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1200,
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
    """Convert markdown-ish digest to clean HTML email."""
    import re
    lines  = digest_text.split("\n")
    blocks = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            blocks.append(f'<h2 style="font-family:monospace;font-size:14px;color:#334155;'
                          f'text-transform:uppercase;letter-spacing:0.08em;border-bottom:'
                          f'1px solid #e2e8f0;padding-bottom:6px;margin:28px 0 10px">'
                          f'{heading}</h2>')
        elif line.startswith("- "):
            text = line[2:].strip()
            # linkify URLs
            text = re.sub(r'(https?://\S+)', r'<a href="\1" style="color:#3b82f6">\1</a>', text)
            blocks.append(f'<div style="display:flex;gap:10px;margin:7px 0">'
                          f'<span style="color:#3b82f6;flex-shrink:0">›</span>'
                          f'<span style="font-family:Georgia,serif;font-size:14px;'
                          f'color:#334155;line-height:1.65">{text}</span></div>')
        else:
            blocks.append(f'<p style="font-family:Georgia,serif;font-size:13px;'
                          f'color:#64748b;font-style:italic;margin:6px 0">{line}</p>')

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
        {date_str} · {article_count} articles · HN · Lobste.rs · TechCrunch
      </div>
    </div>
    <!-- Body -->
    <div style="padding:24px 32px 32px">
      {body}
    </div>
    <!-- Footer -->
    <div style="background:#f8fafc;border-top:1px solid #e2e8f0;padding:16px 32px;
                font-family:monospace;font-size:11px;color:#94a3b8;text-align:center">
      Generated by Claude · Unsubscribe by removing your email from EMAIL_TO in GitHub Secrets
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
        "subject": f"Tech Digest — {date_str}",
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
