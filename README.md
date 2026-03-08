# Tech Signal · Daily Digest

A self-hosted tech news digest that scrapes Hacker News, Lobste.rs, and TechCrunch every morning, summarises everything with Claude, emails it to you, and publishes a dashboard on GitHub Pages.

---

## How it works

```
GitHub Actions (cron, 7 AM UTC)
  └── scripts/generate_digest.py
        ├── Fetch: HN Algolia API + Lobste.rs JSON + TechCrunch RSS
        ├── Summarise: Claude API → structured digest
        ├── Save: digest-output/YYYY-MM-DD.json + latest.json
        ├── Commit: pushes updated JSONs back to repo
        └── Email: Resend API → HTML digest to recipients

GitHub Pages (dashboard/index.html)
  └── Reads digest-output/latest.json → renders dashboard
```

---

## Setup (15 minutes)

### 1. Fork / clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/tech-digest.git
cd tech-digest
```

### 2. Get your API keys

| Service | Purpose | Free tier |
|---|---|---|
| [Anthropic](https://console.anthropic.com) | Claude digest | Pay-per-use (very cheap) |
| [Resend](https://resend.com) | Email delivery | 3,000 emails/month free |

For Resend you'll also need to verify a sending domain (takes ~5 min with a DNS record).

### 3. Add GitHub Secrets

In your repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `RESEND_API_KEY` | `re_...` |
| `EMAIL_FROM` | `digest@yourdomain.com` |
| `EMAIL_TO` | `you@email.com` (comma-separate for multiple) |

### 4. Enable GitHub Pages

Repo → **Settings → Pages → Source: Deploy from a branch → Branch: `main` → Folder: `/dashboard`**

Your dashboard will be live at:
`https://YOUR_USERNAME.github.io/tech-digest/`

### 5. Run manually to test

Repo → **Actions → Daily Tech Digest → Run workflow**

This will fetch articles, generate the digest, commit the JSON, and send the email. Check the Actions log for any errors.

---

## Customisation

### Change schedule
Edit `.github/workflows/daily-digest.yml`:
```yaml
- cron: "0 7 * * 1-5"   # weekdays at 7 AM UTC
- cron: "0 6 * * *"     # every day at 6 AM UTC
```
Use [crontab.guru](https://crontab.guru) to build your schedule.

### Change Claude model
Edit `scripts/generate_digest.py`:
```python
model="claude-opus-4-5",      # best quality
model="claude-haiku-4-5",    # fastest + cheapest
```

### Add/remove sources
Add a new `fetch_*()` function in `generate_digest.py` and include it in `fetch_all()`.

### Adjust the digest prompt
Edit `build_prompt()` in `generate_digest.py` to change sections, tone, or focus areas.

---

## Cost estimate (monthly)

| Item | Cost |
|---|---|
| GitHub Actions (~22 runs/month) | Free |
| GitHub Pages | Free |
| Claude API (~1,200 tokens/run) | ~$0.10–0.30/month |
| Resend (1 email/day) | Free |
| **Total** | **< $0.30/month** |

---

## File structure

```
tech-digest/
├── .github/
│   └── workflows/
│       └── daily-digest.yml    # cron scheduler
├── dashboard/
│   └── index.html              # GitHub Pages dashboard
├── digest-output/
│   ├── latest.json             # always the most recent digest
│   └── 2025-06-10.json         # dated archive
├── scripts/
│   ├── generate_digest.py      # main script
│   └── requirements.txt
└── README.md
```
