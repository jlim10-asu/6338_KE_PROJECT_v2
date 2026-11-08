"""Value types shared by every stage: claim strength, direction, unit interval, and the six entity types."""
from typing import Annotated, Literal
from pydantic import Field

Strength = Literal[0, 1, 2, 3]
Direction = Literal["increase", "decrease", "no_effect", "unclear"]
Unit = Annotated[float, Field(ge=0, le=1)]
# Semantic type of an extracted entity (v6), aligned with UMLS semantic groups / MeSH top-level categories.
# Order matters: majority_type() breaks ties by this order.
ENTITY_TYPES = ("disorder", "chemical_drug", "procedure", "physiology", "behavior", "other")
EntityType = Literal["disorder", "chemical_drug", "procedure", "physiology", "behavior", "other"]


def majority_type(votes: dict[str, int]) -> str:
    """Most frequent entity type across the findings that mention an entity; ties go to the earlier type in ENTITY_TYPES."""
    votes = {t: n for t, n in votes.items() if t in ENTITY_TYPES and n > 0}
    if not votes:
        return "other"
    return max(votes, key=lambda t: (votes[t], -ENTITY_TYPES.index(t)))
