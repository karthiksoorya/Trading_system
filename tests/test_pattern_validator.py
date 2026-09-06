import json
from agents.learning_models import KnowledgeEntry
from agents.knowledge_store import KnowledgeStore
from agents.pattern_validator import summarize, hypotheses, load_observations, run_validation

def make(path, ident, classification, **fields):
    evidence={"source_trade_id":ident,"classification":classification,"source_trade":{"zone_type":"DBD","zone_class":"supply","timeframe":"5minute",**fields},**fields}
    KnowledgeStore(path).append(KnowledgeEntry(id=f"TRADE-REFLECTION-{ident}",category="completed_trade_reflection",title="x",source="x",learning="x",status="OBSERVED",evidence=(json.dumps(evidence),),sample_count=1))

def test_grouping_numeric_missing_and_duplicate_latest(tmp_path):
    path=tmp_path/"k.jsonl"; make(path,1,"ZONE_CORRECT_OPTION_CORRECT",holding_minutes=10,iv_entry=None); make(path,2,"ZONE_CORRECT_OPTION_CORRECT",holding_minutes=20)
    duplicate=json.loads(path.read_text().splitlines()[-1]); duplicate["evidence"][0]=duplicate["evidence"][0].replace('"holding_minutes": 20','"holding_minutes": 99'); path.write_text(path.read_text()+json.dumps(duplicate)+'\n')
    result=summarize(path); assert result["groups"]["ZONE_CORRECT_OPTION_CORRECT"]["sample_count"] == 2; assert result["groups"]["ZONE_CORRECT_OPTION_CORRECT"]["numeric"]["holding_minutes"]["mean"] == 54.5
    assert "iv_entry" not in result["groups"]["ZONE_CORRECT_OPTION_CORRECT"]["numeric"]

def test_insufficient_evidence_and_hypotheses(tmp_path):
    path=tmp_path/"k.jsonl"
    for i in range(3): make(path,i+1,"ZONE_CORRECT_OPTION_CORRECT",booster_score=8)
    for i in range(3): make(path,10+i,"ZONE_CORRECT_OPTION_WRONG",booster_score=10)
    result=summarize(path,min_samples=3); assert result["comparison"]["status"] == "OK"; assert any(x["metric"]=="booster_score" for x in hypotheses(result))
    assert summarize(path,min_samples=4)["comparison"]["status"] == "INSUFFICIENT_EVIDENCE"

def test_hypothesis_persistence_never_validated(tmp_path):
    path=tmp_path/"k.jsonl"
    for i in range(3): make(path,i+1,"ZONE_CORRECT_OPTION_CORRECT",booster_score=8)
    for i in range(3): make(path,10+i,"ZONE_CORRECT_OPTION_WRONG",booster_score=10)
    run_validation(path,min_samples=3,dry_run=False); entries=KnowledgeStore(path).read_entries(); h=[e for e in entries if e.category=="pattern_validation"]; assert h and all(e.status.value=="HYPOTHESIS" and not e.live_use_allowed for e in h)

def test_malformed_jsonl_safe(tmp_path):
    path=tmp_path/"k.jsonl"; path.write_text('{bad}\n'); assert load_observations(path)==[]; assert summarize(path)["comparison"]["status"]=="INSUFFICIENT_EVIDENCE"
