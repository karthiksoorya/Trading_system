"""Deterministic, advisory-only trading assessment. Never places or blocks orders."""
from datetime import datetime, timezone
import json, math, sqlite3
from pathlib import Path
from agents.knowledge_store import KnowledgeStore, DEFAULT_PATH

ROOT=Path(__file__).resolve().parents[1]; DEFAULT_DB=ROOT/"data/trades.db"; AUDIT=ROOT/"data/learning/advisory_audit.jsonl"
NUMERIC=("dte_at_trade_date","booster_score","freshness","strength","time_score","rr_score","confluence_count","moneyness_at_signal","vix_at_entry","iv_entry","abs_delta_entry")
def _num(x): return float(x) if isinstance(x,(int,float)) and math.isfinite(x) else None
def load_signal(signal_id, db_path=DEFAULT_DB):
    with sqlite3.connect(Path(db_path).resolve().as_uri()+"?mode=ro",uri=True) as c:
        c.row_factory=sqlite3.Row; row=c.execute("select * from signals where id=?",(signal_id,)).fetchone()
        return dict(row) if row else None
def _knowledge(path):
    try: entries=KnowledgeStore(path).read_entries()
    except (OSError,ValueError): return [],[]
    accepted=[]; exploratory=[]
    for e in entries:
        if e.status.value=="REJECTED": continue
        if e.status.value=="VALIDATED": accepted.append(e)
        elif e.category=="pattern_validation": exploratory.append(e)
    return accepted,exploratory
def _trade_analogues(signal, db_path):
    try:
        with sqlite3.connect(Path(db_path).resolve().as_uri()+"?mode=ro",uri=True) as c:
            c.row_factory=sqlite3.Row; rows=c.execute("select * from signals where status='closed' and id != ?",(signal.get("id"),)).fetchall()
    except sqlite3.Error: return []
    scored=[]
    for row in rows:
        r=dict(row); score=0
        for key in ("zone_class","zone_type","timeframe"):
            if signal.get(key) and r.get(key)==signal.get(key): score+=3
        for key in ("booster_score","confluence_count"):
            a,b=_num(signal.get(key)),_num(r.get(key)); score += max(0,2-abs(a-b)) if a is not None and b is not None else 0
        if signal.get("options_symbol") and r.get("options_symbol"):
            if str(signal["options_symbol"])[-2:]==str(r["options_symbol"])[-2:]: score+=2
        scored.append((score,r))
    return [r for _,r in sorted(scored,key=lambda x:x[0],reverse=True)[:5]]
def advise(signal_id, *, db_path=DEFAULT_DB, knowledge_path=DEFAULT_PATH, audit_path=AUDIT):
    signal=load_signal(signal_id,db_path)
    if not signal: return {"decision":"INSUFFICIENT_DATA","confidence":0.0,"primary_reasons":["Signal not found"],"supporting_evidence":[],"contradictory_evidence":[],"similar_historical_trades":[],"missing_information":["signal context"],"risk_warnings":[],"knowledge_ids_used":[],"advisory_only":True}
    validated,exploratory=_knowledge(knowledge_path); analogues=_trade_analogues(signal,db_path)
    missing=[k for k in ("options_entry_price","options_symbol","vix_at_signal") if signal.get(k) is None]
    decision="REVIEW" if not validated else "REVIEW"; confidence=0.2 if validated else 0.0
    reasons=["Advisory only; no deterministic live rule inferred", "Historical evidence is descriptive, not causal"]
    if not validated: decision="INSUFFICIENT_DATA"; reasons.append("No VALIDATED knowledge available")
    report={"decision":decision,"confidence":confidence,"primary_reasons":reasons,"supporting_evidence":[{"id":e.id,"status":e.status.value} for e in validated],"contradictory_evidence":[],"similar_historical_trades":[{"id":r.get("id"),"date":r.get("date"),"zone_class":r.get("zone_class"),"result":r.get("result")} for r in analogues],"missing_information":missing,"risk_warnings":["Do not infer theta, IV crush, delta causation, or strike error"],"knowledge_ids_used":[e.id for e in validated+exploratory],"advisory_only":True,"advisory_version":"5.0-deterministic","timestamp":datetime.now(timezone.utc).isoformat()}
    try:
        Path(audit_path).parent.mkdir(parents=True,exist_ok=True)
        with Path(audit_path).open("a",encoding="utf-8") as f: f.write(json.dumps({"input":signal,"output":report,"knowledge_ids":report["knowledge_ids_used"],"historical_trade_ids":[r.get("id") for r in analogues],"timestamp":report["timestamp"],"advisory_version":report["advisory_version"]})+"\n")
    except Exception: pass
    return report

if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument("--signal-id",type=int,required=True); parser.add_argument("--json",action="store_true")
    args=parser.parse_args(); result=advise(args.signal_id)
    print(json.dumps(result,indent=2) if args.json else f"Decision: {result['decision']}\nConfidence: {result['confidence']}\nAdvisory only: {result['advisory_only']}\nReasons: {'; '.join(result['primary_reasons'])}\nSimilar trades: {len(result['similar_historical_trades'])}")
