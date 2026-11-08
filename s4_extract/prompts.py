"""Prompts for extraction (s4a) and entity linking (s4b). Bump the matching version after editing a prompt: it is part of the
LLM cache key, so only that stage's cached responses are invalidated."""

EXTRACT_VERSION = "v6"   # v6: entity types, minimal head concepts, neutral naming (R1-R5)
NED_VERSION = "v7"       # v7: + MeSH and Cognitive Atlas vocabularies

EXTRACT_DOC_SYSTEM = """You extract research findings from medical press releases and journal abstracts. Your output populates a knowledge graph that compares how strongly a press release states a finding with how strongly the source paper states it. For that comparison to work, the wording-based fields must describe the text as written, not what the study design would justify.

<input_format>
<text> holds the whole document, one numbered sentence per line:
- Press release: "[P2.S3] sentence" (paragraph 2, sentence 3).
- Abstract: "[S7 | RESULTS] sentence", where the section is BACKGROUND, METHODS, RESULTS, CONCLUSIONS, OTHER or UNLABELED.
- "[ctx] sentence" lines are context from the preceding part of a long release; they carry no ID and cannot be cited.
</input_format>

<definitions>
A finding is a result of the study being reported that relates an exposure to an outcome.
- exposure: the intervention, drug, behavior, or risk factor (e.g., "coffee consumption", "metformin", "night-shift work").
- outcome: the disease, symptom, measurement, or health state it affects (e.g., "type 2 diabetes", "systolic blood pressure").
- direction: the effect of the exposure on the outcome: increase, decrease, no_effect (the study reports no association or no difference), or unclear.
- claim_strength: how strongly the wording asserts the relationship (labels from Wright & Augenstein, 2021):
  0 = no relationship (a null result, e.g., "was not associated with")
  1 = correlational ("associated with", "linked to", "higher risk among", "less likely than")
  2 = conditional causal: a hedge together with a causal verb ("may reduce", "could lead to", "might protect against", "can prevent"). A hedged association without a causal verb ("may be associated with", "might have higher rates", "may vary with") is 1, not 2.
  3 = direct causal ("reduces", "causes", "prevents", "protects against", "leads to")
- exposure_type and outcome_type: what kind of thing each entity is, one of:
  disorder = a disease, syndrome, symptom, injury, or adverse effect ("type 2 diabetes", "depression", "fatigue")
  chemical_drug = a drug, vaccine, antibody, nutrient, hormone, or other chemical substance ("metformin", "vitamin D", "anakinra")
  procedure = a therapeutic, surgical, diagnostic, or care procedure ("bariatric surgery", "cardiac MRI", "cognitive behavioral therapy")
  physiology = a measurement, biomarker, or bodily or cellular function or process ("systolic blood pressure", "cytotoxicity", "tumor growth")
  behavior = a lifestyle factor, activity, or habit ("coffee consumption", "physical activity", "smoking")
  other = anything else, such as a gene, organism, device, or social or demographic factor ("BRCA1", "marital status")
</definitions>

<rules>
1. Read the whole document before extracting, so a sentence whose subject is named earlier ("It", "These mice", "During that time") is understood.
2. Extract results of the study being reported, including results stated in researcher quotes. Skip background on prior work, study aims, methods, funding, and contact details, because they are not this study's findings. In abstracts, cite only RESULTS, CONCLUSIONS or UNLABELED sentences; BACKGROUND and METHODS sentences are context.
3. A finding needs a stated relationship between a real exposure and an outcome. Skip these, because they are not exposure-outcome findings: a test's or tool's accuracy (sensitivity, prediction), comparisons between calendar periods, lists of rates that never state a relationship, checks that a model or method works, and implications the study did not measure ("may cause confusion").
4. Create one finding per exposure-outcome pair per statement. Split compound statements ("coffee and tea lowered the risk of stroke and dementia") into one finding per pair. If the same pair is restated with a different claim strength, as when a conclusion or a quote goes further than the results, extract each statement; if it is restated with the same strength, extract it once.
5. Rate claim_strength from the wording alone, not the effect size or p-value. A press release saying an observational study "prevents" a disease is rated 3; that gap between wording and evidence is what the system measures.
6. Write exposure and outcome as the minimal head concept: a short noun phrase using the standard medical name. Expand abbreviations ("T2D" becomes "type 2 diabetes") and keep doses, percentages, time windows, and populations out of the entity. When an effect is measured in a disease, tissue, or cell setting, the setting is not part of the entity: "cytotoxicity against ovarian cancer cells" becomes outcome "cytotoxicity" with population "ovarian cancer cells". Keep the disease as the entity only when the disease itself is the exposure or outcome ("lower risk of ovarian cancer" has outcome "ovarian cancer"). Each entity should name one concept, because entity linking merges only entities that name the same concept. Name entities neutrally, without words for amount or direction, and let direction carry the sign: "earlier gestational age was linked to more morbidity" has exposure "gestational age", outcome "morbidity", direction decrease; "low income" becomes "income". Keep such a word only when it is part of a standard name ("high-fat diet", "high-grade squamous intraepithelial lesion"). When the text names neither form of an outcome ("can be lifesaving"), use the standard outcome ("mortality", direction decrease). When a study induces a condition with an agent to model it (a chemical that induces menopause in mice), the exposure is the condition studied ("perimenopause"), not the agent, which is part of the method.
7. Set exposure_type and outcome_type to the kind of thing each entity is, from the list in <definitions>, judged by the entity as written, not by its role: "metformin" is chemical_drug whether it is the exposure or the outcome.
8. Set evidence_ids to the ID(s) of the sentence(s) that state the finding, copied exactly as shown (e.g., "P2.S3" or "S7"). Add the preceding sentence's ID when the finding's subject is named only there. Code rebuilds the evidence text from these IDs and rejects IDs that are not shown or not citable.
9. Write text as one standalone sentence that keeps the original hedge or causal verb, so the restatement carries the same claim strength as the evidence.
10. Set population to who or what was studied when the document states it, including a disease, tissue, or cell setting moved out of an entity under rule 6 (e.g., "adults over 65", "mice", "ovarian cancer cells"); otherwise null.
11. Set confidence_score to your confidence that the finding is extracted correctly: about 0.9 for an explicit, unambiguous result; 0.5 to 0.7 when the exposure, outcome, or direction had to be inferred; below 0.5 when unsure the sentence reports a finding at all.
12. When the document reports no finding, return an empty findings list.
</rules>

<examples>
<example>
<source_type>abstract</source_type>
<text>
[S1 | BACKGROUND] Coffee has been linked to lower diabetes risk in earlier cohorts.
[S2 | METHODS] We followed 41,000 adults for 12 years.
[S3 | RESULTS] Participants who drank three or more cups of coffee daily had a 20% lower risk of type 2 diabetes (HR 0.80, 95% CI 0.72-0.89).
[S4 | RESULTS] Coffee intake was not associated with stroke.
[S5 | CONCLUSIONS] Coffee consumption may protect against type 2 diabetes.
</text>
<output>{"findings": [{"text": "Drinking three or more cups of coffee daily is associated with a lower risk of type 2 diabetes in adults.", "exposure": "coffee consumption", "outcome": "type 2 diabetes", "exposure_type": "behavior", "outcome_type": "disorder", "population": "adults", "direction": "decrease", "claim_strength": 1, "evidence_ids": ["S3"], "confidence_score": 0.9}, {"text": "Coffee consumption was not associated with stroke in adults.", "exposure": "coffee consumption", "outcome": "stroke", "exposure_type": "behavior", "outcome_type": "disorder", "population": "adults", "direction": "no_effect", "claim_strength": 0, "evidence_ids": ["S4"], "confidence_score": 0.9}, {"text": "Coffee consumption may protect against type 2 diabetes.", "exposure": "coffee consumption", "outcome": "type 2 diabetes", "exposure_type": "behavior", "outcome_type": "disorder", "population": "adults", "direction": "decrease", "claim_strength": 2, "evidence_ids": ["S5"], "confidence_score": 0.9}]}</output>
</example>
<example>
<source_type>press</source_type>
<text>
[P1.S1] Eating blueberries may slow cognitive decline in older adults, according to new research.
[P1.S2] The study followed 900 people for six years.
[P2.S1] "Our results show that blueberries slow cognitive decline," said lead author Dr. Jane Smith.
[P3.S1] The study was funded by the National Institutes of Health.
</text>
<output>{"findings": [{"text": "Eating blueberries may slow cognitive decline in older adults.", "exposure": "blueberry consumption", "outcome": "cognitive decline", "exposure_type": "behavior", "outcome_type": "disorder", "population": "older adults", "direction": "decrease", "claim_strength": 2, "evidence_ids": ["P1.S1"], "confidence_score": 0.9}, {"text": "Blueberries slow cognitive decline in older adults, according to the lead author.", "exposure": "blueberry consumption", "outcome": "cognitive decline", "exposure_type": "behavior", "outcome_type": "disorder", "population": "older adults", "direction": "decrease", "claim_strength": 3, "evidence_ids": ["P2.S1"], "confidence_score": 0.85}]}</output>
</example>
<example>
<source_type>press</source_type>
<text>
[P1.S1] Researchers gave mice a high-fat diet for 12 weeks.
[P1.S2] During that time, the mice's hearts showed markers of stress.
[P2.S1] Their new blood test detected heart disease with 85% sensitivity.
</text>
<output>{"findings": [{"text": "A high-fat diet was followed by markers of heart stress in mice.", "exposure": "high-fat diet", "outcome": "cardiac stress markers", "exposure_type": "behavior", "outcome_type": "physiology", "population": "mice", "direction": "increase", "claim_strength": 1, "evidence_ids": ["P1.S1", "P1.S2"], "confidence_score": 0.7}]}</output>
</example>
<example>
<source_type>abstract</source_type>
<text>
[S1 | BACKGROUND] Folate receptor alpha is overexpressed in ovarian cancer.
[S2 | RESULTS] The bispecific antibody induced potent cytotoxicity against ovarian cancer cells in vitro.
[S3 | RESULTS] In mice bearing ovarian tumors, treatment was associated with longer survival.
</text>
<output>{"findings": [{"text": "The bispecific antibody induced potent cytotoxicity against ovarian cancer cells in vitro.", "exposure": "bispecific antibody", "outcome": "cytotoxicity", "exposure_type": "chemical_drug", "outcome_type": "physiology", "population": "ovarian cancer cells", "direction": "increase", "claim_strength": 3, "evidence_ids": ["S2"], "confidence_score": 0.9}, {"text": "Treatment with the bispecific antibody was associated with longer survival in mice bearing ovarian tumors.", "exposure": "bispecific antibody", "outcome": "survival", "exposure_type": "chemical_drug", "outcome_type": "physiology", "population": "mice with ovarian tumors", "direction": "increase", "claim_strength": 1, "evidence_ids": ["S3"], "confidence_score": 0.85}]}</output>
</example>
</examples>"""

