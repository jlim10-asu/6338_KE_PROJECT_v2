"""Pipeline runner: the one place that defines stage order.

  python run.py list                      show stages and groups
  python run.py setup                     schema + vocabularies (once per Neo4j database)
  python run.py pipeline                  units -> extract -> ned -> score -> load -> verify
  python run.py extract ned               any stages, in the order given (names or numbers: python run.py 4a 4b)
  python run.py summaries -- --dry-run    pass arguments to a single stage after "--"

Stages run in this process, in order, and stop at the first failure.
"""
import runpy
import sys
import time

STAGES = {  # name: (module, what it does)
    "corpus-pool": ("s1_corpus.s1a_pool", "eligible pool from the EurekAlert! SQLite file: field, years, complete records"),
    "corpus-topics": ("s1_corpus.s1b_topics", "OpenAlex subfield / topic for every pool DOI (network, resumable)"),
    "corpus-sample": ("s1_corpus.s1c_sample", "restrict to SCOPE subfields, rank in seeded order (-- --list: counts)"),
    "corpus-crawl": ("s1_corpus.s1d_crawl", "press body text in rank order until SAMPLE_SIZE (network, 1 req/s)"),
    "corpus-enrich": ("s1_corpus.s1e_enrich", "Crossref (retraction + abstract) and Europe PMC pmcid, candidates only (network)"),
    "corpus-abstracts": ("s1_corpus.s1f_abstracts", "PMC + PubMed abstracts, judged; Semantic Scholar backfill (network)"),
    "corpus-export": ("s1_corpus.s1g_corpus", "sample -> data/raw/corpus.csv + abstract_sources.csv + funnel"),
    "schema": ("s2_setup.s2a_schema", "Neo4j constraints, indexes, NED full-text index"),
    "doid": ("s2_setup.s2b_doid", "load the Disease Ontology"),
    "hetionet": ("s2_setup.s2c_hetionet", "load Hetionet compounds, diseases, symptoms, side effects"),
    "mesh": ("s2_setup.s2d_mesh", "load MeSH brain-and-mind branches (MESH_BRANCHES); downloads d<year>.bin once"),
    "cogat": ("s2_setup.s2e_cogat", "load Cognitive Atlas concepts and tasks; downloads once"),
    "units": ("s3_ingest.s3a_build_units", "records, extraction units, numbered sentences (no network)"),
    "extract": ("s4_extract.s4a_extract", "exposure -> outcome findings (LLM)"),
    "ned": ("s4_extract.s4b_ned", "type-routed entity linking (LLM)"),
    "score": ("s5_reconcile.s5a_score", "statement -> claim matching and exaggeration labels (LLM)"),
    "load": ("s6_graph.s6a_load", "load nodes and edges into Neo4j"),
    "verify": ("s6_graph.s6b_verify", "load verification queries -> results/load_verification.json"),
    "anchor": ("s7_analysis.s7a_anchor", "broader anchors for unlinked mentions + ANCHORED_TO edges (LLM); never merges"),
    "communities": ("s7_analysis.s7b_communities", "Leiden communities over shared entities + weighted anchors (GDS)"),
    "summaries": ("s7_analysis.s7c_summaries", "one report per community (LLM)"),
    "reset": ("s6_graph.reset", "clear derived tables and graph data; keeps ontology, LLM cache, abstracts"),
}
GROUPS = {
    "corpus": ["corpus-pool", "corpus-topics", "corpus-sample", "corpus-crawl", "corpus-enrich", "corpus-abstracts", "corpus-export"],
    "fill": ["corpus-crawl", "corpus-enrich", "corpus-abstracts"],   # repeat until 1f prints "sample complete"
    "setup": ["schema", "doid", "hetionet", "mesh", "cogat"],
    "pipeline": ["units", "extract", "ned", "score", "load", "verify"],
    "analysis": ["anchor", "communities", "summaries", "verify"],
}
GROUPS["all"] = GROUPS["setup"] + GROUPS["pipeline"][:-1] + GROUPS["analysis"]


def stage_id(name):
    """'s3_ingest.s3a_build_units' -> '3a'; reset has no number."""
    tail = STAGES[name][0].rsplit(".", 1)[1]
    return tail[1:3] if tail[:1] == "s" and tail[1:2].isdigit() else ""


IDS = {stage_id(n): n for n in STAGES if stage_id(n)}


def expand(names):
    out = []
    for n in names:
        n = IDS.get(n.lower().removeprefix("s"), n)
        if n in GROUPS:
            out += GROUPS[n]
        elif n in STAGES:
            out.append(n)
        else:
            raise SystemExit(f"unknown stage or group: {n} (try: python run.py list)")
    return out


def show():
    for name, (module, what) in STAGES.items():
        print(f"  {stage_id(name) or '-':<4} {name:<17} {module:<30} {what}")
    print()
    for g, members in GROUPS.items():
        print(f"       {g:<17} {' -> '.join(members)}")


def main(argv):
    args, extra = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    if not args or args == ["list"]:
        return show()
    stages = expand(args)
    if extra and len(stages) > 1:
        raise SystemExit("arguments after -- can only go to a single stage")
    for name in stages:
        module = STAGES[name][0]
        print(f"\n=== {name} ({module})")
        t = time.time()
        sys.argv = [module] + extra
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        print(f"=== {name} done in {time.time() - t:.1f}s")


if __name__ == "__main__":
    main(sys.argv[1:])
