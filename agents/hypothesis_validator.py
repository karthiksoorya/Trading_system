"""Chronological, leakage-safe validation of Phase 4B predictor hypotheses."""
from datetime import date
from statistics import mean
from agents.pattern_validator import GROUPS, PRE_TRADE, load_observations, _value

def validate(path, from_date=None, to_date=None, train_ratio=.7, min_development=3, min_holdout=3, min_subgroup=3):
    rows=load_observations(path)
    rows=[r for r in rows if (from_date is None or r[1].get("source_trade",{}).get("date", "") >= from_date) and (to_date is None or r[1].get("source_trade",{}).get("date", "") <= to_date)]
    rows.sort(key=lambda r:(r[1].get("source_trade",{}).get("date", ""),r[1].get("source_trade",{}).get("time_signal", ""),r[1].get("source_trade_id")))
    split=max(1,min(len(rows)-1,int(len(rows)*train_ratio))) if len(rows)>1 else 0
    dev,hold=rows[:split],rows[split:]
    metrics=[]
    for metric in PRE_TRADE:
        def effect(part):
            a=[_value(v,metric) for _,v in part if v.get("classification")==GROUPS[0] and isinstance(_value(v,metric),(int,float))]
            b=[_value(v,metric) for _,v in part if v.get("classification")==GROUPS[1] and isinstance(_value(v,metric),(int,float))]
            return (mean(b)-mean(a),len(a),len(b)) if a and b else (None,len(a),len(b))
        de,da,db=effect(dev); he,ha,hb=effect(hold)
        status="INSUFFICIENT_EVIDENCE" if da<min_development or db<min_development or ha<min_holdout or hb<min_holdout else ("VALIDATION_CANDIDATE" if de and he and de*he>0 else "REJECTED")
        metrics.append({"hypothesis_id":f"PATTERN-HYPOTHESIS-{metric}","metric":metric,"development_sample_count":da+db,"holdout_sample_count":ha+hb,"development_group_counts":{"successful":da,"wrong":db},"holdout_group_counts":{"successful":ha,"wrong":hb},"development_effect":de,"holdout_effect":he,"effect_direction_persists":bool(de and he and de*he>0),"status":status,"field_category":"PRE_TRADE","post_trade_predictor":metric not in PRE_TRADE})
    return {"from_date":from_date,"to_date":to_date,"train_count":len(dev),"holdout_count":len(hold),"hypotheses_tested":metrics}
