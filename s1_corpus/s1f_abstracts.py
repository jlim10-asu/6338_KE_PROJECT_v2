"""1f: abstracts for the sample candidates, fetched once, judged with the units stage's own rules (network; resumable).

For each candidate DOI without a verdict:
  1. Fetch the curated sources into abstract_sources: PMC (efetch db=pmc, by the pmcid 1e found) and PubMed (esearch by
     DOI, then efetch). Crossref's abstract was stored by 1e. The parsers are s3_ingest.text's.
  2. Judge them with s3_ingest.text.abstract_candidates / choose_abstract in ABSTRACT_SOURCE_PRIORITY, the same rules the
     units stage applies: usable = the chosen source has no blocking flag (front matter, truncated start, too short, empty).
  3. Still nothing usable: Semantic Scholar backfill (batch of up to 500; academic_papers_enriched_2 pass 2), stored in
     papers.abstract like the notebook did, then judged again.
  4. Verdict in s1_abstracts. A DOI with no usable abstract is final (usable = 0): 1d skips it and the next rank takes its
     place. A DOI whose fetch failed with a network or server error gets no verdict and is retried on the next run.

Usage: python run.py 1f [-- --rejudge]     --rejudge: recompute every candidate's verdict from stored sources (no fetching)
"""
import argparse
import json
import requests
from config import (ABSTRACT_MAX_WORDS as MAX_WORDS, ABSTRACT_MIN_WORDS as MIN_WORDS, ABSTRACT_SOURCE_PRIORITY as PRIORITY,
                    NCBI_API_KEY, S2_API_KEY, SAMPLE_SIZE)
from s1_corpus.eureka import candidates, connect, contact, ensure_tables, get_json, get_text, now, set_meta
from s3_ingest.text import abstract_candidates, choose_abstract, join_sections, parse_pmc, parse_pubmed, usable_abstract

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
S2_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
S2_BATCH, S2_DELAY = 500, 1.1


def transient(err):
    return bool(err) and err.startswith("error:")


class Fetcher:
    def __init__(self, email):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"CS6399-capstone/1.0 (mailto:{email})"})
        self.ncbi = {"tool": "ke-project", "email": email, **({"api_key": NCBI_API_KEY} if NCBI_API_KEY else {})}
        self.pause = 0.11 if NCBI_API_KEY else 0.34          # NCBI: 10 requests/s with a key, 3 without

    def pmc(self, doi, pmcid):
        uid = str(pmcid).upper().removeprefix("PMC")
        xml, err = get_text(self.session, EUTILS + "efetch.fcgi", self.pause, params={"db": "pmc", "id": uid, "retmode": "xml", **self.ncbi})
        return (parse_pmc(xml), None) if xml else ([], err)

    def pubmed(self, doi, pmcid=None):
        found, err = get_json(self.session, EUTILS + "esearch.fcgi", self.pause,
                              params={"db": "pubmed", "term": f"{doi}[doi]", "retmode": "json", **self.ncbi})
        if err:
            return [], err
        ids = found.get("esearchresult", {}).get("idlist", [])
        if len(ids) != 1:
            return [], f"{len(ids)} PubMed records for DOI"
        xml, err = get_text(self.session, EUTILS + "efetch.fcgi", self.pause, params={"db": "pubmed", "id": ids[0], "retmode": "xml", **self.ncbi})
        return (parse_pubmed(xml), None) if xml else ([], err)

    def s2(self, dois):
        """{doi: abstract text or None}, or (None, error) for the whole batch."""
        data, err = get_json(self.session, S2_BATCH_URL, S2_DELAY, method="POST", params={"fields": "abstract,externalIds"},
                             json_body={"ids": [f"DOI:{d}" for d in dois]}, headers={"x-api-key": S2_API_KEY} if S2_API_KEY else None)
        if err:
            return None, err
        return {d: ((p or {}).get("abstract") or "").strip() or None if p else None for d, p in zip(dois, data)}, None


def stored(conn, doi):
    """{source: sections} for every stored source with text."""
    return {s: json.loads(sec) for s, sec in conn.execute(
        "SELECT source, sections_json FROM abstract_sources WHERE doi = ? AND text IS NOT NULL", (doi,))}


def judge(conn, doi):
    """(usable, chosen source, flags) for one DOI from what is stored, with the units stage's rules."""
    corpus = (conn.execute("SELECT abstract FROM papers WHERE doi = ?", (doi,)).fetchone() or [None])[0]
    cands = abstract_candidates(stored(conn, doi), corpus, PRIORITY, MIN_WORDS, MAX_WORDS)
    pick = choose_abstract(cands, PRIORITY) if cands else None
    return usable_abstract(pick), (pick or {}).get("source"), ";".join((pick or {}).get("flags", []))


