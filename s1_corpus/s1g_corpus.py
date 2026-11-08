"""1g: export the study sample to data/raw/corpus.csv and its fetched abstracts to data/raw/abstract_sources.csv.

The sample is the first SAMPLE_SIZE ranked releases with press body text (1d) and a usable abstract (1f).

corpus.csv keeps the columns the pipeline reads (see README) plus the scope columns (oa_subfield, oa_topic). sample_rank
is the order inside the corpus (1..N); scope_rank is the release's rank in the whole scoped pool.
results/sample_report.json holds every funnel step from 1a, 1c, 1d and 1f, for the report's dataset table.

Usage: python run.py 1g [-- --force]     refuses to overwrite an existing corpus.csv without --force
"""
import argparse
import json
import pandas as pd
from config import ABSTRACT_SOURCES_CSV, CORPUS_CSV, RESULTS, SAMPLE_SEED, SAMPLE_SIZE, SCOPE
from s1_corpus.eureka import BODY_OK, connect, ensure_tables, get_meta, norm_doi, print_funnel, scope_key

PAPER_COLS = ["title", "abstract", "abstract_source", "publication_date", "publication_year", "authors_json", "journal",
              "issn_l", "cited_by_count", "primary_field", "oa_is_retracted", "cr_retracted", "pmcid", "fulltext_status"]


def export(conn, n):
    have = {r[1] for r in conn.execute("PRAGMA table_info(papers)")}
    pcols = ", ".join(f"p.{c}" if c in have else f"NULL AS {c}" for c in PAPER_COLS)
    df = pd.read_sql(f"""
        SELECT s.sample_rank AS scope_rank, s.euid, s.doi, s.field AS oa_field, s.subfield_id AS oa_subfield_id, s.subfield AS oa_subfield,
               s.topic_id AS oa_topic_id, s.topic AS oa_topic, f.body_text, f.article_title AS release_article_title,
               f.method_of_research, f.subject_of_research, r.title AS release_title, r.post_time, r.institution AS release_institution,
               {pcols}
        FROM s1_ranked s
        JOIN fulltext f ON f.euid = s.euid
        JOIN releases r ON r.euid = s.euid
        JOIN papers p ON p.doi = s.doi
        JOIN s1_abstracts a ON a.doi = s.doi
        WHERE {BODY_OK} AND a.usable = 1
        ORDER BY s.sample_rank LIMIT ?""", conn, params=(n,))
    per_paper = pd.Series([norm_doi(d) for (d,) in conn.execute("SELECT doi FROM releases")]).value_counts()
    df = df.rename(columns={"title": "paper_title", "publication_year": "year"})
    chosen = pd.read_sql("""SELECT a.doi, a.source AS chosen_source, t.text AS chosen_text FROM s1_abstracts a
    JOIN abstract_sources t ON t.doi = a.doi AND t.source = a.source WHERE a.usable = 1""", conn)
    df = df.merge(chosen, on="doi", how="left")
    blank = df.abstract.isna() | (df.abstract.fillna("").str.strip() == "")
    df.loc[blank, "abstract"] = df.loc[blank, "chosen_text"]
    df.loc[blank, "abstract_source"] = df.loc[blank, "chosen_source"]
    df = df.drop(columns=["chosen_source", "chosen_text"])
    df.insert(0, "sample_rank", range(1, len(df) + 1))
    df.insert(4, "domain", df.oa_field.fillna(df.primary_field).str.lower())
    df["releases_per_paper"] = df.doi.map(per_paper).fillna(1).astype(int)
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite an existing corpus.csv")
    args = ap.parse_args()
    conn = connect()
    ensure_tables(conn)
    if get_meta(conn, "scope") != scope_key(SCOPE):
        raise SystemExit("SCOPE changed since 1c ranked the pool: run 1c, 1d, 1e and 1f again first")
    if CORPUS_CSV.exists() and not args.force:
        raise SystemExit(f"{CORPUS_CSV} exists; pass -- --force to overwrite it")
    df = export(conn, SAMPLE_SIZE)
    crawl, abstracts = get_meta(conn, "crawl", {}), get_meta(conn, "abstracts", {})
    funnel = get_meta(conn, "funnel_pool", []) + get_meta(conn, "funnel_sample", [])
    if crawl:
        funnel.append(["crawled in rank order (examined)", crawl.get("examined")])
    if abstracts:
        funnel.append(["abstracts judged (releases with press body text)", abstracts.get("checked")])
        funnel.append(["  no usable abstract after backfill (skipped; next rank took the place)", abstracts.get("unusable")])
    funnel.append([f"study sample: first {SAMPLE_SIZE:,} with body text and a usable abstract", len(df)])
    CORPUS_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CORPUS_CSV, index=False)
    dois = list(dict.fromkeys(df.doi))
    src = pd.read_sql(f"SELECT doi, source, sections_json, text, words, error FROM abstract_sources "
                      f"WHERE doi IN ({', '.join('?' * len(dois)) or 'NULL'})", conn, params=dois)
    src.to_csv(ABSTRACT_SOURCES_CSV, index=False)
    retracted = int((df.oa_is_retracted.fillna(0).astype(float).astype(bool) | df.cr_retracted.fillna(0).astype(float).astype(bool)).sum())
    report = {"scope": SCOPE, "sample_size": SAMPLE_SIZE, "seed": SAMPLE_SEED, "funnel": funnel,
              "sample_by_subfield": df.oa_subfield.value_counts().to_dict(), "sample_by_year": df.year.value_counts().sort_index().to_dict(),
              "retracted_papers": retracted, "with_pmcid": int(df.pmcid.notna().sum()),
              "abstract_chosen_by_source": pd.read_sql(f"SELECT source, COUNT(*) n FROM s1_abstracts WHERE usable = 1 AND doi IN "
                                                       f"({', '.join('?' * len(dois)) or 'NULL'}) GROUP BY source", conn,
                                                       params=dois).set_index("source").n.to_dict(),
              "crossref_checked": int(df.cr_retracted.notna().sum())}
    json.dump(report, open(RESULTS / "sample_report.json", "w"), indent=2, default=str)
    print_funnel(funnel)
    print(f"-> {CORPUS_CSV}  ({len(df):,} releases; {retracted} retracted papers; {report['with_pmcid']:,} with PMCID)")
    print(f"-> {ABSTRACT_SOURCES_CSV}  ({src.text.notna().sum():,} fetched abstracts for {len(dois):,} papers)")
    if len(df) < SAMPLE_SIZE:
        print("short of SAMPLE_SIZE: run 1d, 1e and 1f again (they fill the gaps from the next ranks), or widen SCOPE")
    if report["crossref_checked"] < len(df):
        print("some papers have no Crossref retraction check: run 1e")


if __name__ == "__main__":
    main()
