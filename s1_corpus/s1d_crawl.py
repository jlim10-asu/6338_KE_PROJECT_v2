"""1d: press-release body text, crawled in sample_rank order until SAMPLE_SIZE releases have it (network; resumable).

The parser is Eurekalert_press_papers_full_3's, unchanged (PARSER_VERSION 2), and rows go into the same fulltext table,
so releases you crawled before are reused, not fetched again. Polite by design: robots.txt is checked first, requests are
sequential with CRAWL_DELAY (>= 1 s) between them, the user agent names the project and CONTACT_EMAIL, and pages that
returned 404 / 410 are never retried. About 1 release per second: 500 releases take roughly 10 minutes.

Usage: python run.py 1d [-- --dry-run]     --dry-run: report how many releases still need crawling, fetch nothing
"""
import argparse
import re
import time
from urllib import robotparser

from config import CRAWL_DELAY, SAMPLE_SIZE
from s1_corpus.eureka import PARSER_VERSION, connect, contact, ensure_tables, now, set_meta

BASE_URL = "https://www.eurekalert.org/news-releases/{euid}"
TIMEOUT_SEC, MAX_RETRIES, MIN_CHARS, COMMIT_EVERY = 30, 3, 300, 25
GONE = (404, 410)

FULLTEXT_COLUMNS = {
    "euid": "TEXT PRIMARY KEY", "url": "TEXT", "http_status": "INTEGER", "body_text": "TEXT", "n_chars": "INTEGER",
    "selector_used": "TEXT", "subtitle": "TEXT", "article_title": "TEXT", "method_of_research": "TEXT",
    "subject_of_research": "TEXT", "article_pub_date": "TEXT", "coi_statement": "TEXT", "parser_version": "INTEGER",
    "error": "TEXT", "fetched_at": "TEXT",
}
COLS = list(FULLTEXT_COLUMNS)

# ---------- parser (Eurekalert_press_papers_full_3, section 3) ----------
BODY_SELECTORS = ["div#ahmain-content div.entry", "div.ahcenter", "div.entry", "article div.entry",
                  "#main-content article", "article", "main"]
META_FIELDS = {
    "article title": "article_title", "method of research": "method_of_research",
    "subject of research": "subject_of_research", "article publication date": "article_pub_date",
    "coi statement": "coi_statement",
}
STOP_LABELS = set(META_FIELDS) | {"journal", "doi", "funder", "meeting", "keywords", "tags", "media contact", "disclaimer"}
LABEL_TAGS = ["h3", "h4", "h5", "dt", "strong", "b"]
SKIP_PREFIXES = ("image:", "video:", "audio:", "credit:", "disclaimer:", "aaas and eurekalert!")
STOP_MARKERS = ("contact:", "contacts:", "media contact", "press contact", "for more information, contact")


def norm(s):
    """Collapse whitespace and fix spacing left by inline tags ("Biology ." -> "Biology.")."""
    s = " ".join((s or "").split())
    s = re.sub(r"\s+([.,;:!?)\]’”])", r"\1", s)
    s = re.sub(r"([(\[‘“])\s+", r"\1", s)
    return s


def label_of(tag):
    """Return the lowercase label text if this tag is a metadata label heading, else None."""
    if tag.name not in LABEL_TAGS:
        return None
    t = norm(tag.get_text(" ", strip=True)).rstrip(":").lower()
    return t if t in STOP_LABELS else None


def clean_paragraphs(paras):
    """Drop captions/credits; stop at an in-body contact block."""
    out = []
    for p in paras:
        t = norm(p)
        low = t.lower()
        if not t or low.startswith(SKIP_PREFIXES) or low.endswith("view more"):
            continue
        if low.startswith(STOP_MARKERS):
            break
        out.append(t)
    return out


