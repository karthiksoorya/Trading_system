import json
from agents.knowledge_store import KnowledgeStore
from agents.learning_models import KnowledgeEntry
from agents.hypothesis_validator import validate

def add(path,i,day,cls,value):
 ev={"source_trade_id":i,"classification":cls,"source_trade":{"date":day,"time_signal":"10:00:00"},"booster_score":value}
 KnowledgeStore(path).append(KnowledgeEntry(id=f"T-{i}",category="completed_trade_reflection",title="x",source="x",learning="x",status="OBSERVED",evidence=(json.dumps(ev),)))
def dataset(path, unstable=False):
 i=1
 for day in ["2024-01-01","2024-01-02","2024-01-03","2024-01-04"]:
  for _ in range(3): add(path,i,day,"ZONE_CORRECT_OPTION_CORRECT",8); i+=1
  val=10 if not unstable or day<"2024-01-03" else 7
  for _ in range(3): add(path,i,day,"ZONE_CORRECT_OPTION_WRONG",val); i+=1
def test_chronological_consistent_and_unstable(tmp_path):
 p=tmp_path/"k.jsonl"; dataset(p); r=validate(p,train_ratio=.5,min_development=3,min_holdout=3); x=[x for x in r["hypotheses_tested"] if x["metric"]=="booster_score"][0]; assert x["status"]=="VALIDATION_CANDIDATE"; assert r["train_count"]==12 and r["holdout_count"]==12
 p2=tmp_path/"u.jsonl"; dataset(p2,True); assert [x for x in validate(p2,train_ratio=.5,min_development=3,min_holdout=3)["hypotheses_tested"] if x["metric"]=="booster_score"][0]["status"]=="REJECTED"
def test_insufficient_and_post_trade_excluded(tmp_path):
 p=tmp_path/"k.jsonl"; dataset(p); r=validate(p,train_ratio=.5,min_development=20,min_holdout=20); assert all(x["status"]=="INSUFFICIENT_EVIDENCE" for x in r["hypotheses_tested"]); assert all(x["metric"] not in ("holding_minutes","option_premium_change_pct") for x in r["hypotheses_tested"])
