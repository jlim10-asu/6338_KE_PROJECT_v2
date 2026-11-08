"""1a: the eligible pool. Releases joined to their papers, in SCOPE's fields and years, with every stage-1 field present.

Same rules as academic_papers_enriched_2 section 4b, except the abstract: release title and post time; paper title,
publication date, journal and at least one author (empty strings, "[]", "None" and "null" count as missing). Abstracts are
checked only for the releases that reach the sample (1f), where missing ones are backfilled. Writes table s1_pool
(euid, doi, release_year) and the funnel steps. Re-run it after changing SCOPE's fields or years.

Usage: python run.py 1a
"""
import pandas as pd
from config import SCOPE
from s1_corpus.eureka import connect, norm_doi, print_funnel, set_meta, year_of


def present(col):
    return f"(CASE WHEN {col} IS NULL OR TRIM(CAST({col} AS TEXT)) IN ('', '[]', 'None', 'null') THEN 0 ELSE 1 END)"


REQUIRED = {  # funnel label: column
    "release title": "r.title", "release date": "r.post_time", "paper title": "p.title",
    "paper publication date": "p.publication_date", "journal": "p.journal", "authors": "p.authors_json",
}


def build(conn, scope):
    rel = pd.read_sql(f"SELECT r.euid, r.doi AS raw_doi, r.post_time, {present('r.title')} AS ok_release_title, "
                      f"{present('r.post_time')} AS ok_release_date FROM releases r", conn)
    pap = pd.read_sql("SELECT p.doi, p.primary_field, " + ", ".join(
        f"{present(c)} AS ok_{k.replace(' ', '_')}" for k, c in REQUIRED.items() if c.startswith("p.")) + " FROM papers p", conn)
    funnel = [["releases in the database (2018+, with DOI, from the ETL)", len(rel)]]
    rel["doi"] = rel.raw_doi.map(norm_doi)
    df = rel.merge(pap, on="doi", how="inner")
    funnel.append(["DOI matched in papers", len(df)])
    fields = scope.get("fields") or []
    if not fields:
        raise SystemExit("SCOPE['fields'] is empty: name at least one OpenAlex field (e.g. 'Medicine')")
    df = df[df.primary_field.isin(fields)]
    funnel.append([f"field in {fields}", len(df)])
    for label in REQUIRED:
        df = df[df[f"ok_{label.replace(' ', '_')}"] == 1]
        funnel.append([f"+ has {label}", len(df)])
    df = df.drop_duplicates("euid")
    funnel.append(["unique releases", len(df)])
    df = df.assign(release_year=df.post_time.map(year_of))
    lo, hi = scope.get("year_min"), scope.get("year_max")
    keep = df.release_year.notna()
    if lo is not None:
        keep &= df.release_year >= lo
    if hi is not None:
        keep &= df.release_year <= hi
    df = df[keep]
    funnel.append([f"release year {lo or 'any'}-{hi or 'latest'}", len(df)])
    return df[["euid", "doi", "release_year"]].astype({"release_year": int}), funnel


def main():
    conn = connect()
    pool, funnel = build(conn, SCOPE)
    pool.to_sql("s1_pool", conn, if_exists="replace", index=False)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_s1_pool_doi ON s1_pool(doi)")
    set_meta(conn, "funnel_pool", funnel)
    print_funnel(funnel)


if __name__ == "__main__":
    main()