def extract_body(soup):
    """Walk the container in document order; collect <p> text; stop at the first metadata label."""
    for sel in BODY_SELECTORS:
        node = soup.select_one(sel)
        if node is None:
            continue
        raw = []
        for el in node.find_all(["p"] + LABEL_TAGS):
            if label_of(el):
                break
            if el.name == "p":
                raw.append(el.get_text(" ", strip=True))
        text = "\n\n".join(clean_paragraphs(raw))
        if len(text) >= MIN_CHARS:
            return text, sel
    return None, None


def extract_meta(soup):
    """Value = text between a label heading and the next label heading."""
    out = {v: None for v in META_FIELDS.values()}
    for lab in soup.find_all(LABEL_TAGS):
        key = label_of(lab)
        if key not in META_FIELDS or out[META_FIELDS[key]]:
            continue
        parts = []
        for sib in lab.find_next_siblings():
            if label_of(sib) or sib.find(lambda t: label_of(t) is not None):
                break
            parts.append(sib.get_text(" ", strip=True))
        if not parts and lab.parent is not None:       # value may sit in the parent wrapper
            parts = [lab.parent.get_text(" ", strip=True).replace(lab.get_text(" ", strip=True), "", 1)]
        val = norm(" ".join(parts))
        out[META_FIELDS[key]] = val or None
    return out


def extract_subtitle(soup):
    """Subtitle sits between the <h1> title and the release-type line ("Peer-Reviewed Publication", ...).
    Returns None when nothing appears there, so the institution line is never mistaken for it."""
    h1 = soup.find("h1")
    if not h1:
        return None
    for el in h1.find_all_next(["h2", "h3", "p", "div"], limit=8):
        t = norm(el.get_text(" ", strip=True))
        if not t or el.find(["p", "div"]):
            continue
        if t.lower().startswith(("peer-reviewed", "reports and proceedings", "business announcement",
                                 "meeting announcement", "news release", "announcement")) \
                or "meta_institute" in (el.get("class") or []) \
                or el.find_parent(class_="entry"):
            return None                                  # reached the type line / body: no subtitle
        return t if len(t) <= 300 else None
    return None


