"""Output schemas for extraction (FindingRef) and entity linking (NedDecision). Validators run with context."""
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from core.types import Direction, EntityType, Strength, Unit


class FindingRef(BaseModel):
    """A finding that cites numbered sentences instead of copying text (the extract stage v4; entity types added in v6). Code builds
    evidence_span from the IDs. Validation context: {"citable": set of IDs the finding may cite, "known": set of every ID shown}."""
    text: str; exposure: str; outcome: str; population: str | None = None
    exposure_type: EntityType = Field(description="Semantic type of the exposure entity")
    outcome_type: EntityType = Field(description="Semantic type of the outcome entity")
    direction: Direction; claim_strength: Strength
    evidence_ids: list[str] = Field(description="IDs of the sentence(s) that state the finding, e.g. [\"P2.S3\"] or [\"S7\"]")
    confidence_score: Unit

    @field_validator("evidence_ids")
    @classmethod
    def ids_exist(cls, v: list[str], info: ValidationInfo) -> list[str]:
        v = list(dict.fromkeys(i.strip().strip("[]") for i in v if i and i.strip()))
        if not v:
            raise ValueError("evidence_ids must list at least one sentence ID")
        ctx = info.context or {}
        known, citable = ctx.get("known"), ctx.get("citable")
        if known is not None and (missing := [i for i in v if i not in known]):
            raise ValueError(f"evidence_ids not in the numbered text: {missing}")
        if citable is not None and (blocked := [i for i in v if i not in citable]):
            raise ValueError(f"evidence_ids cite context, background or methods sentences, which cannot be evidence: {blocked}")
        return v

    @model_validator(mode="after")
    def ontology_rules(self):
        if not self.exposure.strip() or not self.outcome.strip():
            raise ValueError("exposure and outcome must be non-empty")
        if self.exposure.strip().lower() == self.outcome.strip().lower():
            raise ValueError("exposure and outcome must be different entities")
        return self

class ExtractionRefResult(BaseModel):
    findings: list[FindingRef]

class NedDecision(BaseModel):
    selected_id: str = Field(description="Candidate id, or NIL if no candidate denotes the mention")
    confidence: Unit
    brief_explanation: str
