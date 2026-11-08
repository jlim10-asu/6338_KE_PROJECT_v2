"""Prompts for the analysis stages: broader anchors (s7a) and community reports (s7c; Edge et al. 2024).
Bump ANCHOR_VERSION / COMMUNITY_VERSION after editing that prompt (part of the cache key)."""

COMMUNITY_VERSION = "v6"

COMMUNITY_SYSTEM = """You write a short report on one community of a medical knowledge graph. A community is a group of findings from journal abstracts that share medical entities (exposures or outcomes), found by the Leiden algorithm. The report is shown to users and is searched to answer broad questions ("what do studies say about X?"), so it must state only what the listed claims say.

<input_format>
<papers>: one line per paper, "[P1] title | journal | date | doi", with "RETRACTED" when the paper is retracted.
<claims>: one line per finding, "[C1] P1 | exposure -> outcome | direction | strength | press: ... | wording".
- strength is how strongly the paper's wording asserts the relationship: 0 no relationship, 1 correlational ("associated with"), 2 conditional causal ("may reduce"), 3 direct causal ("reduces").
- press lists how press releases about that paper stated the same finding, as computed by an earlier step: overstated, aligned or understated, with the press and paper strengths. "none" means no press statement was matched to it.
- wording is the paper's own sentence.
<stats>: counts computed by code; use them as given.
</input_format>

<rules>
1. Use only the listed claims and stats. Add no outside knowledge, numbers, or conclusions the claims do not state.
2. Describe each relationship with the paper's strength: strength 0 as "no association found", 1 as "associated with", 2 as "may", 3 as a direct causal verb. Never state a relationship more strongly than its strength.
3. title: at most 12 words naming the shared exposures and outcomes.
4. summary: 3 to 5 sentences covering what the findings study, where papers agree, where they conflict (opposite directions, or a null result beside a positive one), and how press coverage compares, using only the press labels given. Name retracted papers when you use their claims.
5. key_findings: 1 to 5 findings, most widely reported first. Each is one sentence and cites in claim_refs every claim ID that states it. A conflict between papers can be a key finding.
6. Cite claims only by the IDs shown (C1, C2, ...)."""

COMMUNITY_USER = """<papers>
{papers}
</papers>
<claims>
{claims}
</claims>
<stats>
{stats}
</stats>
Write the community report."""

COMMUNITY_REPAIR = """{user}

Your previous report failed validation: {error}
Write the report again, fixing that problem."""


ANCHOR_USER = """<mention>{mention}</mention>
<context>{context}</context>
<candidates>
{candidates}
</candidates>
Give the mention its broader anchor id, or return NIL."""


ANCHOR_VERSION = "v2"    # v2: sibling and wrong-sense rules, parent-must-contain test

ANCHOR_SYSTEM = """You give a biomedical entity mention, taken from a press release or journal abstract, a broader anchor: the most specific term in a reference vocabulary (MeSH, the Cognitive Atlas, the Human Disease Ontology or Hetionet) that the mention is a kind, part, property, activity or measurement of. The mention already failed exact linking, so no candidate names it exactly. Anchors group related findings for browsing; they never merge findings, so a wrong anchor joins unrelated findings, and NIL is better than a loose one.

<candidate_format>
Each line in <candidates> is: id | name | kind | synonyms (the synonyms are also separated by |).
</candidate_format>

<rules>
1. Select the candidate the mention is about, made more general: a brain region the mention measures, the process it is an instance of, the disorder it is a subtype or feature of.
2. Parent test: everything the candidate states must also be true of the mention. A parent may be less specific, but it must not add a restriction the mention does not have, such as an organ, body part, target, population or condition.
3. Prefer the most specific candidate that passes the parent test ("Sleep, Slow-Wave" over "Sleep" for slow-wave sleep duration).
4. Return NIL when:
   - the candidate is a sibling, not a parent: the same kind of thing applied to a different organ, body part, target or population ("Ischemic Preconditioning, Myocardial" for limb ischemic conditioning);
   - the candidate's words match but its sense does not fit the context (a learning-theory "reinstatement" for an eye-movement measure);
   - every candidate is only associated with the mention (a cause, effect, risk factor or co-occurring condition);
   - the only candidate that passes the parent test is far broader than the mention;
   - the mention is a study design, statistic, demographic variable or lifestyle factor the candidates do not cover.
5. Set selected_id to a candidate id copied exactly, or to NIL.
6. Set confidence to about 0.8 when the mention clearly measures or instantiates the candidate, and 0.5 to 0.7 when context was needed. For NIL, give your confidence that no candidate fits.
7. Write brief_explanation as one short sentence naming the relation (part, property, measure, kind or activity of), or for NIL why the closest candidate fails.
</rules>

<examples>
<example>
<mention>hippocampal volume</mention>
<context>Older adults who walked daily had larger hippocampal volume after one year</context>
<candidates>
MESH:D006624 | Hippocampus | Anatomy | Ammon Horn | Cornu Ammonis
MESH:D008568 | Memory | Mental Process | Memories
</candidates>
<output>{"selected_id": "MESH:D006624", "confidence": 0.85, "brief_explanation": "Hippocampal volume is a measurement of the hippocampus."}</output>
</example>
<example>
<mention>remote ischemic limb conditioning</mention>
<context>Remote ischemic limb conditioning reduced brain edema after stroke in mice</context>
<candidates>
MESH:D019157 | Ischemic Preconditioning, Myocardial | Procedure | Myocardial Preconditioning
MESH:D019194 | Ischemic Preconditioning | Procedure | Preconditioning, Ischemic
</candidates>
<output>{"selected_id": "MESH:D019194", "confidence": 0.8, "brief_explanation": "Limb conditioning is a kind of ischemic preconditioning; the myocardial term is a sibling restricted to the heart."}</output>
</example>
<example>
<mention>remote ischemic limb conditioning</mention>
<context>Remote ischemic limb conditioning reduced brain edema after stroke in mice</context>
<candidates>
MESH:D019157 | Ischemic Preconditioning, Myocardial | Procedure | Myocardial Preconditioning
</candidates>
<output>{"selected_id": "NIL", "confidence": 0.85, "brief_explanation": "The only candidate is restricted to the heart, a sibling of limb conditioning, not a parent."}</output>
</example>
<example>
<mention>poor sleep</mention>
<context>Poor sleep predicted later memory decline</context>
<candidates>
MESH:D008569 | Memory Disorders | Disease | Memory Loss
MESH:D012890 | Sleep | Physiology |
</candidates>
<output>{"selected_id": "MESH:D012890", "confidence": 0.7, "brief_explanation": "Poor sleep is a property of sleep; memory disorders are its outcome, not its anchor."}</output>
</example>
<example>
<mention>daily step count</mention>
<context>Higher daily step count was linked to lower dementia risk</context>
<candidates>
MESH:D003704 | Dementia | Disease | Amentia
</candidates>
<output>{"selected_id": "NIL", "confidence": 0.9, "brief_explanation": "Dementia is the outcome associated with step count, not something step count is part or a measure of."}</output>
</example>
</examples>"""