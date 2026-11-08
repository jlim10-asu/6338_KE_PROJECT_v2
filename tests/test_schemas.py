"""the extract stage output schema (FindingRef): entity rules and entity types. Sentence-ID checks are in test_ingest.py (test 7)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic import ValidationError
from s4_extract.schemas import ExtractionRefResult, FindingRef

CTX = {"known": {"S1", "S2"}, "citable": {"S2"}}
base = {"text": "Coffee consumption is associated with a lower risk of type 2 diabetes.", "exposure": "coffee consumption",
        "outcome": "type 2 diabetes", "exposure_type": "behavior", "outcome_type": "disorder", "direction": "decrease",
        "claim_strength": 1, "evidence_ids": ["S2"], "confidence_score": 0.9}


def rejects(change, needle):
    try:
        FindingRef.model_validate({**base, **change}, context=CTX)
    except ValidationError as e:
        assert needle in str(e), (change, str(e))
        return
    raise AssertionError(f"accepted: {change}")


assert FindingRef.model_validate(base, context=CTX).population is None
rejects({"outcome": "Coffee Consumption"}, "must be different entities")
rejects({"exposure": "  "}, "must be non-empty")
print("1 entity rules OK")

rejects({"outcome_type": "symptom"}, "outcome_type")
rejects({"claim_strength": 4}, "claim_strength")
rejects({"direction": "lower"}, "direction")
missing = {k: v for k, v in base.items() if k != "exposure_type"}
try:
    FindingRef.model_validate(missing, context=CTX)
    raise AssertionError("missing exposure_type accepted")
except ValidationError as e:
    assert "exposure_type" in str(e)
print("2 field values and required types OK")

r = ExtractionRefResult.model_validate({"findings": []})
assert r.findings == []
print("3 empty result OK\nALL TESTS PASSED")
