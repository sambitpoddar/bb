"""
Brahmaputra Board — Nightly FAQ Generator
==========================================
Runs 100% autonomously on GitHub Actions. No local machine. No third-party proxy.

ROOT CAUSE OF PREVIOUS FAILURES:
  brahmaputraboard.gov.in sits behind a WAF (Cloudflare or similar) that uses
  TLS fingerprinting (JA3/JA4) to block non-browser clients. Python's `requests`
  library produces a TLS ClientHello that looks nothing like Chrome — it gets
  blocked at the handshake level before a single HTTP header is even read.
  That's why changing User-Agent or headers did absolutely nothing.

THE FIX:
  `curl_cffi` — a Python library that wraps curl-impersonate.
  It performs the TLS handshake with Chrome's exact cipher suites, extensions,
  and HTTP/2 settings. From the server's perspective, it IS Chrome.
  Single pip install. No compilation. No external service. No API key.
  Pre-built binary for Ubuntu (the GitHub Actions runner OS).

FLOW:
  1. curl_cffi crawls brahmaputraboard.gov.in with Chrome TLS impersonation
  2. BFS link discovery (same logic as the working local scraper you provided)
  3. BeautifulSoup extracts clean text (same content-container priority order)
  4. Cerebras AI (free) structures text → FAQ pairs
  5. Jaccard + MD5 deduplication against existing faqs.json
  6. Updated faqs.json committed back to repo by GitHub Actions

Requirements (pip install — all free, no keys needed except Cerebras):
    curl_cffi beautifulsoup4 cerebras-cloud-sdk
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

from curl_cffi import requests as cf_requests  # Chrome TLS impersonation
from bs4 import BeautifulSoup
from cerebras.cloud.sdk import Cerebras

# ─── Config ────────────────────────────────────────────────────────────────
BASE_URL        = "https://brahmaputraboard.gov.in"
OUTPUT_FILE     = "data/faqs.json"
MODEL           = "llama-3.3-70b"
MAX_PAGES       = 80
MIN_TEXT_LEN    = 150
DELAY           = 1.5        # seconds between page fetches
CEREBRAS_DELAY  = 0.4        # seconds between Cerebras API calls
REQUEST_TIMEOUT = 20
SIMILARITY_THR  = 0.52
CEREBRAS_KEY    = os.environ["CEREBRAS_API_KEY"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ─── curl_cffi session — impersonates Chrome 120 TLS fingerprint ────────────
# impersonate="chrome120" makes the TLS handshake byte-for-byte identical
# to Chrome 120 on Windows. The WAF cannot distinguish this from a real browser.
SESSION = cf_requests.Session(impersonate="chrome120")

SKIP_EXT = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".jpg", ".jpeg", ".png", ".gif", ".zip",
    ".rar", ".mp4", ".mp3", ".ppt", ".pptx",
    ".css", ".js", ".ico", ".xml", ".svg", ".woff", ".woff2"
}

# ─── URL helpers (from your working local scraper) ──────────────────────────
def is_same_domain(url: str) -> bool:
    return urlparse(url).netloc in ("", urlparse(BASE_URL).netloc)

def normalize_url(url: str, base: str) -> str:
    full = urljoin(base, url)
    return urlparse(full)._replace(fragment="").geturl()

# ─── Page fetch via curl_cffi ───────────────────────────────────────────────
def get_page(url: str) -> BeautifulSoup | None:
    """
    Fetch a page using Chrome TLS impersonation.
    Error handling mirrors your working local scraper exactly.
    """
    try:
        resp = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if not resp.text or len(resp.text) < 100:
            return None
        return BeautifulSoup(resp.text, "html.parser")
    except cf_requests.exceptions.Timeout:
        log.warning("[TIMEOUT] %s", url)
    except cf_requests.exceptions.ConnectionError:
        log.warning("[CONNECTION ERROR] %s", url)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        log.warning("[ERROR %s] %s — %s", status, url, type(e).__name__)
    return None

# ─── Link extraction (from your working local scraper) ─────────────────────
def extract_links(soup: BeautifulSoup, current_url: str) -> list[str]:
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full = normalize_url(href, current_url)
        if is_same_domain(full) and full.startswith("http"):
            links.append(full)
    return links

# ─── Content extraction (from your working local scraper) ──────────────────
def extract_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "iframe",
                     "header", "footer", "nav"]):
        tag.decompose()

    # Priority order from your working scraper
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
    return re.sub(r"\n{3,}", "\n\n", text).strip()

# ─── BFS Crawler ────────────────────────────────────────────────────────────
def crawl() -> dict[str, str]:
    """
    BFS crawl using Chrome TLS impersonation.
    Structure identical to your working local scraper's crawl().
    Returns {url: text}.
    """
    visited: set[str]    = set()
    queue:   deque[str]  = deque([BASE_URL])
    pages:   dict[str, str] = {}

    log.info("Starting crawl of %s (max %d pages, Chrome TLS impersonation)",
             BASE_URL, MAX_PAGES)

    while queue and len(visited) < MAX_PAGES:
        url = queue.popleft()

        if url in visited:
            continue
        visited.add(url)

        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in SKIP_EXT):
            continue

        log.info("[%3d/%d] %s", len(visited), MAX_PAGES, url)
        soup = get_page(url)

        if soup is None:
            time.sleep(DELAY)
            continue

        text = extract_text(soup)
        if len(text) >= MIN_TEXT_LEN:
            pages[url] = text
            log.info("  → stored %d chars", len(text))

        for link in extract_links(soup, url):
            if link not in visited:
                queue.append(link)

        time.sleep(DELAY)

    log.info("Crawl complete: %d pages with content", len(pages))
    return pages

# ─── Cerebras AI extraction ─────────────────────────────────────────────────
cerebras_client = Cerebras(api_key=CEREBRAS_KEY)

EXTRACT_PROMPT = """\
You are an information extraction assistant for the Brahmaputra Board,
a Government of India statutory body under the Ministry of Jal Shakti.

