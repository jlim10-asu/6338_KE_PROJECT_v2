"""FR-5 to FR-8 evaluation: GraphRAG vs. vector-only baseline on eval/gold/eval_queries.json.

  routing           graph only: path_type and template_id match the gold (FR-6, FR-7)
  flagged           sentences with a missing or unknown citation (FR-8)
  faithfulness      judge splits the answer into claims; supported / all, against the retrieved rows (RAGAS)
  answer_relevancy  judge writes 3 questions the answer answers; mean cosine to the real question (RAGAS)
  context_recall    gold claims supported by the retrieved rows / all gold claims (RAGAS)
  latency           retrieval and end-to-end p95, uncached calls only (run with --fresh)
Judge = Claude Opus 5.5, a different model from the generator. Also writes a blind A/B sheet for the 1-5 human rubric.

  python eval/eval_qa.py [--fresh] [--only Q01,Q02]
Outputs: results/eval_qa.json, results/eval_qa_rows.csv, results/qa_rubric.csv, results/qa_rubric_key.json
"""
import argparse
import json
import random
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from config import RESULTS
from core.embed import QA_EMBED_MODEL, qa_embed
from core.llm import llm_json, LLMStop
from s8_qa import graphrag
from s8_qa.graphrag import answer, render_rows

GOLD = Path(__file__).resolve().parent / "gold" / "eval_queries.json"
JUDGE_MODEL, JUDGE_EFFORT, JUDGE_VERSION = "claude-opus-5-5", "medium", "j1"
SYSTEMS = ("graph", "baseline")


class Claim(BaseModel):
    claim: str = Field(description="One atomic factual claim made by the answer")
    supported: bool = Field(description="True only if the context states it or it follows directly")


class Faithfulness(BaseModel):
    claims: list[Claim]


class GoldVerdict(BaseModel):
    n: int = Field(description="Number of the gold claim")
    in_context: bool = Field(description="True if the context supports the gold claim")


class Recall(BaseModel):
    verdicts: list[GoldVerdict]


class Relevancy(BaseModel):
    questions: list[str] = Field(description="Exactly 3 different questions this answer would answer")


FAITH = ("Split the answer into atomic factual claims and decide for each whether the context supports it (states it or it "
         "follows directly). Numbers must match exactly. Judge only against the context.")
RECALL = "For each numbered gold claim, decide whether the context supports it. Return one verdict per gold claim."
RELEVANCY = "Write exactly 3 different questions that the answer would be a good answer to, based only on its content."


def judge(schema, system, user, metric):
    try:
        raw, _ = llm_json(schema, system, user, tag=f"judge_{metric}", version=JUDGE_VERSION, effort=JUDGE_EFFORT, model=JUDGE_MODEL)
        return schema.model_validate_json(raw)
    except LLMStop:
        return None


