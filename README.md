# Brahmaputra Board – Self-Updating AI Chatbot

A sticky chatbot widget for **brahmaputraboard.gov.in** that automatically stays up to date — for free, forever.

---

## How It Works

```
brahmaputraboard.gov.in
        │
        │  every night at 1:30 AM IST
        ▼
  GitHub Actions
  (free runner)
        │
        ├─ scrape_and_update.py crawls the site (up to 60 pages)
        │
        ├─ sends each page's text → Cerebras AI (free API)
        │       model: llama-3.3-70b
        │       task: extract FAQ pairs as structured JSON
        │
        ├─ deduplicates against existing data/faqs.json
        │       (Jaccard similarity + MD5 exact-match check)
        │
        └─ commits updated faqs.json back to this repo
                │
                ▼
         data/faqs.json  (GitHub raw URL / jsDelivr CDN)
                │
                ▼
         chatbot widget on BB website
         fetches JSON on load → answers user questions
```

**Zero ongoing cost.** GitHub Actions gives 2,000 free minutes/month (the job uses ~8–12 min/night). Cerebras API is free. GitHub raw URLs are free. The widget is pure HTML/CSS/JS.

---

## Repository Structure

```
bb-chatbot/
├── .github/
│   └── workflows/
│       └── nightly_faq_update.yml   ← GitHub Actions workflow
├── data/
│   └── faqs.json                    ← Auto-updated FAQ database (seed + nightly)
├── scripts/
│   └── scrape_and_update.py         ← Scraper + Cerebras AI extractor
├── widget/
│   └── chatbot.html                 ← The embeddable chatbot widget
└── README.md
```

---

## Setup Instructions

### Step 1 — Fork / Create the GitHub Repository

1. Create a new GitHub repository (e.g. `brahmaputra-board/chatbot`)
2. Upload all files from this project maintaining the folder structure
3. Make the repository **public** (so the raw faqs.json URL is publicly accessible)

### Step 2 — Add the Cerebras API Key

1. Get a free API key from [cerebras.ai](https://cerebras.ai) — no credit card required
2. Go to your GitHub repo → **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Name: `CEREBRAS_API_KEY`
5. Value: your Cerebras API key
6. Click **Add secret**

### Step 3 — Enable GitHub Actions

1. Go to the **Actions** tab in your repository
2. If prompted, click **"I understand my workflows, go ahead and enable them"**
3. The workflow will now run automatically every night at 01:30 AM IST (20:00 UTC)
4. To test immediately: go to Actions → "Nightly FAQ Update" → "Run workflow"

### Step 4 — Update the Widget URL

Open `widget/chatbot.html` and find this line:

```javascript
var FAQS_URL = 'https://raw.githubusercontent.com/brahmaputra-board/chatbot/main/data/faqs.json';
```

Replace `brahmaputra-board/chatbot` with your actual GitHub `org/repo` name.

**Optional — use jsDelivr CDN** (faster, better caching):
```javascript
var FAQS_URL = 'https://cdn.jsdelivr.net/gh/YOUR_ORG/YOUR_REPO@main/data/faqs.json';
```

### Step 5 — Embed the Widget

Copy three blocks from `widget/chatbot.html` into the Brahmaputra Board website:

| Block | Where to paste |
|-------|---------------|
| `<style>` block | Inside `<head>` |
| `<div id="bb-widget">` | Before `</body>` |
| `<script>` block | Just before `</body>` |

That's it. No server setup, no build process, no monthly bills.

---

## Configuration Reference

### Scraper (`scripts/scrape_and_update.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `BASE_URL` | `https://brahmaputraboard.gov.in` | Site to crawl |
| `MAX_PAGES` | `60` | Max pages per run |
| `MIN_TEXT_LEN` | `120` | Skip pages with fewer chars |
| `REQUEST_DELAY` | `1.2` | Seconds between requests (be polite) |
| `SIMILARITY_THR` | `0.52` | Jaccard threshold for dedup (0–1) |
| `MODEL` | `llama-3.3-70b` | Cerebras model |

### Widget (`widget/chatbot.html`)

| Variable | Description |
|----------|-------------|
| `FAQS_URL` | GitHub raw URL to your `faqs.json` |
| `FAQS_URL_CDN` | Fallback jsDelivr CDN URL |
| `MATCH_THR` | Minimum similarity to return a match (default: `0.18`) |

---

## FAQ Data Format (`data/faqs.json`)

```json
{
  "meta": {
    "last_updated": "2026-03-27T00:00:00+00:00",
    "total_faqs": 40,
    "pages_scraped": 60,
    "added_this_run": 5,
    "skipped_this_run": 120,
    "source_site": "https://brahmaputraboard.gov.in",
    "generator": "Cerebras/llama-3.3-70b"
  },
  "faqs": [
    {
      "id": "a1b2c3d4e5f6",
      "question": "What is the Brahmaputra Board?",
      "answer": "The Brahmaputra Board is a statutory authority...",
      "source": "https://brahmaputraboard.gov.in/about",
      "added_at": "2026-03-27T00:00:00+00:00"
    }
  ]
}
```

To **manually add** a FAQ, just append an entry to the `faqs` array and push. The nightly job will skip it on the next run (deduplication by question hash).

---

## Cost Breakdown

| Service | Free tier | Usage |
|---------|-----------|-------|
| GitHub Actions | 2,000 min/month | ~8–12 min/night = ~300 min/month |
| Cerebras API | Free | ~60 API calls/night |
| GitHub raw URLs | Free | Unlimited reads |
| jsDelivr CDN | Free | Unlimited |
| **Total** | **$0/month** | ✅ |

---

## Troubleshooting

**Widget shows "Could not load knowledge base"**
→ Check the `FAQS_URL` is correct and the repo is public. Try the jsDelivr CDN URL.

**GitHub Action fails**
→ Go to Actions tab → click the failed run → read the logs. Most common cause: `CEREBRAS_API_KEY` secret not set.

**Bot gives wrong answers**
→ Lower `MATCH_THR` in the widget (e.g. `0.15`) for more permissive matching, or raise it (e.g. `0.25`) for stricter matching.

**Scraper hitting rate limits**
→ Increase `REQUEST_DELAY` in `scrape_and_update.py` (e.g. `2.0` seconds).
