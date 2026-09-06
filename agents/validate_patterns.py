import argparse
from agents.pattern_validator import run_validation
from agents.knowledge_store import DEFAULT_PATH

def main():
    parser=argparse.ArgumentParser(description="Offline completed-trade pattern validation")
    parser.add_argument("--dry-run", action="store_true", help="Do not write HYPOTHESIS entries")
    parser.add_argument("--classification", choices=["ZONE_CORRECT_OPTION_CORRECT","ZONE_CORRECT_OPTION_WRONG","ZONE_WRONG_OPTION_PROFIT","ZONE_WRONG_OPTION_LOSS"])
    parser.add_argument("--min-samples", type=int, default=3)
    parser.add_argument("--knowledge-path", default=DEFAULT_PATH)
    args=parser.parse_args()
    run_validation(args.knowledge_path,args.classification,args.min_samples,args.dry_run)
if __name__ == "__main__": main()
