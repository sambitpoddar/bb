"""
Brahmaputra Board — Nightly FAQ Generator
==========================================
Runs via GitHub Actions every night.
1. Scrapes all pages from brahmaputraboard.gov.in
2. Sends raw text to Cerebras AI (free) to extract FAQ pairs
3. Deduplicates against existing faqs.json using keyword similarity
4. Commits updated faqs.json back to the repo

Requirements (installed by GitHub Actions workflow):
    pip install requests beautifulsoup4 cerebras-cloud-sdk
"""

import os
import json
import re
import time
import hashlib
import logging
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from cerebras.cloud.sdk import Cerebras

# ─── Config ────────────────────────────────────────────────────────────────
BASE_URL       = "https://brahmaputraboard.gov.in"
OUTPUT_FILE    = "data/faqs.json"
MODEL          = "llama-3.3-70b"          # Cerebras free tier model
MAX_PAGES      = 60                        # safety cap on pages to crawl
MIN_TEXT_LEN   = 120                       # skip pages with too little text
REQUEST_DELAY  = 1.2                       # seconds between HTTP requests
SIMILARITY_THR = 0.52                      # Jaccard threshold for dedup
CEREBRAS_KEY   = os.environ["CEREBRAS_API_KEY"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── HTTP helpers ───────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BrahmaputraBoardBot/1.0; "
        "+https://github.com/brahmaputra-board/chatbot)"
    )
}

def safe_get(url: str, timeout: int = 18) -> requests.Response | None:
    """GET with retry on transient errors."""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            log.warning("HTTP %s → %s", r.status_code, url)
            return None
        except requests.RequestException as exc:
            log.warning("Attempt %d failed for %s: %s", attempt + 1, url, exc)
            time.sleep(2 ** attempt)
    return None

# ─── Crawler ────────────────────────────────────────────────────────────────
def crawl(base: str, max_pages: int) -> dict[str, str]:
    """
    BFS crawl of the site.
    Returns {url: cleaned_text} for every page successfully scraped.
    Only follows internal links. Skips PDFs, images, anchors, admin pages.
    """
    visited: set[str] = set()
    queue:   list[str] = [base]
    pages:   dict[str, str] = {}

    SKIP_EXT  = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg",
                 ".png", ".gif", ".zip", ".rar", ".mp4", ".mp3"}
    SKIP_FRAG = {"admin", "login", "logout", "wp-", "feed", "sitemap"}

    def is_internal(url: str) -> bool:
        p = urlparse(url)
        return p.netloc == "" or p.netloc.endswith("brahmaputraboard.gov.in")

    def normalize(url: str) -> str:
        u = urljoin(base, url).split("#")[0].split("?")[0].rstrip("/")
        return u

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        url = normalize(url)
        if url in visited:
            continue
        visited.add(url)

        # Skip unwanted
        path = urlparse(url).path.lower()
        if any(path.endswith(e) for e in SKIP_EXT):
            continue
        if any(f in url.lower() for f in SKIP_FRAG):
            continue

        log.info("Crawling [%d/%d] %s", len(pages) + 1, max_pages, url)
        resp = safe_get(url)
        if not resp:
            continue
        time.sleep(REQUEST_DELAY)

        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract clean text
        for tag in soup(["script", "style", "nav", "footer", "header",
                          "noscript", "iframe", "form", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) >= MIN_TEXT_LEN:
            pages[url] = text
            log.info("  → %d chars stored", len(text))

        # Enqueue internal links
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("mailto:") or href.startswith("tel:"):
                continue
            full = normalize(urljoin(url, href))
            if is_internal(full) and full not in visited:
                queue.append(full)

    log.info("Crawl complete: %d pages scraped", len(pages))
    return pages

# ─── Cerebras AI extraction ──────────────────────────────────────────────────
client = Cerebras(api_key=CEREBRAS_KEY)

EXTRACT_PROMPT = """\
You are an information extraction assistant for the Brahmaputra Board,
a Government of India statutory body under the Ministry of Jal Shakti.

From the webpage text below, extract FAQ pairs that would be genuinely useful
to citizens, contractors, researchers, or government officials visiting the site.

Rules:
- Extract ONLY factual, specific information present in the text.
- Do NOT invent, infer, or hallucinate any details.
- Each FAQ must be self-contained (answer should make sense without reading the question).
- Answers should be concise but complete (2-6 sentences or a short list).
- Questions must be natural-language queries a real user would type.
- Include contact details, dates, names, project info, procedures, etc. when present.
- Return ONLY valid JSON — an array of objects with keys "question" and "answer".
- If the page has no useful FAQ-worthy content, return an empty array [].
- Minimum quality bar: the FAQ must answer something a citizen would actually ask.

Example format:
[
  {
    "question": "What is the contact email of the Brahmaputra Board?",
    "answer": "The official email is secy-bbrd[at]gov[dot]in. You can also reach them at bbrd-ghy[at]nic[dot]in."
  }
]

Webpage text:
\"\"\"
{text}
\"\"\"
"""

def extract_faqs_from_text(url: str, text: str) -> list[dict]:
    """Call Cerebras to extract FAQ pairs from a single page's text."""
    # Truncate to avoid token limits (keep first + last for most info density)
    if len(text) > 6000:
        text = text[:3500] + "\n\n[...]\n\n" + text[-2000:]

    prompt = EXTRACT_PROMPT.format(text=text)

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=1800,
            temperature=0.15,
        )
        raw = resp.choices[0].message.content.strip()

        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []

        faqs = []
        for item in parsed:
            if (isinstance(item, dict)
                    and "question" in item
                    and "answer" in item
                    and len(str(item["question"]).strip()) > 8
                    and len(str(item["answer"]).strip()) > 12):
                faqs.append({
                    "question": str(item["question"]).strip(),
                    "answer":   str(item["answer"]).strip(),
                    "source":   url,
                })
        log.info("  → extracted %d FAQs", len(faqs))
        return faqs

    except json.JSONDecodeError as exc:
        log.warning("JSON parse error for %s: %s", url, exc)
        return []
    except Exception as exc:
        log.warning("Cerebras error for %s: %s", url, exc)
        return []

