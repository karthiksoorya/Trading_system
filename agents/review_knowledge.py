"""Human-gated review and distillation for offline validation candidates."""
import argparse, json
from datetime import date
from pathlib import Path
from agents.hypothesis_validator import validate
from agents.knowledge_store import KnowledgeStore, DEFAULT_PATH
from agents.learning_models import KnowledgeEntry, KnowledgeStatus

HUMAN_KNOWLEDGE = Path(__file__).resolve().parents[1] / "AGENT_KNOWLEDGE.md"

def _candidate(path, hypothesis_id, **kwargs):
    result=validate(path, **kwargs)
    return next((x for x in result["hypotheses_tested"] if x["hypothesis_id"]==hypothesis_id), None)

def review_candidates(path=DEFAULT_PATH, **kwargs):
    return [x for x in validate(path, **kwargs)["hypotheses_tested"] if x["status"]=="VALIDATION_CANDIDATE"]

def _distill(candidate, entry_id, review_date):
    return (f"\n\n## {entry_id}: {candidate['metric']}\n\n"
            f"- **ID/title:** {entry_id} — {candidate['metric']} validation candidate\n"
            "- **Category:** offline pattern validation\n"
            "- **Status:** VALIDATED (human approved)\n"
            f"- **Learning:** {candidate['metric']} showed the same descriptive effect direction in development and holdout samples.\n"
            f"- **Evidence summary:** Development n={candidate['development_sample_count']}, holdout n={candidate['holdout_sample_count']}; effects {candidate['development_effect']} and {candidate['holdout_effect']}.\n"
            "- **Limitations:** Descriptive observational evidence; no causal or significance claim; proxy/model-derived values may be present; subgroup and after-cost evidence may be incomplete.\n"
            "- **Live-use eligibility:** False — separate live-use approval is required.\n"
            f"- **Next validation/review date:** {review_date}\n")

def decide(action, hypothesis_id, *, path=DEFAULT_PATH, confirm=False, min_development=3, min_holdout=3, human_path=HUMAN_KNOWLEDGE):
    candidate=_candidate(path,hypothesis_id,min_development=min_development,min_holdout=min_holdout)
    if candidate is None: raise ValueError("Hypothesis is not a current validation candidate")
    if action=="validate":
        if confirm is not True: raise ValueError("validate requires --confirm")
        if candidate["status"]!="VALIDATION_CANDIDATE": raise ValueError("Insufficient evidence blocks validation")
    store=KnowledgeStore(path); existing={e.id:e for e in store.read_entries()}
    entry=existing.get(hypothesis_id)
    status=KnowledgeStatus.VALIDATED if action=="validate" else (KnowledgeStatus.REJECTED if action=="reject" else KnowledgeStatus.HYPOTHESIS)
    observed=json.dumps({"review_action":action,"review_date":date.today().isoformat(),"candidate":candidate},sort_keys=True)
    if entry:
        if action=="validate": store.update_status(hypothesis_id, status, approved=True, reason="Explicit human review")
        else: store.update_status(hypothesis_id,status,reason=f"Human decision: {action}")
    else:
        store.append(KnowledgeEntry(id=hypothesis_id,category="pattern_validation",title=f"{candidate['metric']} validation",source="Phase 4C + human review",learning=candidate["metric"],status=status,evidence=(observed,),sample_count=min(candidate["development_sample_count"],candidate["holdout_sample_count"])))
    if action=="validate":
        with human_path.open("a",encoding="utf-8") as handle: handle.write(_distill(candidate,hypothesis_id,date.today().isoformat()))
    return status.value

def main(argv=None):
    p=argparse.ArgumentParser(description="Human review of offline validation candidates")
    p.add_argument("--knowledge-path",default=DEFAULT_PATH); p.add_argument("--hypothesis-id"); p.add_argument("--decision",choices=["approve-more-validation","reject","defer","validate"]); p.add_argument("--confirm",action="store_true"); p.add_argument("--min-development",type=int,default=3); p.add_argument("--min-holdout",type=int,default=3); p.add_argument("--dry-run",action="store_true")
    a=p.parse_args(argv); candidates=review_candidates(a.knowledge_path,min_development=a.min_development,min_holdout=a.min_holdout)
    for c in candidates: print(json.dumps(c,sort_keys=True))
    if a.decision and a.hypothesis_id:
        if a.dry_run: print(json.dumps({"action":a.decision,"id":a.hypothesis_id,"dry_run":True}))
        else: print(json.dumps({"action":a.decision,"id":a.hypothesis_id,"status":decide(a.decision,a.hypothesis_id,path=a.knowledge_path,confirm=a.confirm,min_development=a.min_development,min_holdout=a.min_holdout)}))
if __name__=="__main__": main()
