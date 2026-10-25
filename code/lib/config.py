"""Repository paths shared by every stage. All paths resolve relative to the repo root."""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATASET_DIR = os.path.join(REPO_ROOT, "dataset")
CACHE_DIR = os.path.join(REPO_ROOT, "cache")

INDEX_PATH = os.path.join(CACHE_DIR, "index.db")
MEDIA_EXTRACTIONS_PATH = os.path.join(CACHE_DIR, "media_extractions.json")
INTENT_CACHE_PATH = os.path.join(CACHE_DIR, "intent_cache.json")
USAGE_LOG_PATH = os.path.join(CACHE_DIR, "usage_log.jsonl")
RUN_LOG_PATH = os.path.join(CACHE_DIR, "run_log.jsonl")
CASE_RESULTS_PATH = os.path.join(CACHE_DIR, "case_results.jsonl")

EVAL_REQUESTS = os.path.join(DATASET_DIR, "deletion_requests.csv")
SAMPLE_REQUESTS = os.path.join(DATASET_DIR, "sample_deletion_requests.csv")
SAMPLE_GOLD = os.path.join(DATASET_DIR, "sample_output.csv")

OUTPUT_PATH = os.path.join(REPO_ROOT, "output.csv")                       # submission file (full run only)
UNVERIFIED_OUTPUT_PATH = os.path.join(CACHE_DIR, "unverified_output.csv")  # full run that failed the gate
SAMPLE_PREDICTIONS_PATH = os.path.join(CACHE_DIR, "sample_predictions.csv")
PARTIAL_PREDICTIONS_PATH = os.path.join(CACHE_DIR, "partial_predictions.csv")
SAMPLE_EVAL_PATH = os.path.join(CACHE_DIR, "sample_evaluation.json")
JUDGE_CACHE_PATH = os.path.join(CACHE_DIR, "judge_cache.json")
JUDGE_RESULTS_PATH = os.path.join(CACHE_DIR, "judge_results.jsonl")
JUDGE_SUMMARY_PATH = os.path.join(CACHE_DIR, "judge_summary.json")
