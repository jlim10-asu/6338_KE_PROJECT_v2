"""1e: Crossref and Europe PMC for the sample candidates only (network; resumable).

Candidates are the first SAMPLE_SIZE ranked releases with body text whose abstract is usable or not yet checked.
  Crossref (academic_papers_enriched_2, pass 3), one call per DOI for two things:
    retraction status: cr_retracted / cr_update_types from `updated-by` (includes Retraction Watch)
    the deposited JATS abstract, parsed with s3_ingest.text.parse_crossref into abstract_sources (source 'crossref')
  Europe PMC (academic_paper_fulltext_4): pmcid and epmc_is_oa, which 1f needs to fetch the PMC abstract.
DOIs that already have a status are skipped, so papers the notebooks checked are not looked up again.

Usage: python run.py 1e
"""
import json
import requests
from config import SAMPLE_SIZE
from s1_corpus.eureka import candidates, connect, contact, ensure_tables, get_json, now
from s3_ingest.text import join_sections, parse_crossref

CROSSREF = "https://api.crossref.org/works/{doi}"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CR_DELAY, EPMC_DELAY, EPMC_BATCH = 0.25, 0.2, 50


def pending(conn, dois, status_col):
    q = f"SELECT doi FROM papers WHERE doi = ? AND ({status_col} IS NULL OR {status_col} LIKE 'error%')"
    return [d for d in dois if conn.execute(q, (d,)).fetchone()]


def crossref_fields(message):
    types = sorted({u.get("type") for u in message.get("updated-by") or [] if u.get("type")})
    return {"cr_status": "found", "cr_retracted": int("retraction" in types), "cr_update_types": json.dumps(types)}


def crossref_abstract(message):
    """(sections, error) from a Crossref message: the deposited JATS abstract, or why there is none."""
    jats = message.get("abstract")
    return (parse_crossref(jats), None) if jats else ([], "no abstract deposited")


def epmc_best(results, batch):
    """A DOI can return several records (MED, PMC, ...): prefer an open-access record with a PMCID."""
    best = {}
    for rec in results or []:
        d = (rec.get("doi") or "").lower()
        if d not in batch:
            continue
        oa = rec.get("isOpenAccess") == "Y" and bool(rec.get("pmcid"))
        if d not in best or (oa and not best[d][1]) or (rec.get("pmcid") and not best[d][0]):
            best[d] = (rec.get("pmcid"), oa)
    return best


def update(conn, doi, fields):
    fields = {**fields, "updated_at": now()}
    conn.execute(f"UPDATE papers SET {', '.join(f'{k} = ?' for k in fields)} WHERE doi = ?", [*fields.values(), doi])


def store_source(conn, doi, source, sections, err):
    text = join_sections(sections or [])
    conn.execute("INSERT OR REPLACE INTO abstract_sources VALUES (?,?,?,?,?,?,?)",
                 (doi, source, json.dumps(sections or []), text or None, len(text.split()) if text else 0, err, now()))


def main():
    conn = connect()
    ensure_tables(conn)
    dois = list(dict.fromkeys(doi for _, _, doi in candidates(conn, SAMPLE_SIZE)))
    print(f"{len(dois):,} candidate DOIs")
    session = requests.Session()
    session.headers.update({"User-Agent": f"CS6399-capstone/1.0 (mailto:{contact()})"})

    todo = pending(conn, dois, "cr_status")
    print(f"Crossref (retraction + abstract): {len(todo):,} pending")
    for n, d in enumerate(todo, 1):
        data, err = get_json(session, CROSSREF.format(doi=requests.utils.quote(d, safe="/")), CR_DELAY,
                             params={"mailto": contact()})
        if err:
            update(conn, d, {"cr_status": err})
            if err == "not_found":
                store_source(conn, d, "crossref", [], "not_found")
        else:
            msg = data.get("message", {})
            update(conn, d, crossref_fields(msg))
            store_source(conn, d, "crossref", *crossref_abstract(msg))
        if n % 25 == 0:
            conn.commit()
    conn.commit()

    todo = pending(conn, dois, "epmc_status")
    print(f"Europe PMC (pmcid): {len(todo):,} pending")
    for i in range(0, len(todo), EPMC_BATCH):
        batch = todo[i:i + EPMC_BATCH]
        data, err = get_json(session, EPMC_SEARCH, EPMC_DELAY, params={
            "query": " OR ".join(f'DOI:"{d}"' for d in batch), "format": "json", "resultType": "lite", "pageSize": 1000})
        if err:
            for d in batch:
                update(conn, d, {"epmc_status": err})
        else:
            best = epmc_best((data or {}).get("resultList", {}).get("result", []), batch)
            for d in batch:
                pmcid, oa = best.get(d, (None, False))
                update(conn, d, {"pmcid": pmcid, "epmc_is_oa": int(oa), "epmc_status": "found" if d in best else "not_found"})
        conn.commit()
    q = ", ".join("?" * len(dois)) or "NULL"
    row = conn.execute(f"""SELECT SUM(cr_status = 'found'), SUM(COALESCE(cr_retracted, 0) OR COALESCE(oa_is_retracted, 0)),
                                  SUM(pmcid IS NOT NULL) FROM papers WHERE doi IN ({q})""", dois).fetchone()
    print(f"candidates: Crossref found {row[0] or 0:,}; retracted {row[1] or 0:,}; with PMCID {row[2] or 0:,}")


if __name__ == "__main__":
    main()