def parse_page(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.select("figure, figcaption, script, style, aside, nav, footer"):
        tag.decompose()
    subtitle = extract_subtitle(soup)
    meta = extract_meta(soup)
    body, sel = extract_body(soup)
    return body, sel, subtitle, meta


# ---------- fetch (Eurekalert_press_papers_full_3, section 4) ----------
class Crawler:
    def __init__(self, user_agent, delay):
        import requests
        self.requests = requests
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"})
        self.delay, self.user_agent, self._last = max(delay, 1.0), user_agent, 0.0

    def allowed(self):
        resp = self.session.get("https://www.eurekalert.org/robots.txt", timeout=TIMEOUT_SEC)
        rp = robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(self.user_agent, BASE_URL.format(euid=838589))

    def polite_get(self, url):
        """GET with a global rate limit and exponential backoff on 429/5xx/network errors."""
        for attempt in range(1, MAX_RETRIES + 1):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(url, timeout=TIMEOUT_SEC)
            except self.requests.RequestException as e:
                if attempt == MAX_RETRIES:
                    return None, f"network: {e}"
                time.sleep(self.delay * 2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.isdigit() else self.delay * 2 ** attempt)
                continue
            return r, None
        return None, "retries exhausted"

    def fetch_one(self, euid):
        url = BASE_URL.format(euid=euid)
        rec = dict.fromkeys(COLS)
        rec.update(euid=str(euid), url=url, n_chars=0, parser_version=PARSER_VERSION, fetched_at=now())
        r, err = self.polite_get(url)
        if r is None:
            rec["error"] = err
        elif r.status_code != 200:
            rec.update(http_status=r.status_code, error=f"HTTP {r.status_code}")
        else:
            body, sel, subtitle, meta = parse_page(r.text)
            rec.update(http_status=200, subtitle=subtitle, **meta)
            if body is None:
                rec["error"] = "no body matched selectors"
            else:
                rec.update(body_text=body, n_chars=len(body), selector_used=sel)
        return tuple(rec[c] for c in COLS)


def ensure_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS fulltext (" + ", ".join(f"{c} {t}" for c, t in FULLTEXT_COLUMNS.items()) + ")")
    have = {r[1] for r in conn.execute("PRAGMA table_info(fulltext)")}
    for c, t in FULLTEXT_COLUMNS.items():
        if c not in have:
            conn.execute(f"ALTER TABLE fulltext ADD COLUMN {c} {t.replace(' PRIMARY KEY', '')}")
    conn.commit()


def status(conn):
    """Ranked releases with their state, in rank order: no_abstract (1f found no usable abstract: never crawled),
    ok (usable body), gone (404/410), or todo."""
    rows = conn.execute("""SELECT s.sample_rank, s.euid, f.body_text IS NOT NULL AND TRIM(f.body_text) <> '' AS has_body,
                                  COALESCE(f.parser_version, 0) AS pv, COALESCE(f.http_status, 0) AS http,
                                  COALESCE(a.usable, 1) AS abstract_ok
                           FROM s1_ranked s LEFT JOIN fulltext f ON f.euid = s.euid LEFT JOIN s1_abstracts a ON a.doi = s.doi
                           ORDER BY s.sample_rank""").fetchall()
    return [(rank, euid, "no_abstract" if not ab else "ok" if has and pv >= PARSER_VERSION else "gone" if http in GONE else "todo")
            for rank, euid, has, pv, http, ab in rows]


def crawl(conn, crawler, target, log=print):
    """Walk the ranking; fetch 'todo' releases until `target` have body text. Each release is fetched at most once."""
    upsert = f"INSERT OR REPLACE INTO fulltext ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})"
    ok = fetched = examined = 0
    buf = []
    try:
        for rank, euid, state in status(conn):
            if ok >= target:
                break
            examined += 1
            if state == "todo":
                rec = crawler.fetch_one(euid)
                buf.append(rec)
                fetched += 1
                state = "ok" if rec[COLS.index("body_text")] else "failed"
                if len(buf) >= COMMIT_EVERY:
                    conn.executemany(upsert, buf); conn.commit(); buf.clear()
                    log(f"  rank {rank:,}: {ok + (state == 'ok'):,}/{target:,} with body text, {fetched:,} fetched")
            ok += state == "ok"
    finally:                                            # safe to interrupt; progress is kept
        if buf:
            conn.executemany(upsert, buf); conn.commit()
    return ok, fetched, examined


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="count what still needs crawling; fetch nothing")
    args = ap.parse_args()
    conn = connect()
    ensure_table(conn)
    ensure_tables(conn)
    st = status(conn)
    have = sum(s == "ok" for _, _, s in st)
    print(f"{len(st):,} ranked releases; {have:,} already have body text; target {SAMPLE_SIZE:,}")
    if args.dry_run:
        todo = sum(s == "todo" for _, _, s in st)
        print(f"at least {max(0, SAMPLE_SIZE - have):,} fetches needed; {todo:,} ranked releases not crawled yet")
        return
    crawler = Crawler(f"CS6399-capstone-research-crawler/1.0 (non-commercial academic use; contact: {contact()})", CRAWL_DELAY)
    if not crawler.allowed():
        raise SystemExit("robots.txt disallows news-release pages for this user agent; stopping")
    ok, fetched, examined = crawl(conn, crawler, SAMPLE_SIZE)
    set_meta(conn, "crawl", {"examined": examined, "fetched_this_run": fetched, "with_body": ok, "target": SAMPLE_SIZE})
    print(f"done: {ok:,}/{SAMPLE_SIZE:,} with body text after examining {examined:,} ranked releases ({fetched:,} fetched now)")
    if ok < SAMPLE_SIZE:
        print("the scoped pool ran out before the target: widen SCOPE or lower SAMPLE_SIZE")


if __name__ == "__main__":
    main()
