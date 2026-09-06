import json
import pytest
from agents.knowledge_store import KnowledgeStore
from agents.review_knowledge import decide, review_candidates
def dataset(path):
 from tests.test_pattern_validator import make
 i=1
 for day in ["2024-01-01","2024-01-02","2024-01-03","2024-01-04"]:
  for _ in range(3): make(path,i,"ZONE_CORRECT_OPTION_CORRECT",booster_score=8); i+=1
  for _ in range(3): make(path,i,"ZONE_CORRECT_OPTION_WRONG",booster_score=10); i+=1

def test_review_requires_explicit_confirmation_and_blocks_small_samples(tmp_path):
 p=tmp_path/"k.jsonl"; dataset(p)
 candidates=review_candidates(p,min_development=3,min_holdout=3); assert candidates
 with pytest.raises(ValueError,match="confirm"): decide("validate",candidates[0]["hypothesis_id"],path=p)
 p2=tmp_path/"small.jsonl"; dataset(p2)
 with pytest.raises(ValueError,match="Insufficient evidence"): decide("validate",candidates[0]["hypothesis_id"],path=p2,min_development=99,min_holdout=99,confirm=True)

def test_reject_retained_and_validate_distills_without_raw_json(tmp_path):
 p=tmp_path/"k.jsonl"; dataset(p); candidates=review_candidates(p,min_development=3,min_holdout=3); target=candidates[0]["hypothesis_id"]
 assert decide("reject",target,path=p)=="REJECTED"; assert any(e.id==target and e.status.value=="REJECTED" for e in KnowledgeStore(p).read_entries())
 human=tmp_path/"AGENT_KNOWLEDGE.md"; assert decide("validate",target,path=p,confirm=True,human_path=human)=="VALIDATED"
 text=human.read_text(); assert "VALIDATED" in text and "raw" not in text.lower() and "Evidence summary" in text
 assert any(e.id==target and e.live_use_allowed is False for e in KnowledgeStore(p).read_entries())

def test_defer_and_dry_run_do_not_promote(tmp_path):
 p=tmp_path/"k.jsonl"; dataset(p); c=review_candidates(p,min_development=3,min_holdout=3)[0]
 assert decide("defer",c["hypothesis_id"],path=p)=="HYPOTHESIS"