# ─── Deduplication ──────────────────────────────────────────────────────────
def tokenize(text: str) -> set[str]:
    """Lowercase word tokens, stripping punctuation."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))

def jaccard(a: str, b: str) -> float:
    """Jaccard similarity of token sets for two strings."""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)

def faq_id(q: str) -> str:
    """Stable ID from question text (used for exact-duplicate check)."""
    return hashlib.md5(q.strip().lower().encode()).hexdigest()[:12]

def deduplicate(existing: list[dict], candidates: list[dict]) -> tuple[list[dict], int, int]:
    """
    Merge candidates into existing FAQ list.
    Returns (merged_list, added_count, skipped_count).
    """
    merged  = list(existing)
    added   = 0
    skipped = 0

    existing_questions = [e["question"] for e in merged]
    existing_ids       = {faq_id(q) for q in existing_questions}

    for cand in candidates:
        cq = cand["question"]
        cid = faq_id(cq)

        # 1. Exact (MD5) duplicate check
        if cid in existing_ids:
            skipped += 1
            continue

        # 2. Semantic near-duplicate check (Jaccard)
        is_dup = any(
            jaccard(cq, eq) >= SIMILARITY_THR
            for eq in existing_questions
        )
        if is_dup:
            skipped += 1
            continue

        # New — add it
        cand["id"]       = cid
        cand["added_at"] = datetime.now(timezone.utc).isoformat()
        merged.append(cand)
        existing_questions.append(cq)
        existing_ids.add(cid)
        added += 1

    return merged, added, skipped

# ─── Main ───────────────────────────────────────────────────────────────────
def main():
    log.info("═══ Brahmaputra Board FAQ Updater — %s ═══",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    # Load existing FAQs
    existing: list[dict] = []
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                existing = data.get("faqs", [])
                log.info("Loaded %d existing FAQs", len(existing))
            except json.JSONDecodeError:
                log.warning("Existing faqs.json is malformed — starting fresh")

    # Crawl
    pages = crawl(BASE_URL, MAX_PAGES)

    # Extract from each page
    all_candidates: list[dict] = []
    for url, text in pages.items():
        log.info("Extracting FAQs from: %s", url)
        faqs = extract_faqs_from_text(url, text)
        all_candidates.extend(faqs)
        time.sleep(0.4)  # gentle rate-limit on Cerebras API

    log.info("Total candidates extracted: %d", len(all_candidates))

    # Deduplicate and merge
    merged, added, skipped = deduplicate(existing, all_candidates)
    log.info("Added: %d  |  Skipped (duplicates): %d  |  Total FAQs: %d",
             added, skipped, len(merged))

    # Write output
    output = {
        "meta": {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_faqs":   len(merged),
            "pages_scraped": len(pages),
            "added_this_run": added,
            "skipped_this_run": skipped,
            "source_site": BASE_URL,
            "generator": f"Cerebras/{MODEL}",
        },
        "faqs": merged
    }

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    log.info("Written → %s (%d bytes)", OUTPUT_FILE,
             os.path.getsize(OUTPUT_FILE))
    log.info("═══ Done ═══")

if __name__ == "__main__":
    main()
