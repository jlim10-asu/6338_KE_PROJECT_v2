"""Output schema for statement -> claim adjudication."""
from pydantic import BaseModel, Field, ValidationInfo, model_validator
from core.types import Direction, Strength


class Adjudication(BaseModel):
    reasoning: str = Field(description="One or two sentences: which candidate reports the same finding and why, then how each original wording rates")
    match: int = Field(description="Number of the candidate that reports the same finding, or 0 if none does")
    statement_strength: Strength
    statement_direction: Direction
    claim_strength: Strength | None = Field(description="Strength of the matched candidate's original wording; null when match is 0")
    claim_direction: Direction | None = Field(description="Direction of the matched candidate; null when match is 0")
    null_reframed: bool = Field(description="True only when the matched claim reports no effect or no difference and the press statement presents that as a benefit or a safety assurance")

    @model_validator(mode="after")
    def match_rules(self, info: ValidationInfo):
        n = (info.context or {}).get("n")
        if n is not None and not 0 <= self.match <= n:
            raise ValueError(f"match must be between 0 and {n}")
        if self.match and (self.claim_strength is None or self.claim_direction is None):
            raise ValueError("claim_strength and claim_direction are required when match is not 0")
        return self
