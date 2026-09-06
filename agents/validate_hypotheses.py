import argparse, json
from agents.hypothesis_validator import validate
from agents.knowledge_store import DEFAULT_PATH

def main():
 p=argparse.ArgumentParser(description="Chronological offline hypothesis validation")
 p.add_argument("--dry-run",action="store_true"); p.add_argument("--from-date"); p.add_argument("--to-date"); p.add_argument("--train-ratio",type=float,default=.7); p.add_argument("--min-samples",type=int,default=3); p.add_argument("--knowledge-path",default=DEFAULT_PATH); a=p.parse_args()
 if (a.from_date is None)!=(a.to_date is None) or (a.from_date and a.from_date>a.to_date): p.error("valid from/to dates required together and in order")
 result=validate(a.knowledge_path,a.from_date,a.to_date,a.train_ratio,a.min_samples,a.min_samples)
 tested=result["hypotheses_tested"]
 print(json.dumps({"hypotheses_tested":len(tested),"surviving_candidates":[x for x in tested if x["status"]=="VALIDATION_CANDIDATE"],"rejected_hypotheses":[x for x in tested if x["status"]=="REJECTED"],"insufficient_evidence":[x for x in tested if x["status"]=="INSUFFICIENT_EVIDENCE"],"train_sample_count":result["train_count"],"holdout_sample_count":result["holdout_count"]},indent=2))
if __name__=="__main__": main()
