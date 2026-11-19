"""Structured outputs for the question-answering stage: router decision (FR-5 to FR-7) and generator answer (FR-5, FR-8)."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator

PathType = Literal["local", "cross_paper", "aggregate", "global", "unsupported"]
TemplateId = Literal["statement_check", "claim_check", "release_check", "cross_paper",
                     "agg_rate_by_year", "agg_institution_rank", "agg_by_entity", "topic_overview"]
TEMPLATE_PATH = {
    "statement_check": "local", "claim_check": "local", "release_check": "local", "cross_paper": "cross_paper",
    "agg_rate_by_year": "aggregate", "agg_institution_rank": "aggregate", "agg_by_entity": "aggregate",
    "topic_overview": "global",
}


class RouterParams(BaseModel):
    text: str | None = Field(None, description="The statement or finding restated as a plain sentence without question words, else null")
    entities: list[str] = Field(default_factory=list, description="Exposures, outcomes, drugs or conditions named, as short noun phrases")
    euid: str | None = Field(None, description="Press release ID exactly as written, else null")
    doi: str | None = Field(None, description="DOI exactly as written, else null")
    year_from: int | None = Field(None, description="First release year of a requested range, else null")
    year_to: int | None = Field(None, description="Last release year of a requested range, else null")
    min_releases: int | None = Field(None, description="Minimum releases per institution if stated, else null")


class RouterDecision(BaseModel):
    reasoning: str = Field(description="One sentence: what the question asks and why that type and template fit")
    path_type: PathType
    template_id: TemplateId | None = Field(description="Null only when path_type is unsupported")
    params: RouterParams

    @model_validator(mode="after")
    def template_matches_path(self):
        if self.path_type == "unsupported":
            if self.template_id is not None:
                raise ValueError("template_id must be null when path_type is unsupported")
        elif self.template_id is None or TEMPLATE_PATH[self.template_id] != self.path_type:
            raise ValueError(f"template {self.template_id} does not answer {self.path_type} questions")
        return self


class AnswerSentence(BaseModel):
    text: str = Field(description="One sentence of the answer, without citation markers")
    kind: Literal["finding", "limitation"] = Field(description="finding: states something from the rows (must cite); "
                                                                "limitation: says what the rows cannot show")
    citations: list[str] = Field(description="IDs from the rows, each as doi:<DOI> or release:<euid>; empty only for limitations")


class GeneratorOutput(BaseModel):
    sentences: list[AnswerSentence] = Field(description="Direct answer first, then the supporting evidence")
    insufficient_context: bool = Field(description="True when the rows do not contain what the question asks")