From the webpage text below, extract FAQ pairs genuinely useful to citizens,
contractors, researchers, or government officials.

STRICT RULES:
- Extract ONLY factual information explicitly present in the text.
- Do NOT invent, infer, or hallucinate any detail.
- Each answer must be self-contained (makes sense without reading the question).
- Questions must be natural-language queries a real user would type.
- Prioritise: contact info, dates, names, procedures, project details, rules.
- Return ONLY a valid JSON array of objects with keys "question" and "answer".
- If the page has no FAQ-worthy content, return exactly: []
- Do NOT use markdown code fences around the JSON.

EXAMPLE:
[
  {{
    "question": "What is the contact number of the Brahmaputra Board?",
    "answer": "The Brahmaputra Board can be reached at 0361-2300128. Their office is at Basistha, Guwahati - 781029, Assam."
  }}
]

PAGE URL: {url}

WEBPAGE TEXT:
\"\"\"
{text}
\"\"\"
"""

def extract_faqs(url: str, text: str) -> list[dict]:
    if len(text) > 5500:
        text = text[:3500] + "\n\n[...]\n\n" + text[-1500:]

    try:
        resp = cerebras_client.chat.completions.create(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": EXTRACT_PROMPT.format(url=url, text=text)
            }],
            max_completion_tokens=1800,
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()

        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```\s*$",       "", raw, flags=re.MULTILINE)
        raw = raw.strip()

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []

        result = []
        for item in parsed:
            q = str(item.get("question", "")).strip()
            a = str(item.get("answer",   "")).strip()
            if len(q) > 8 and len(a) > 12:
                result.append({"question": q, "answer": a, "source": url})

        log.info("  → %d FAQs extracted", len(result))
        return result

    except json.JSONDecodeError as e:
        log.warning("  JSON parse error: %s", e)
        return []
    except Exception as e:
        log.warning("  Cerebras error: %s", e)
        return []

# ─── Deduplication ─────────────────────────────────────────────────────────
def tokenize(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))

def jaccard(a: str, b: str) -> float:
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def faq_id(q: str) -> str:
    return hashlib.md5(q.strip().lower().encode()).hexdigest()[:12]

def deduplicate(existing: list[dict], candidates: list[dict]) -> tuple[list[dict], int, int]:
    merged   = list(existing)
    added    = 0
    skipped  = 0
    existing_qs  = [e["question"] for e in merged]
    existing_ids = {faq_id(q) for q in existing_qs}

    for cand in candidates:
        cq  = cand["question"]
        cid = faq_id(cq)

        if cid in existing_ids:
            skipped += 1
            continue
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

# ─── Main ──────────────────────────────────────────────────────────────────
def main():
    log.info("═══ Brahmaputra Board FAQ Updater — %s UTC ═══",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))

    # Load existing FAQs
    existing: list[dict] = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f).get("faqs", [])
            log.info("Loaded %d existing FAQs", len(existing))
        except (json.JSONDecodeError, KeyError):
            log.warning("faqs.json malformed — starting fresh")

    # Step 1: Crawl with Chrome TLS impersonation
    pages = crawl()

    if not pages:
        log.warning("0 pages scraped. WAF may have changed. Keeping existing FAQs.")
        _write(existing, 0, 0, 0)
        return

    # Step 2: Extract FAQs via Cerebras
    all_candidates: list[dict] = []
    for i, (url, text) in enumerate(pages.items(), 1):
        log.info("[%d/%d] Extracting FAQs: %s", i, len(pages), url)
        all_candidates.extend(extract_faqs(url, text))
        time.sleep(CEREBRAS_DELAY)

    log.info("Total candidates: %d", len(all_candidates))

    # Step 3: Deduplicate and merge
    merged, added, skipped = deduplicate(existing, all_candidates)
    log.info("Added: %d | Skipped (dupes): %d | Total: %d", added, skipped, len(merged))

    _write(merged, len(pages), added, skipped)
    log.info("═══ Done ═══")


def _write(faqs: list[dict], pages: int, added: int, skipped: int):
    output = {
        "meta": {
            "last_updated":     datetime.now(timezone.utc).isoformat(),
            "total_faqs":       len(faqs),
            "pages_scraped":    pages,
            "added_this_run":   added,
            "skipped_this_run": skipped,
            "source_site":      BASE_URL,
            "scraping_method":  "curl_cffi Chrome TLS impersonation",
            "generator":        f"Cerebras/{MODEL}",
        },
        "faqs": faqs,
    }
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    log.info("Written → %s  (%.1f KB)", OUTPUT_FILE,
             os.path.getsize(OUTPUT_FILE) / 1024)


if __name__ == "__main__":
    main()
