import argparse, json
from agents.advisory_agent import advise
def main():
 p=argparse.ArgumentParser(); p.add_argument("--signal-id",type=int,required=True); p.add_argument("--json",action="store_true"); a=p.parse_args(); report=advise(a.signal_id)
 print(json.dumps(report,indent=2,default=str) if a.json else f"Decision: {report['decision']}\nConfidence: {report['confidence']}\nAdvisory only: {report['advisory_only']}\nReasons: {'; '.join(report['primary_reasons'])}\nSimilar trades: {len(report['similar_historical_trades'])}")
if __name__=="__main__": main()
