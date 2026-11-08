"""Output schema for community reports."""
from pydantic import BaseModel, Field, ValidationInfo, model_validator


class KeyFinding(BaseModel):
    text: str = Field(description="One sentence stating the finding with the papers' own strength of wording")
    claim_refs: list[str] = Field(description="Claim IDs from the input, e.g. C3, that state this finding")

class CommunitySummary(BaseModel):
    """the summaries stage community report (Edge et al. 2024). Validation context: {"known": set of claim IDs shown}."""
    title: str = Field(description="At most 12 words naming the shared exposures and outcomes")
    summary: str = Field(description="3 to 5 sentences: the topic, where papers agree or conflict, how press coverage compares")
    key_findings: list[KeyFinding] = Field(description="1 to 5 findings, each citing the claims that state it")

    @model_validator(mode="after")
    def report_rules(self, info: ValidationInfo):
        if len(self.title.split()) > 12:
            raise ValueError("title must be at most 12 words")
        if not 1 <= len(self.key_findings) <= 5:
            raise ValueError("key_findings must list 1 to 5 findings")
        known = (info.context or {}).get("known")
        for k in self.key_findings:
            if not k.claim_refs:
                raise ValueError("every key finding must cite at least one claim ID")
            if known is not None and (missing := [r for r in k.claim_refs if r not in known]):
                raise ValueError(f"claim_refs not in the input: {missing}")
        return self
