"""s1_corpus on a synthetic EurekAlert! database (no network): pool rules, topic parsing and batching, scope ranking,
crawl order and reuse, the copied page parser, enrichment and abstract judging, and the exports the pipeline reads."""
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pandas as pd
from s1_corpus import s1a_pool, s1b_topics, s1c_sample, s1d_crawl, s1e_enrich, s1f_abstracts, s1g_corpus
from s1_corpus.eureka import candidates, ensure_tables, norm_doi, scope_key, set_meta, subfield_filter, year_of
from s3_ingest.text import prepare_records

AUTH = json.dumps([{"name": "A Smith", "institutions": [{"name": "Uni", "ror": "r"}]}])
LONG = ("BACKGROUND: " + " ".join(f"Prior work sentence {k} on sleep and memory in older adults." for k in range(9)) +
        " RESULTS: Shorter sleep was associated with faster memory decline (HR 1.3). CONCLUSIONS: Sleep may matter.")


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE s1_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE releases (euid TEXT PRIMARY KEY, title TEXT, doi TEXT, institution TEXT, post_time TEXT)")
    conn.execute("""CREATE TABLE papers (doi TEXT PRIMARY KEY, title TEXT, abstract TEXT, abstract_source TEXT,
        publication_date TEXT, publication_year INTEGER, journal TEXT, issn_l TEXT, cited_by_count INTEGER,
        primary_field TEXT, authors_json TEXT, oa_is_retracted INTEGER, cr_retracted INTEGER, cr_status TEXT)""")
    rel, pap = [], []
    for i in range(30):
        field = "Medicine" if i < 26 else "Neuroscience"                  # 26-29: other field
        rel.append((str(100 + i), f"Release {i}", f"https://doi.org/10.1/P{i}", "Uni", f"2021-0{1 + i % 9}-01"))
        pap.append((f"10.1/p{i}", f"Paper {i}", "" if i == 25 else LONG.replace("sleep", f"sleep {i}"), "openalex", "2021-01-01", 2021,
                    "J", "1234", 3, field, "[]" if i == 24 else AUTH, 0, None, None))
    rel.append(("200", "Old release", "10.1/p0", "Uni", "2016-05-01"))   # same paper, too old
    rel.append(("201", "No paper", "10.9/none", "Uni", "2021-01-01"))     # DOI not in papers
    conn.executemany("INSERT INTO releases VALUES (?,?,?,?,?)", rel)
    conn.executemany("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pap)
    return conn


SCOPE = {"fields": ["Medicine"], "subfields": ["Cardiology and Cardiovascular Medicine", 2712], "topics": [],
         "year_min": 2018, "year_max": None}

# 1 helpers copied from the notebooks
assert norm_doi(" https://doi.org/10.1/AbC ") == "10.1/abc" and norm_doi("doi: 10.2/x") == "10.2/x" and norm_doi("x") is None
assert year_of("2021-03-01") == 2021 and year_of("01 Mar 2019") == 2019 and year_of("1609459200") == 2021 and year_of("") is None
assert subfield_filter(["cardiology and cardiovascular medicine", "2712"], {2705: "Cardiology and Cardiovascular Medicine"}) == {2705, 2712}
print("1 DOI, year and subfield helpers OK")

# 2 pool: field, stage-1 completeness, years, unique releases
conn = make_db()
pool, funnel = s1a_pool.build(conn, SCOPE)
steps = dict(funnel)
assert steps["releases in the database (2018+, with DOI, from the ETL)"] == 32 and steps["DOI matched in papers"] == 31
assert steps["field in ['Medicine']"] == 27 and steps["+ has authors"] == 26 and "+ has abstract" not in steps, funnel
assert len(pool) == 25 and set(pool.euid) == {str(100 + i) for i in range(26)} - {"124"}, sorted(pool.euid)   # "200": 2016
_, both = s1a_pool.build(conn, {**SCOPE, "fields": ["Medicine", "Neuroscience"]})
assert dict(both)["field in ['Medicine', 'Neuroscience']"] == 31          # 4 Neuroscience papers join
pool.to_sql("s1_pool", conn, index=False)
print("2 pool rules OK")

# 3 topics: parsing and batching
def work(i, sub_id, sub):
    return {"doi": f"https://doi.org/10.1/P{i}", "primary_topic": {"id": f"https://openalex.org/T{1000 + sub_id}",
            "display_name": f"Topic {sub_id}", "subfield": {"id": f"https://openalex.org/subfields/{sub_id}", "display_name": sub},
            "field": {"id": "https://openalex.org/fields/27", "display_name": "Medicine"},
            "domain": {"id": "https://openalex.org/domains/4", "display_name": "Health Sciences"}}}
SUBS = {2705: "Cardiology and Cardiovascular Medicine", 2712: "Endocrinology, Diabetes and Metabolism", 2730: "Oncology"}
def sub_of(i):
    return 2705 if i < 10 else 2712 if i < 16 else 2730
calls = []
def fake_get(params):
    calls.append(params)
    dois = params["filter"].removeprefix("doi:").split("|")
    assert len(dois) <= s1b_topics.BATCH
    return {"results": [work(int(d.split("p")[-1]), sub_of(int(d.split("p")[-1])), SUBS[sub_of(int(d.split("p")[-1]))])
                        for d in dois if d != "10.1/p23"]}, None
rows = s1b_topics.fetch(sorted(pool.doi), fake_get)
assert len(rows) == 25 and len(calls) == 1 and {"doi": "10.1/p23", "found": 0} in rows
r0 = next(r for r in rows if r["doi"] == "10.1/p0")
assert r0["subfield_id"] == 2705 and r0["topic_id"] == "T3705" and r0["field_id"] == 27
pd.DataFrame(rows).reindex(columns=s1b_topics.COLS).to_sql("s1_topics", conn, index=False)
print("3 OpenAlex topics OK")

# 4 scope ranking: narrowed scope required, names or IDs, seeded and repeatable
try:
    s1c_sample.rank(s1c_sample.scoped_pool(conn), {**SCOPE, "subfields": []}, 42)
    raise AssertionError("empty subfields accepted")
except SystemExit as e:
    assert "narrowed scope is required" in str(e)
ranked, funnel = s1c_sample.rank(s1c_sample.scoped_pool(conn), SCOPE, 42)
again, _ = s1c_sample.rank(s1c_sample.scoped_pool(conn), SCOPE, 42)
assert len(ranked) == 16 and set(ranked.subfield_id) == {2705, 2712} and list(ranked.euid) == list(again.euid)
assert list(ranked.sample_rank) == list(range(1, 17)) and dict(funnel)["OpenAlex topic known"] == 24
c = s1c_sample.counts(s1c_sample.scoped_pool(conn), ["subfield_id", "subfield"])
assert dict(zip(c.subfield_id, c.releases)) == {2705: 10, 2712: 6, 2730: 8}, c
ranked.to_sql("s1_ranked", conn, index=False)
set_meta(conn, "scope", scope_key(SCOPE))
print("4 scope ranking OK")

# 5 page parser (copied from Eurekalert_press_papers_full_3)
page = """<html><body><h1>Coffee lowers diabetes risk</h1><p class="subtitle">Large cohort study</p>
<p>Peer-Reviewed Publication</p><div class="entry">
<p>Drinking coffee every day was linked to a lower risk of type 2 diabetes in a study of 40,000 adults followed for ten years.</p>
<figure><figcaption>Image: a cup of coffee</figcaption></figure>
<p>Credit: Someone</p>
<p>"Our results suggest coffee may protect against diabetes," said the lead author of the study, adding more work is needed.</p>
<p>The team adjusted for age, smoking, diet and physical activity, and the association held across all subgroups examined.</p>
<h3>Journal</h3><p>Diabetes Care</p><h3>Method of Research</h3><p>Observational study</p>
<h3>Subject of Research</h3><p>People</p></div></body></html>"""
body, sel, subtitle, meta = s1d_crawl.parse_page(page)
assert sel == "div.entry" and body.startswith("Drinking coffee") and "Credit:" not in body and "Diabetes Care" not in body
assert body.count("\n\n") == 2 and subtitle == "Large cohort study", (subtitle, body)
assert meta["method_of_research"] == "Observational study" and meta["subject_of_research"] == "People"
print("5 page parser OK")

# 6 crawl: rank order, reuse of existing rows, 404 never retried, stop at the target
s1d_crawl.ensure_table(conn)
ensure_tables(conn)
first, firstdoi = list(ranked.euid), list(ranked.doi)
conn.execute("INSERT INTO s1_abstracts VALUES (?, 0, NULL, 'too_short', 'x')", (firstdoi[6],))   # rank 7: no usable abstract
up = f"INSERT INTO fulltext ({', '.join(s1d_crawl.COLS)}) VALUES ({', '.join('?' * len(s1d_crawl.COLS))})"
def row(euid, body=None, http=200, pv=2):
    r = dict.fromkeys(s1d_crawl.COLS); r.update(euid=euid, body_text=body, http_status=http, parser_version=pv); return tuple(r[c] for c in s1d_crawl.COLS)
conn.executemany(up, [row(first[0], "Existing body text."), row(first[1], None, 404), row(first[2], "Old parser.", pv=1)])
class FakeCrawler:
    fetched = []
    def fetch_one(self, euid):
        self.fetched.append(euid)
        return row(euid, None if euid == first[3] else f"Body {euid}.")
ok, fetched, examined = s1d_crawl.crawl(conn, FakeCrawler(), target=5, log=lambda *_: None)
assert ok == 5 and FakeCrawler.fetched == [first[2], first[3], first[4], first[5], first[7]], FakeCrawler.fetched
assert examined == 8 and fetched == 5        # rank 1 reused, rank 2 gone, rank 4 no body, rank 7 no usable abstract
print("6 crawl order and reuse OK")

# 7 enrichment parsing and candidates
assert s1e_enrich.crossref_fields({"updated-by": [{"type": "retraction"}, {"type": "correction"}]}) == \
    {"cr_status": "found", "cr_retracted": 1, "cr_update_types": '["correction", "retraction"]'}
secs, err = s1e_enrich.crossref_abstract({"abstract": "<jats:p><jats:bold>Results</jats:bold> Sleep helped.</jats:p>"})
assert err is None and "Sleep helped." in " ".join(t for _, t in secs)
assert s1e_enrich.crossref_abstract({})[1] == "no abstract deposited"
best = s1e_enrich.epmc_best([{"doi": "10.1/P3", "pmcid": None}, {"doi": "10.1/p3", "pmcid": "PMC9", "isOpenAccess": "Y"},
                             {"doi": "10.9/other", "pmcid": "PMC1"}], ["10.1/p3"])
assert best == {"10.1/p3": ("PMC9", True)}
assert [doi for _, _, doi in candidates(conn, 5)] == [firstdoi[i] for i in (0, 2, 4, 5, 7)]
print("7 enrichment parsing and candidates OK")

# 7b abstracts: fetched once per source, judged with the units stage's rules
d_short, d_pmc = firstdoi[0], firstdoi[2]
conn.execute("UPDATE papers SET abstract = 'Too short.', pmcid = 'PMC77' WHERE doi = ?", (d_pmc,))
class FakeFetcher:
    calls = []
    def pmc(self, doi, pmcid):
        self.calls.append(("pmc", doi)); return [("RESULTS", LONG)], None
    def pubmed(self, doi, pmcid=None):
        self.calls.append(("pubmed", doi)); return [], "0 PubMed records for DOI"
assert s1f_abstracts.fetch_sources(conn, FakeFetcher(), d_pmc) is False
assert s1f_abstracts.fetch_sources(conn, FakeFetcher(), d_pmc) is False and len(FakeFetcher.calls) == 2   # cached, not refetched
usable, source, flags = s1f_abstracts.judge(conn, d_pmc)
assert usable and source == "pmc", (usable, source, flags)                  # the short corpus abstract loses to PMC
conn.execute("UPDATE papers SET abstract = 'Too short.' WHERE doi = ?", (d_short,))
assert s1f_abstracts.judge(conn, d_short)[0] is False                       # nothing usable: backfill or skip
class Down(FakeFetcher):
    def pubmed(self, doi, pmcid=None):
        return [], "error:HTTP 503"
assert s1f_abstracts.fetch_sources(conn, Down(), firstdoi[4]) is True       # transient: no row stored, retried later
assert not conn.execute("SELECT 1 FROM abstract_sources WHERE doi = ? AND source = 'pubmed'", (firstdoi[4],)).fetchone()
for _, _, d in candidates(conn, 5):
    s1f_abstracts.verdict(conn, d, *s1f_abstracts.judge(conn, d))
assert [doi for _, _, doi in candidates(conn, 5)] == [firstdoi[i] for i in (2, 4, 5, 7)]   # rank 1 now unusable; ranks 9+ not crawled
print("7b abstract fetch and judging OK")

# 8 export: first N with body text, columns the pipeline reads
df = s1g_corpus.export(conn, 5)
assert list(df.scope_rank) == [3, 5, 6, 8] and list(df.sample_rank) == [1, 2, 3, 4]   # 1 and 7: no usable abstract
assert set(df.oa_subfield) <= {SUBS[2705], SUBS[2712]} and (df.domain == "medicine").all()
assert df.releases_per_paper.max() == 2 or "10.1/p0" not in set(df.doi)
recs, before = prepare_records(df.astype(str).replace({"None": None, "nan": None}))
assert len(recs) == len(df) and set(recs.euid) == set(df.euid), len(recs)
print("8 corpus export OK\nALL TESTS PASSED")
