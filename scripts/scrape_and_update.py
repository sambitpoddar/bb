"""
Brahmaputra Board — Nightly FAQ Generator
==========================================
Runs via GitHub Actions every night.

Crawl approach taken from working brahmaputra scraper:
  - requests.Session with real browser headers (NOT bot UA)
  - Proper link normalisation (drop fragments, skip anchors/js/mailto)
  - BFS with visited-set dedup
  - Graceful per-error handling (Timeout / ConnectionError / HTTPError)

On top of the crawl we add:
  - Cerebras AI (free) to extract FAQ pairs from each page's text
  - Jaccard + MD5 deduplication against existing data/faqs.json
  - Commit updated faqs.json back to the repo

Requirements (installed by GitHub Actions workflow):
    pip install requests beautifulsoup4 cerebras-cloud-sdk
"""

import os
import json
import re
import time
import hashlib
import logging
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from cerebras.cloud.sdk import Cerebras

# ─── Config ────────────────────────────────────────────────────────────────
BASE_URL        = "https://brahmaputraboard.gov.in"
OUTPUT_FILE     = "data/faqs.json"
MODEL           = "gpt-oss-120b"
MAX_PAGES       = 100
MIN_TEXT_LEN    = 150
DELAY           = 1.5        # seconds between requests — be polite
REQUEST_TIMEOUT = 15
SIMILARITY_THR  = 0.52       # Jaccard threshold for near-duplicate FAQ check
CEREBRAS_KEY    = os.environ["CEREBRAS_API_KEY"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── HTTP Session (browser headers — works on GOI servers) ─────────────────
# These headers mirror what the working scraper used and avoid bot-blocking.
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
})

# ─── URL helpers (taken from working scraper) ───────────────────────────────
def is_same_domain(url: str) -> bool:
    return urlparse(url).netloc in ("", urlparse(BASE_URL).netloc)

def normalize_url(url: str, base: str) -> str:
    """Resolve relative URLs and strip fragments."""
    full = urljoin(base, url)
    parsed = urlparse(full)
    return parsed._replace(fragment="").geturl()

# ─── Page fetch (taken from working scraper) ────────────────────────────────
def get_page(url: str) -> BeautifulSoup | None:
    """Fetch a single URL. Returns BeautifulSoup or None on any error."""
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.exceptions.Timeout:
        log.warning("[TIMEOUT] %s", url)
    except requests.exceptions.ConnectionError:
        log.warning("[CONNECTION ERROR] %s", url)
    except requests.exceptions.HTTPError as e:
        log.warning("[HTTP %s] %s", e.response.status_code, url)
    except Exception as e:
        log.warning("[ERROR] %s — %s", url, e)
    return None

# ─── Link extraction (taken from working scraper) ───────────────────────────
def extract_links(soup: BeautifulSoup, current_url: str) -> list[str]:
    """Return all internal HTTP links found on the page."""
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = normalize_url(href, current_url)
        if is_same_domain(full) and full.startswith("http"):
            links.append(full)
    return links

# ─── Content extraction (taken from working scraper, extended) ──────────────
def extract_text(soup: BeautifulSoup) -> str:
    """
    Strip noise tags, prefer main content containers, return clean text.
    Mirrors the working scraper's extract_content approach.
    """
    for tag in soup(["script", "style", "noscript", "iframe",
                     "header", "footer", "nav"]):
        tag.decompose()

    # Try content containers in priority order (same as working scraper)
    body = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id="content")
        or soup.find(id="main-content")
        or soup.find(class_="content")
        or soup.find(class_="main-content")
        or soup.body
    )

    raw = body.get_text(separator="\n") if body else ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

# ─── BFS Crawler ────────────────────────────────────────────────────────────
def crawl() -> dict[str, str]:
    """
    BFS crawl of brahmaputraboard.gov.in.
    Returns {url: cleaned_text} for pages with enough content.

    Structure mirrors the working scraper's crawl() function exactly —
    Session, deque, visited-set, per-URL error handling, DELAY between requests.
    """
    visited: set[str]    = set()
    queue:   deque[str]  = deque([BASE_URL])
    pages:   dict[str, str] = {}

    SKIP_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx",
                ".jpg", ".jpeg", ".png", ".gif", ".zip", ".rar",
                ".mp4", ".mp3", ".ppt", ".pptx"}

    log.info("Starting crawl of %s (max %d pages)", BASE_URL, MAX_PAGES)

    while queue and len(visited) < MAX_PAGES:
        url = queue.popleft()

        if url in visited:
            continue
        visited.add(url)

        # Skip binary files
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in SKIP_EXT):
            continue

        log.info("[%d/%d] Fetching: %s", len(visited), MAX_PAGES, url)
        soup = get_page(url)

        if soup is None:
            time.sleep(DELAY)
            continue

        text = extract_text(soup)

        if len(text) >= MIN_TEXT_LEN:
            pages[url] = text
            log.info("  → stored %d chars", len(text))

        # Discover new links
        for link in extract_links(soup, url):
            if link not in visited:
                queue.append(link)

        time.sleep(DELAY)

    log.info("Crawl complete: %d pages with content", len(pages))
    return pages

