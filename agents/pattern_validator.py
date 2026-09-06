"""Offline pattern validation with predictor/outcome separation."""
from collections import defaultdict
from statistics import mean, median
import json, sys
from agents.knowledge_store import KnowledgeStore, DEFAULT_PATH
from agents.learning_models import KnowledgeEntry, KnowledgeStatus

GROUPS=("ZONE_CORRECT_OPTION_CORRECT","ZONE_CORRECT_OPTION_WRONG","ZONE_WRONG_OPTION_PROFIT","ZONE_WRONG_OPTION_LOSS")
PRE_TRADE=("booster_score","freshness","strength","time_score","rr_score","departure_strength","base_compression","confluence_count","dte_at_trade_date","vix_at_entry","iv_entry","gamma_entry","theta_entry","vega_entry","distance_from_strike","signal_to_fill_seconds","abs_delta_entry")
POST_TRADE=("zone_pnl_points","option_premium_change","option_premium_change_pct","holding_minutes")
CATEGORICAL=("zone_type","zone_class","timeframe","option_type","moneyness_at_signal")
def _ev(e):
    try:
        v=json.loads(e.evidence[-1]); return v if isinstance(v,dict) else None
    except (ValueError,TypeError,IndexError): return None
def load_observations(path=DEFAULT_PATH):
    try: entries=KnowledgeStore(path).read_entries()
    except (OSError,ValueError): return []
    latest={}
    for e in entries:
        v=_ev(e)
        if e.category=="completed_trade_reflection" and v and isinstance(v.get("source_trade_id"),int) and v.get("classification") in GROUPS: latest[v["source_trade_id"]]=(e,v)
    return list(latest.values())
def _value(v,m):
    x=v.get(m,v.get("source_trade",{}).get(m))
    if m=="abs_delta_entry" and x is None: x=v.get("delta_entry")
    return abs(x) if m=="abs_delta_entry" and isinstance(x,(int,float)) else x
def summarize(path=DEFAULT_PATH,classification=None,min_samples=3):
    rows=load_observations(path); groups=defaultdict(list)
    for r in rows: groups[r[1]["classification"]].append(r)
    selected=[classification] if classification else GROUPS; result={"groups":{},"comparison":{"status":"INSUFFICIENT_EVIDENCE"},"data_quality":{"total_reflected_trades":len(rows),"usable_trades_per_classification":{},"missing_option_entry_exit":0,"missing_vix":0,"missing_iv_greeks":0,"proxy_greeks":0,"exact_fill_greeks":0}}
    for g in selected:
        mem=groups[g]; st={"sample_count":len(mem),"evidence_ids":[e.id for e,_ in mem],"numeric":{},"categorical":{}}
        for m in PRE_TRADE+POST_TRADE:
            vals=[float(x) for _,v in mem if isinstance((x:=_value(v,m)),(int,float)) and not isinstance(x,bool)]
            if vals: st["numeric"][m]={"count":len(vals),"mean":round(mean(vals),4),"median":round(median(vals),4),"min":round(min(vals),4),"max":round(max(vals),4)}
        for m in CATEGORICAL:
            vals=[v.get("source_trade",{}).get(m,v.get(m)) for _,v in mem]; vals=[x for x in vals if x is not None]
            if vals: st["categorical"][m]={str(x):vals.count(x) for x in sorted(set(vals),key=str)}
        result["groups"][g]=st; result["data_quality"]["usable_trades_per_classification"][g]=len(mem)
        for _,v in mem:
            result["data_quality"]["missing_option_entry_exit"] += v.get("option_entry_price") is None or v.get("option_exit_price") is None
            result["data_quality"]["missing_vix"] += v.get("vix_at_entry") is None
            if v.get("iv_entry") is None: result["data_quality"]["missing_iv_greeks"]+=1
            elif v.get("greeks_basis_entry")=="signal_time_proxy_model": result["data_quality"]["proxy_greeks"]+=1
            elif v.get("greeks_basis_entry"): result["data_quality"]["exact_fill_greeks"]+=1
    a,b=result["groups"].get(GROUPS[0],{}),result["groups"].get(GROUPS[1],{})
    if a.get("sample_count",0)>=min_samples and b.get("sample_count",0)>=min_samples:
        metrics={}
        for m in PRE_TRADE:
            av=[_value(v,m) for _,v in groups[GROUPS[0]] if isinstance(_value(v,m),(int,float))]; bv=[_value(v,m) for _,v in groups[GROUPS[1]] if isinstance(_value(v,m),(int,float))]
            if av and bv:
                am,bm=mean(av),mean(bv); metrics[m]={"field_category":"PRE_TRADE","successful_mean":round(am,4),"successful_median":round(median(av),4),"wrong_mean":round(bm,4),"wrong_median":round(median(bv),4),"absolute_difference":round(abs(bm-am),4),"difference_wrong_minus_success":round(bm-am,4),"normalized_effect_size":round((bm-am)/max(abs(am),abs(bm),1e-9),4)}
        result["comparison"]={"status":"OK","sample_counts":{GROUPS[0]:a["sample_count"],GROUPS[1]:b["sample_count"]},"metrics":metrics}
    return result
def hypotheses(summary,min_samples=3):
    c=summary.get("comparison",{});
    if c.get("status")!="OK": return []
    return [{"metric":m,"compared_groups":list(c["sample_counts"]),"sample_counts":c["sample_counts"],"observed_values":v,"effect":v["difference_wrong_minus_success"],"field_category":"PRE_TRADE","status":"HYPOTHESIS","live_use_allowed":False,"statement":f"HYPOTHESIS: ZONE_CORRECT_OPTION_WRONG trades appear to differ in {m} from successful option trades."} for m,v in c["metrics"].items() if v["difference_wrong_minus_success"]!=0]
def run_validation(path=DEFAULT_PATH,classification=None,min_samples=3,dry_run=True,output=None):
    out=output or sys.stdout; s=summarize(path,classification,min_samples); print("Offline pattern validation",file=out)
    for g,v in s["groups"].items(): print(f"{g}: samples={v['sample_count']}",file=out)
    print("Data quality: "+json.dumps(s["data_quality"], sort_keys=True), file=out)
    print(f"Comparison: {s['comparison']['status']}",file=out); cs=hypotheses(s,min_samples)
    for c in cs: print(c["statement"]+f" (effect={c['effect']})",file=out)
    if not dry_run:
        store=KnowledgeStore(path); existing={e.id for e in store.read_entries()}
        for c in cs:
            ident="PATTERN-HYPOTHESIS-"+c["metric"]
            if ident not in existing: store.append(KnowledgeEntry(id=ident,category="pattern_validation",title=c["statement"],source="offline pattern validator",learning=c["statement"],status=KnowledgeStatus.HYPOTHESIS,evidence=(json.dumps(c),),sample_count=min(s["comparison"]["sample_counts"].values())))
    return s,cs
