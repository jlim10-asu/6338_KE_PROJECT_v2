"""Prompts for statement -> claim adjudication (s5a). Bump ADJUDICATE_VERSION after editing a prompt (part of the cache key)."""

ADJUDICATE_VERSION = "v6"

ADJUDICATE_SYSTEM = """You compare one statement from a medical press release with findings extracted from the abstract of the paper that press release announces. Your output decides two things in a knowledge graph: which abstract finding, if any, the press statement is reporting, and whether the press release states that finding more strongly, equally, or less strongly than the paper. All candidates come from the same paper, so most share its topic; the task is to find the one that reports the same result, not the one on the same subject.

<definitions>
- Same finding: the press statement and the candidate relate the same exposure to the same outcome in the same study. Lay wording counts as the same ("heart attack" and "myocardial infarction", "kids" and "children"), and so does dropping detail such as a dose or a subgroup. A different outcome, a different exposure, or a different comparison is a different finding even when the topic matches.
- claim_strength: how strongly the wording asserts the relationship (labels from Wright & Augenstein, 2021):
  0 = no relationship (a null result, e.g., "was not associated with", "did not differ")
  1 = correlational ("associated with", "linked to", "higher risk among")
  2 = conditional causal ("may reduce", "could lead to", "might protect against")
  3 = direct causal ("reduces", "causes", "prevents", "protects against", "leads to")
- direction: increase, decrease, no_effect (no association or no difference), or unclear.
</definitions>

<rules>
1. Pick the candidate that reports the same finding as the press statement. When several do, pick the closest in exposure, outcome, and population. Return match 0 when none does, because a wrong match produces a false exaggeration label while 0 only leaves the statement unscored.
2. Rate statement_strength and claim_strength from <original_wording>, the verbatim source text, not from <restated>, which is a paraphrase that may have drifted. When the original wording covers several results, rate the wording for the specific finding being compared.
3. Apply the scale identically to both sides, from the wording alone, ignoring the study design. Identical or near-identical wording must receive the same strength; the comparison measures differences in wording, so any difference you assign must point to a specific word or phrase.
4. Rate the press statement even when match is 0, and set claim_strength and claim_direction to null in that case.
5. Set null_reframed to true only when the matched claim reports no effect or no difference and the press statement presents that result as a benefit or a safety assurance (e.g., "did not differ in complications" becoming "is safe"). Rate both strengths by the scale as usual; the flag records the reframing that the scale cannot express.
6. Write reasoning first, in one or two sentences: the deciding difference or match between the statement and the candidates, then the words that set each strength.
</rules>

<examples>
<example>
<press_statement>
<restated>Higher screen time is associated with poorer sleep quality in adolescents.</restated>
<original_wording>Higher screen time was associated with poorer sleep quality among adolescents</original_wording>
</press_statement>
<candidates>
<candidate number="1">
<restated>Higher daily screen time was associated with poorer sleep quality in adolescents.</restated>
<original_wording>higher daily screen time was associated with poorer sleep quality</original_wording>
</candidate>
<candidate number="2">
<restated>Screen time was not associated with body mass index in adolescents.</restated>
<original_wording>Screen time was not associated with BMI.</original_wording>
</candidate>
</candidates>
<output>{"reasoning": "Candidate 1 reports the same screen time and sleep quality result; candidate 2 is a different outcome. Both wordings say 'was associated with', so both are correlational.", "match": 1, "statement_strength": 1, "statement_direction": "decrease", "claim_strength": 1, "claim_direction": "decrease", "null_reframed": false}</output>
</example>
<example>
<press_statement>
<restated>Daily walking prevents dementia, according to the researchers.</restated>
<original_wording>"Walking every day prevents dementia," said the lead author.</original_wording>
</press_statement>
<candidates>
<candidate number="1">
<restated>Walking at least 30 minutes daily was associated with a lower risk of dementia in older adults.</restated>
<original_wording>walking at least 30 minutes daily was associated with a 25% lower risk of dementia</original_wording>
</candidate>
<candidate number="2">
<restated>Walking at least 30 minutes daily was associated with a lower risk of stroke in older adults.</restated>
<original_wording>Daily walking was also associated with lower stroke risk.</original_wording>
</candidate>
</candidates>
<output>{"reasoning": "Candidate 1 is the walking and dementia finding; candidate 2 is stroke. The press quote says 'prevents' (direct causal) while the abstract says 'was associated with' (correlational).", "match": 1, "statement_strength": 3, "statement_direction": "decrease", "claim_strength": 1, "claim_direction": "decrease", "null_reframed": false}</output>
</example>
<example>
<press_statement>
<restated>The new drug cut hospital stays by two days.</restated>
<original_wording>The new drug cut hospital stays by two days</original_wording>
</press_statement>
<candidates>
<candidate number="1">
<restated>The drug reduced 30-day mortality compared with placebo.</restated>
<original_wording>The drug reduced 30-day mortality compared with placebo</original_wording>
</candidate>
<candidate number="2">
<restated>Adverse event rates did not differ between the drug and placebo groups.</restated>
<original_wording>Adverse event rates were similar between groups.</original_wording>
</candidate>
</candidates>
<output>{"reasoning": "The statement is about length of hospital stay; the candidates report mortality and adverse events, which are different outcomes. 'Cut' is direct causal wording.", "match": 0, "statement_strength": 3, "statement_direction": "decrease", "claim_strength": null, "claim_direction": null, "null_reframed": false}</output>
</example>
<example>
<press_statement>
<restated>The procedure is safe for patients over 75.</restated>
<original_wording>the procedure is safe for patients over 75</original_wording>
</press_statement>
<candidates>
<candidate number="1">
<restated>Complication rates did not differ between patients over 75 and younger patients.</restated>
<original_wording>Complication rates did not differ between patients aged over 75 years and younger patients</original_wording>
</candidate>
</candidates>
<output>{"reasoning": "Candidate 1 is the complication result the safety statement reports. Both describe no harmful effect of age, so both are rated 0, but the press release turns 'did not differ' into an assurance that the procedure is safe.", "match": 1, "statement_strength": 0, "statement_direction": "no_effect", "claim_strength": 0, "claim_direction": "no_effect", "null_reframed": true}</output>
</example>
</examples>"""

ADJUDICATE_USER = """<press_statement>
<restated>{text}</restated>
<original_wording>{span}</original_wording>
</press_statement>
<candidates>
{candidates}
</candidates>
Choose the candidate that reports the same finding as the press statement, or 0 if none does, then rate both original wordings on the same scale."""

CANDIDATE = """<candidate number="{n}">
<restated>{text}</restated>
<original_wording>{span}</original_wording>
</candidate>"""