def fetch_sources(conn, fetcher, doi):
    """Fetch PMC and PubMed if not stored yet. Returns True if a fetch failed transiently (retry later)."""
    have = {s for (s,) in conn.execute("SELECT source FROM abstract_sources WHERE doi = ?", (doi,))}
    pmcid = (conn.execute("SELECT pmcid FROM papers WHERE doi = ?", (doi,)).fetchone() or [None])[0]
    failed = False
    for source, fn, needs_pmcid in (("pmc", fetcher.pmc, True), ("pubmed", fetcher.pubmed, False)):
        if source in have or (needs_pmcid and not pmcid):
            continue
        try:
            sections, err = fn(doi, pmcid)
        except Exception as e:                       # a parse failure is final for this source
            sections, err = [], f"parse: {type(e).__name__}: {str(e)[:150]}"
        if transient(err):
            failed = True
            continue
        text = join_sections(sections or [])
        conn.execute("INSERT OR REPLACE INTO abstract_sources VALUES (?,?,?,?,?,?,?)",
                     (doi, source, json.dumps(sections or []), text or None, len(text.split()) if text else 0, err, now()))
    return failed


def verdict(conn, doi, usable, source, flags):
    conn.execute("INSERT OR REPLACE INTO s1_abstracts VALUES (?,?,?,?,?)", (doi, int(usable), source, flags, now()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rejudge", action="store_true", help="recompute verdicts from stored sources; fetch nothing")
    args = ap.parse_args()
    conn = connect()
    ensure_tables(conn)
    cands = list(dict.fromkeys(doi for _, _, doi in candidates(conn, SAMPLE_SIZE)))
    if args.rejudge:
        for d in cands:
            verdict(conn, d, *judge(conn, d))
        conn.commit()
        print(f"re-judged {len(cands):,} candidates")
        return
    todo = [d for d in cands if not conn.execute("SELECT 1 FROM s1_abstracts WHERE doi = ?", (d,)).fetchone()]
    print(f"{len(cands):,} candidate DOIs; {len(todo):,} without an abstract verdict")
    fetcher = Fetcher(contact())
    unusable, retry = [], set()
    for n, d in enumerate(todo, 1):
        if fetch_sources(conn, fetcher, d):
            retry.add(d)
        usable, source, flags = judge(conn, d)
        if usable:
            verdict(conn, d, usable, source, flags)
        else:
            unusable.append(d)
        if n % 25 == 0:
            conn.commit()
            print(f"  {n:,}/{len(todo):,}")
    conn.commit()
    s2_todo = [d for d in unusable if conn.execute(
        "SELECT 1 FROM papers WHERE doi = ? AND (s2_status IS NULL OR s2_status LIKE 'error%')", (d,)).fetchone()]
    print(f"no usable abstract yet: {len(unusable):,}; Semantic Scholar backfill: {len(s2_todo):,}")
    for i in range(0, len(s2_todo), S2_BATCH):
        batch = s2_todo[i:i + S2_BATCH]
        found, err = fetcher.s2(batch)
        if err:
            retry.update(batch)
            print(f"  Semantic Scholar batch failed: {err} (retried next run)")
            continue
        for d, text in found.items():
            if text:
                conn.execute("UPDATE papers SET abstract = ?, abstract_source = 'semanticscholar', s2_status = 'found', "
                             "updated_at = ? WHERE doi = ?", (text, now(), d))
            else:
                conn.execute("UPDATE papers SET s2_status = 'not_found', updated_at = ? WHERE doi = ?", (now(), d))
        conn.commit()
    counts = {"usable": 0, "unusable": 0, "retry": 0}
    for d in unusable:
        usable, source, flags = judge(conn, d)
        if not usable and d in retry:
            counts["retry"] += 1                     # a transient failure: no verdict, so the next run tries again
            continue
        verdict(conn, d, usable, source, flags)
        counts["usable" if usable else "unusable"] += 1
    conn.commit()
    # cumulative over the whole ranking, so the funnel also counts releases skipped in earlier rounds
    total = dict(conn.execute("""SELECT a.usable, COUNT(DISTINCT a.doi) FROM s1_abstracts a
                                 WHERE a.doi IN (SELECT doi FROM s1_ranked) GROUP BY a.usable""").fetchall())
    set_meta(conn, "abstracts", {"checked": total.get(1, 0) + total.get(0, 0), "usable": total.get(1, 0),
                                 "unusable": total.get(0, 0), "retry": counts["retry"]})
    print(f"ranked releases judged so far: {total.get(1, 0):,} usable, {total.get(0, 0):,} unusable; "
          f"{counts['retry']:,} left for a retry")
    if len(candidates(conn, SAMPLE_SIZE)) < SAMPLE_SIZE:
        print("places are free (unusable abstracts or crawl gaps): run 1d, 1e and 1f again to fill them from the next ranks")


if __name__ == "__main__":
    main()