# ─── Cerebras AI FAQ extraction ─────────────────────────────────────────────
cerebras_client = Cerebras(api_key=CEREBRAS_KEY)

EXTRACT_PROMPT = """\
You are an information extraction assistant for the Brahmaputra Board,
a Government of India statutory body under the Ministry of Jal Shakti.

From the webpage text below, extract FAQ pairs genuinely useful to citizens,
contractors, researchers, or government officials.

STRICT RULES:
- Extract ONLY factual information explicitly present in the text.
- Do NOT invent, infer, or hallucinate any detail.
- Each answer must be self-contained and make sense without the question.
- Questions must be natural queries a real user would type.
- Include: contact details, dates, names, project info, procedures, rules.
- Return ONLY valid JSON — an array of objects with keys "question" and "answer".
- If the page has no FAQ-worthy content, return an empty array: []
- Do NOT wrap the JSON in markdown code fences.

EXAMPLE OUTPUT:
[
  {{
    "question": "What is the contact email of the Brahmaputra Board?",
    "answer": "The official email is secy-bbrd[at]gov[dot]in. You can also reach them at bbrd-ghy[at]nic[dot]in."
  }}
]

WEBPAGE TEXT:
\"\"\"
{text}
\"\"\"
"""

def extract_faqs(url: str, text: str) -> list[dict]:
    """Send page text to Cerebras and get back structured FAQ pairs."""
    # Keep first 3500 + last 1500 chars for info-dense pages
    if len(text) > 5500:
        text = text[:3500] + "\n\n[...truncated...]\n\n" + text[-1500:]

    try:
        resp = cerebras_client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": EXTRACT_PROMPT.format(text=text)}],
            max_completion_tokens=1800,
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()

        # Strip accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []

        result = []
        for item in parsed:
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer", "")).strip()
            if len(q) > 8 and len(a) > 12:
                result.append({"question": q, "answer": a, "source": url})

        log.info("  → %d FAQs extracted", len(result))
        return result

    except json.JSONDecodeError as e:
        log.warning("JSON parse error for %s: %s", url, e)
        return []
    except Exception as e:
        log.warning("Cerebras error for %s: %s", url, e)
        return []

# ─── Deduplication ──────────────────────────────────────────────────────────
def tokenize(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))

def jaccard(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def faq_id(q: str) -> str:
    return hashlib.md5(q.strip().lower().encode()).hexdigest()[:12]

def deduplicate(
    existing: list[dict],
    candidates: list[dict]
) -> tuple[list[dict], int, int]:
    """
    Merge new FAQ candidates into existing list.
    Two-pass check: exact MD5 match, then Jaccard near-duplicate.
    Returns (merged, added_count, skipped_count).
    """
    merged   = list(existing)
    added    = 0
    skipped  = 0

    existing_qs  = [e["question"] for e in merged]
    existing_ids = {faq_id(q) for q in existing_qs}

    for cand in candidates:
        cq  = cand["question"]
        cid = faq_id(cq)

        # Pass 1: exact duplicate
        if cid in existing_ids:
            skipped += 1
            continue

        # Pass 2: near-duplicate via Jaccard
        if any(jaccard(cq, eq) >= SIMILARITY_THR for eq in existing_qs):
            skipped += 1
            continue

        cand["id"]       = cid
        cand["added_at"] = datetime.now(timezone.utc).isoformat()
        merged.append(cand)
        existing_qs.append(cq)
        existing_ids.add(cid)
        added += 1

    return merged, added, skipped

# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    log.info("═══ Brahmaputra Board FAQ Updater — %s UTC ═══",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    # Load existing FAQs
    existing: list[dict] = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                existing = data.get("faqs", [])
            log.info("Loaded %d existing FAQs", len(existing))
        except (json.JSONDecodeError, KeyError):
            log.warning("faqs.json malformed — starting fresh")

    # Step 1: Crawl
    pages = crawl()

    if not pages:
        log.warning("No pages scraped — site may be unreachable. Keeping existing FAQs unchanged.")
    
    # Step 2: Extract FAQs via Cerebras
    all_candidates: list[dict] = []
    for i, (url, text) in enumerate(pages.items(), 1):
        log.info("Extracting [%d/%d]: %s", i, len(pages), url)
        faqs = extract_faqs(url, text)
        all_candidates.extend(faqs)
        time.sleep(0.5)  # gentle rate-limit on Cerebras API

    log.info("Total candidates: %d", len(all_candidates))

    # Step 3: Deduplicate and merge
    merged, added, skipped = deduplicate(existing, all_candidates)
    log.info("Added: %d | Skipped (dupes): %d | Total: %d", added, skipped, len(merged))

    # Step 4: Write output
    output = {
        "meta": {
            "last_updated":     datetime.now(timezone.utc).isoformat(),
            "total_faqs":       len(merged),
            "pages_scraped":    len(pages),
            "added_this_run":   added,
            "skipped_this_run": skipped,
            "source_site":      BASE_URL,
            "generator":        f"Cerebras/{MODEL}",
        },
        "faqs": merged,
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info("Written → %s  (%d bytes)", OUTPUT_FILE, os.path.getsize(OUTPUT_FILE))
    log.info("═══ Done ═══")


if __name__ == "__main__":
    main()
