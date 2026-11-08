"""1b: OpenAlex primary topic, subfield, field and domain for every pool DOI (network; resumable).

papers stores only the field, so the subfield is looked up here: 100 DOIs per list call (the OpenAlex maximum), about
350 calls for a 35,000-DOI pool, well inside the free daily budget with OPENALEX_API_KEY. DOIs already in s1_topics are
skipped. A DOI OpenAlex does not return is stored with found = 0.

Usage: python run.py 1b
"""
import requests
from config import OPENALEX_API_KEY
from s1_corpus.eureka import connect, contact, get_json, norm_doi, now

API = "https://api.openalex.org/works"
BATCH = 100
DELAY = 0.2
COLS = ["doi", "found", "topic_id", "topic", "subfield_id", "subfield", "field_id", "field", "domain_id", "domain", "fetched_at"]


def _tail(url):
    """'https://openalex.org/subfields/2705' -> 2705; 'https://openalex.org/T11636' -> 'T11636'."""
    if not url:
        return None
    t = str(url).rstrip("/").rsplit("/", 1)[-1]
    return int(t) if t.isdigit() else t


def parse_work(work):
    pt = work.get("primary_topic") or {}
    row = {"doi": norm_doi(work.get("doi")), "found": 1, "topic_id": _tail(pt.get("id")), "topic": pt.get("display_name")}
    for level in ("subfield", "field", "domain"):
        part = pt.get(level) or {}
        row[f"{level}_id"], row[level] = _tail(part.get("id")), part.get("display_name")
    return row


def fetch(dois, get):
    """Rows for every requested DOI. get(params) -> (json | None, error | None)."""
    rows = []
    for i in range(0, len(dois), BATCH):
        chunk = [d for d in dois[i:i + BATCH] if "|" not in d and "," not in d]   # filter delimiters can't be batched
        if not chunk:
            continue
        params = {"filter": "doi:" + "|".join(chunk), "select": "doi,primary_topic", "per_page": BATCH}
        data, err = get(params)
        if err:
            print(f"  batch {i // BATCH + 1}: {err} (left for the next run)")
            continue
        got = {r["doi"]: r for r in map(parse_work, (data or {}).get("results", []))}
        rows += [got.get(d, {"doi": d, "found": 0}) for d in chunk]
    return rows


def main():
    conn = connect()
    conn.execute("CREATE TABLE IF NOT EXISTS s1_topics (doi TEXT PRIMARY KEY, found INTEGER, topic_id TEXT, topic TEXT, "
                 "subfield_id INTEGER, subfield TEXT, field_id INTEGER, field TEXT, domain_id INTEGER, domain TEXT, fetched_at TEXT)")
    todo = [r[0] for r in conn.execute("SELECT DISTINCT doi FROM s1_pool WHERE doi NOT IN (SELECT doi FROM s1_topics) ORDER BY doi")]
    if not OPENALEX_API_KEY:
        print("warning: OPENALEX_API_KEY is not set; OpenAlex allows only a small daily budget without a key")
    print(f"{len(todo):,} pool DOIs without a topic row")
    session = requests.Session()
    session.headers.update({"User-Agent": f"CS6399-capstone/1.0 (mailto:{contact()})"})
    key = {"api_key": OPENALEX_API_KEY} if OPENALEX_API_KEY else {}
    get = lambda params: get_json(session, API, DELAY, params={**params, **key})
    for i in range(0, len(todo), BATCH * 10):                     # commit every 1,000 DOIs
        rows = fetch(todo[i:i + BATCH * 10], get)
        conn.executemany(f"INSERT OR REPLACE INTO s1_topics VALUES ({', '.join('?' * len(COLS))})",
                         [tuple({**r, "fetched_at": now()}.get(c) for c in COLS) for r in rows])
        conn.commit()
        print(f"  {min(i + BATCH * 10, len(todo)):,}/{len(todo):,}")
    n, found = conn.execute("SELECT COUNT(*), SUM(found) FROM s1_topics WHERE doi IN (SELECT doi FROM s1_pool)").fetchone()
    print(f"pool DOIs with a topic row: {n:,}; found in OpenAlex: {found or 0:,}")


if __name__ == "__main__":
    main()
