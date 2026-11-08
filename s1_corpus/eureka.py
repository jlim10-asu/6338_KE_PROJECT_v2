"""Shared helpers for the s1 corpus stages: the EurekAlert! SQLite connection, DOI and date parsing, a polite HTTP
helper, the funnel log, and the scope check.

The s1 stages write their own tables (s1_pool, s1_topics, s1_ranked, s1_meta) and add rows to the notebooks' tables
(fulltext, papers columns). They never drop or rewrite the notebooks' rows."""
import json
import re
import sqlite3
import time
from datetime import datetime, timezone

RETRY_STATUS = {429, 500, 502, 503, 504}
_YEAR_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")


def connect():
    from config import EUREKA_DB
    if not EUREKA_DB.exists():
        raise SystemExit(f"EurekAlert! database not found: {EUREKA_DB} (built by the ETL notebooks; see README)")
    conn = sqlite3.connect(EUREKA_DB)
    conn.execute("CREATE TABLE IF NOT EXISTS s1_meta (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def norm_doi(d):
    """'https://doi.org/10.1073/PNAS.123 ' -> '10.1073/pnas.123' (same rule as academic_papers_enriched_2)."""
    if not d:
        return None
    d = str(d).strip().lower()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    return d if d.startswith("10.") else None


def year_of(post_time):
    """Year from post_time: ISO or other date strings, or epoch seconds / ms (same rule as Eurekalert_local_etl_1)."""
    if post_time is None or post_time == "":
        return None
    v = post_time
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit() and len(v) >= 9):
        ts = float(v)
        ts = ts / 1000 if ts > 1e11 else ts
        return datetime.fromtimestamp(ts, tz=timezone.utc).year
    m = _YEAR_RE.search(str(v))
    return int(m.group(0)) if m else None


def now():
    return datetime.now(timezone.utc).isoformat()


def contact():
    from config import CONTACT_EMAIL
    if not CONTACT_EMAIL:
        raise SystemExit("set CONTACT_EMAIL (environment variable): it identifies you to EurekAlert, Crossref and Europe PMC")
    return CONTACT_EMAIL


def get_json(session, url, delay, params=None, tries=4, timeout=60, method="GET", json_body=None, headers=None):
    """Request with fixed pacing and backoff on 429 / 5xx / network errors. Returns (json | None, error | None)."""
    import requests
    for attempt in range(1, tries + 1):
        time.sleep(delay)
        try:
            r = session.request(method, url, params=params, json=json_body, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if attempt == tries:
                return None, f"error:network {e}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return None, "not_found"
        if r.status_code in RETRY_STATUS and attempt < tries:
            ra = r.headers.get("Retry-After")
            time.sleep(float(ra) if ra and ra.isdigit() else 2 ** attempt)
            continue
        if r.status_code != 200:
            return None, f"error:HTTP {r.status_code}"
        try:
            return r.json(), None
        except ValueError:
            return None, "error:bad json"
    return None, "error:retries exhausted"


# ---------- funnel log (each stage records its steps; s1g_corpus writes them all to results/sample_report.json) ----------
def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO s1_meta VALUES (?, ?)", (key, json.dumps(value, default=str)))
    conn.commit()


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM s1_meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def print_funnel(steps):
    for step, n in steps:
        print(f"{n:>9,}  {step}")


# ---------- scope ----------
def scope_key(scope):
    """The parts of SCOPE that decide the ranked sample, in a stable form (detects a changed scope)."""
    return json.dumps({k: scope.get(k) for k in ("fields", "subfields", "topics", "year_min", "year_max")}, sort_keys=True)


def subfield_filter(subfields, known):
    """SCOPE['subfields'] entries (IDs or names, any case) -> set of subfield IDs. known: {id: name}."""
    by_name = {str(n).strip().lower(): i for i, n in known.items()}
    ids, unknown = set(), []
    for s in subfields:
        if isinstance(s, int) or str(s).isdigit():
            ids.add(int(s))
        elif str(s).strip().lower() in by_name:
            ids.add(by_name[str(s).strip().lower()])
        else:
            unknown.append(s)
    if unknown:
        raise SystemExit(f"unknown subfields in SCOPE: {unknown} (see python run.py 1c -- --list)")
    return ids


# ---------- sample state shared by 1d-1g ----------
PARSER_VERSION = 2   # crawler parser (s1d); fulltext rows from an older parser are re-fetched
BODY_OK = "(f.body_text IS NOT NULL AND TRIM(f.body_text) <> '' AND COALESCE(f.parser_version, 0) >= 2)"


def ensure_tables(conn):
    """Columns and tables the s1 stages add next to the notebooks' data (never drops anything)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    for col, typ in [("abstract_source", "TEXT"), ("s2_status", "TEXT"), ("cr_retracted", "INTEGER"), ("cr_update_types", "TEXT"),
                     ("cr_status", "TEXT"), ("pmcid", "TEXT"), ("epmc_is_oa", "INTEGER"), ("epmc_status", "TEXT"),
                     ("updated_at", "TEXT")]:
        if col not in have:
            conn.execute(f"ALTER TABLE papers ADD COLUMN {col} {typ}")
    conn.execute("""CREATE TABLE IF NOT EXISTS abstract_sources (doi TEXT, source TEXT, sections_json TEXT, text TEXT, words INT,
                    error TEXT, fetched_at TEXT, PRIMARY KEY (doi, source))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS s1_abstracts (doi TEXT PRIMARY KEY, usable INTEGER, source TEXT, flags TEXT,
                    checked_at TEXT)""")
    conn.commit()


def candidates(conn, n):
    """The first n ranked releases with body text whose abstract is usable or not yet checked: the releases the sample
    will come from once 1e-1f have run. Returns [(sample_rank, euid, doi)]."""
    return conn.execute(f"""SELECT s.sample_rank, s.euid, s.doi FROM s1_ranked s
        JOIN fulltext f ON f.euid = s.euid LEFT JOIN s1_abstracts a ON a.doi = s.doi
        WHERE {BODY_OK} AND COALESCE(a.usable, 1) = 1 ORDER BY s.sample_rank LIMIT ?""", (n,)).fetchall()


def get_text(session, url, delay, params=None, tries=4, timeout=60):
    """Like get_json, for XML responses. Returns (text | None, error | None)."""
    import requests
    for attempt in range(1, tries + 1):
        time.sleep(delay)
        try:
            r = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            if attempt == tries:
                return None, f"error:network {e}"
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return None, "not_found"
        if r.status_code in RETRY_STATUS and attempt < tries:
            time.sleep(2 ** attempt)
            continue
        if r.status_code != 200:
            return None, f"error:HTTP {r.status_code}"
        return r.text, None
    return None, "error:retries exhausted"