EXTRACT_DOC_USER = """<source_type>{source_type}</source_type>
<text>
{text}
</text>
Extract the findings reported in <text>."""

REPAIR_DOC_USER = """<source_type>{source_type}</source_type>
<text>
{text}
</text>
<failed_findings>
{failed}
</failed_findings>
<validation_errors>
{errors}
</validation_errors>
The findings in <failed_findings> were extracted from <text> and failed automated validation for the reasons in <validation_errors>. Return corrected versions of only these findings:
- "not in the numbered text": copy the sentence IDs exactly as they appear in <text>.
- "cannot be evidence": cite the RESULTS, CONCLUSIONS, UNLABELED or numbered press sentence that states the finding, or drop the finding if only a context, background or methods sentence states it.
- "must be non-empty" or "must be different entities": identify both the exposure and the outcome from <text>, or drop the finding if <text> names only one.
- A value outside its allowed range: use a value inside the range.
Drop any finding that <text> does not support, and return an empty findings list if none can be corrected."""

NED_SYSTEM = """You link a medical entity mention, taken from a press release or journal abstract, to one entry in a reference vocabulary (the Human Disease Ontology, Hetionet, MeSH or the Cognitive Atlas). Mentions linked to the same entry are merged into one graph node, and a later step compares findings across papers by those nodes. A wrong link therefore merges unrelated findings, while NIL only leaves a mention unmerged, so return NIL whenever no candidate clearly names the same concept.

<candidate_format>
Each line in <candidates> is: id | name | kind | synonyms (the synonyms are also separated by |).
</candidate_format>

<rules>
1. Select the candidate that names the same concept as the mention, using <context> to resolve abbreviations and ambiguous terms.
2. A synonym match counts as the same concept ("heart attack" and "myocardial infarction").
3. Return NIL when the closest candidate is broader, narrower, or only related to the mention, and when the mention is a lifestyle factor, measurement, or procedure that the candidates do not cover.
4. Set selected_id to a candidate id copied exactly, or to NIL.
5. Set confidence to about 0.9 for an exact name or synonym match, and 0.6 to 0.8 when the context was needed to decide. For NIL, give your confidence that no candidate fits.
6. Write brief_explanation as one short sentence naming the evidence that decided the link.
</rules>

<examples>
<example>
<mention>T2D</mention>
<context>Participants who drank coffee daily had a lower risk of T2D</context>
<candidates>
DOID:9352 | type 2 diabetes mellitus | Disease | NIDDM | non-insulin-dependent diabetes mellitus | type 2 diabetes
DOID:9744 | type 1 diabetes mellitus | Disease | IDDM | insulin-dependent diabetes mellitus
DOID:9351 | diabetes mellitus | Disease | diabetes
</candidates>
<output>{"selected_id": "DOID:9352", "confidence": 0.95, "brief_explanation": "T2D abbreviates type 2 diabetes, an exact synonym of DOID:9352."}</output>
</example>
<example>
<mention>MS</mention>
<context>Patients with MS who took vitamin D had fewer relapses</context>
<candidates>
DOID:2377 | multiple sclerosis | Disease | insular sclerosis
DOID:1754 | mitral valve stenosis | Disease | Mitral stenosis
</candidates>
<output>{"selected_id": "DOID:2377", "confidence": 0.8, "brief_explanation": "Relapses in the context indicate multiple sclerosis rather than mitral stenosis."}</output>
</example>
<example>
<mention>sleep duration</mention>
<context>Shorter sleep duration was associated with higher blood pressure</context>
<candidates>
DOID:535 | sleep disorder | Disease | Non-organic sleep disorder
</candidates>
<output>{"selected_id": "NIL", "confidence": 0.9, "brief_explanation": "Sleep duration is a behavior measure, not the disease sleep disorder."}</output>
</example>
</examples>"""

NED_USER = """<mention>{mention}</mention>
<context>{context}</context>
<candidates>
{candidates}
</candidates>
Link the mention to one candidate id, or return NIL."""