def metrics(q, out):
    ctx = render_rows(out["retrieval"]["results"]) or "(no rows)"
    ans = " ".join(s["text"] for s in out["sentences"])
    gold = "\n".join(f"{i}. {c}" for i, c in enumerate(q["gold_claims"], 1))
    m = {}
    if f := judge(Faithfulness, FAITH, f"<context>\n{ctx}\n</context>\n<answer>{ans}</answer>", "faith"):
        m["faithfulness"] = round(np.mean([c.supported for c in f.claims]), 3) if f.claims else None
    if r := judge(Recall, RECALL, f"<gold_claims>\n{gold}\n</gold_claims>\n<context>\n{ctx}\n</context>", "recall"):
        hits = {v.n for v in r.verdicts if v.in_context}
        m["context_recall"] = round(len(hits & set(range(1, len(q["gold_claims"]) + 1))) / len(q["gold_claims"]), 3)
    if (rel := judge(Relevancy, RELEVANCY, f"<answer>{ans}</answer>", "relevancy")) and rel.questions:
        qv, gv = np.array(qa_embed([q["question"]])[0]), np.array(qa_embed(rel.questions[:3]))
        m["answer_relevancy"] = round(float(np.mean(gv @ qv)), 3)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fresh", action="store_true", help="bypass the LLM cache (true latency; costs API calls)")
    ap.add_argument("--only")
    args = ap.parse_args()
    qs = [q for q in json.load(open(GOLD))["questions"] if not args.only or q["id"] in args.only.split(",")]
    qa_embed(["warm up"])  # load the embedding model outside the timers

    rows, outs = [], {}
    for q in qs:
        for mode in SYSTEMS:  # sequential, so latencies are not distorted
            out = outs[(q["id"], mode)] = answer(q["question"], mode=mode, use_cache=not args.fresh)
            rows.append({"id": q["id"], "system": mode, "gold_path": q["path_type"], "gold_template": q["template_id"],
                         "path_type": out["path_type"], "template_id": out["template_id"],
                         "route_ok": out["template_id"] == q["template_id"] if mode == "graph" else None,
                         "flagged": len(out["flagged"]), "insufficient_context": out["insufficient_context"],
                         "uncached": out["uncached"], **{f"{k}_ms": v for k, v in out["timing_ms"].items()},
                         **metrics(q, out)})
            print(rows[-1])
    df = pd.DataFrame(rows)

    def summary(d):
        lat = d[d.uncached]
        mean = lambda c: round(float(d[c].mean()), 3) if c in d and d[c].notna().any() else None
        return {"n": len(d), "faithfulness": mean("faithfulness"), "answer_relevancy": mean("answer_relevancy"),
                "context_recall": mean("context_recall"), "flagged_sentences": int(d.flagged.sum()),
                "routing_accuracy": mean("route_ok") if d.route_ok.notna().any() else None,
                "retrieval_p95_ms": float(np.percentile(d.retrieval_ms, 95)),
                "end_to_end_p95_ms_uncached": float(np.percentile(lat.end_to_end_ms, 95)) if len(lat) else None}

    S = {s: summary(df[df.system == s].astype({"route_ok": "float"})) for s in SYSTEMS}
    G, B = S["graph"], S["baseline"]
    ge = lambda k: None if G[k] is None or B[k] is None else G[k] >= B[k]
    targets = {"FR-5 faithfulness >= 0.80": None if G["faithfulness"] is None else G["faithfulness"] >= 0.80,
               "FR-5 graph >= baseline: faithfulness": ge("faithfulness"),
               "FR-5 graph >= baseline: answer_relevancy": ge("answer_relevancy"),
               "FR-5 graph >= baseline: context_recall": ge("context_recall"),
               "FR-8 no flagged sentences (graph)": G["flagged_sentences"] == 0,
               "Latency: retrieval p95 < 3000 ms": G["retrieval_p95_ms"] < 3000,
               "Latency: end-to-end p95 < 10000 ms": None if G["end_to_end_p95_ms_uncached"] is None
               else G["end_to_end_p95_ms_uncached"] < 10000}

    # Blind A/B sheet for the human rubric (1-5); the key says which slot is which system
    rng, sheet, key = random.Random(42), [], {}
    for q in qs:
        order = rng.sample(SYSTEMS, 2)
        key[q["id"]] = dict(zip("AB", order))
        sheet += [{"id": q["id"], "slot": s, "question": q["question"], "gold_answer": q["gold_answer"],
                   "answer": outs[(q["id"], m)]["answer"], "score_1to5": "", "notes": ""} for s, m in zip("AB", order)]

    json.dump({"embed_model": QA_EMBED_MODEL, "generator": graphrag.GENERATOR_MODEL, "router": graphrag.ROUTER_MODEL, "judge": JUDGE_MODEL,
               "fresh": args.fresh, "targets": targets, "summary": S}, open(RESULTS / "eval_qa.json", "w"), indent=2, default=str)
    df.to_csv(RESULTS / "eval_qa_rows.csv", index=False)
    pd.DataFrame(sheet).to_csv(RESULTS / "qa_rubric.csv", index=False)
    json.dump(key, open(RESULTS / "qa_rubric_key.json", "w"), indent=2)
    print(json.dumps({"targets": targets, "summary": S}, indent=2, default=str))


if __name__ == "__main__":
    main()
