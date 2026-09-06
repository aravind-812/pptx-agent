"""Batch benchmark runner: runs all evals across N transcripts → pass rate table + JSONL."""
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from eval_agent.evaluations.completeness import evaluate_completeness
from eval_agent.evaluations.faithfulness import evaluate_faithfulness
from eval_agent.evaluations.hallucination import evaluate_hallucination
from eval_agent.evaluations.placeholder import evaluate_placeholder
from eval_agent.evaluations.sanitization import evaluate_sanitization
from eval_agent.evaluations.shape_coverage import evaluate_shape_coverage
from eval_agent.runner import run_eval_case
from eval_agent.scenarios import generate_scenario
from llm_factory import default_provider

_RESULTS_DIR = Path(__file__).parent / "results"
_RESULTS_FILE = _RESULTS_DIR / "benchmark.jsonl"

EVAL_NAMES = [
    "placeholder", "shape_coverage",
    "faithfulness", "hallucination", "sanitization", "completeness",
]


def run_all_evals(
    run_result: dict,
    transcript_path: str,
    scenario: dict,
    provider: str,
) -> dict[str, dict]:
    """Run all 6 evals for one transcript run. Returns {eval_name: EvalResult.to_dict()}."""
    state = run_result["state"]
    pptx_text = run_result["pptx_text"]
    pptx_all_text = pptx_text.get("all_text", "")
    output_path = run_result.get("output_path", "")
    transcript = Path(transcript_path).read_text(encoding="utf-8", errors="replace")

    results: dict[str, dict] = {}

    # Deterministic — free
    results["placeholder"] = evaluate_placeholder(output_path or "").to_dict()
    results["shape_coverage"] = evaluate_shape_coverage(state).to_dict()

    # Skip LLM evals if pipeline itself errored (no PPTX output)
    if run_result.get("error") or not pptx_all_text:
        for name in ["faithfulness", "hallucination", "sanitization", "completeness"]:
            results[name] = {
                "eval_name": name, "passed": False,
                "reasoning": f"No PPTX output — pipeline error: {run_result.get('error', 'unknown')}",
                "confidence": 1.0, "details": {},
            }
        return results

    # LLM evals
    results["faithfulness"] = evaluate_faithfulness(
        transcript, pptx_all_text, scenario, provider
    ).to_dict()
    results["hallucination"] = evaluate_hallucination(
        transcript, pptx_all_text, provider
    ).to_dict()
    results["sanitization"] = evaluate_sanitization(
        transcript, pptx_all_text, provider
    ).to_dict()
    results["completeness"] = evaluate_completeness(
        state, pptx_all_text, provider
    ).to_dict()

    return results


def run_benchmark(
    template_path: str,
    transcript_paths: list[str],
    provider: str,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """
    Run benchmark across all transcripts.

    on_progress called with dicts:
      {"type": "eval_start", "total": N}
      {"type": "eval_transcript_start", "id": "...", "index": i, "total": N}
      {"type": "eval_transcript_done", "id": "...", "results": {...}, "error": None}
      {"type": "eval_done", "pass_rates": {...}, "run_id": "..."}

    Returns: {run_id, pass_rates, details, timestamp}
    """
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())[:8]
    total = len(transcript_paths)
    run_dir = tempfile.mkdtemp(prefix=f"eval_bench_{run_id}_")

    if on_progress:
        on_progress({"type": "eval_start", "total": total, "run_id": run_id})

    details: list[dict] = []
    pass_counts: dict[str, int] = {name: 0 for name in EVAL_NAMES}

    try:
        for i, transcript_path in enumerate(transcript_paths, 1):
            transcript_id = Path(transcript_path).stem

            if on_progress:
                on_progress({
                    "type": "eval_transcript_start",
                    "id": transcript_id,
                    "index": i,
                    "total": total,
                })

            # Load or generate scenario (cached)
            try:
                scenario = generate_scenario(transcript_path, provider)
            except Exception:
                scenario = {}

            # Run pipeline
            run_result = run_eval_case(transcript_path, template_path, provider, run_dir)

            # Run all evals
            try:
                eval_results = run_all_evals(run_result, transcript_path, scenario, provider)
            except Exception as exc:
                eval_results = {
                    name: {"eval_name": name, "passed": False,
                           "reasoning": f"Eval error: {exc}", "confidence": 0.0, "details": {}}
                    for name in EVAL_NAMES
                }

            for name in EVAL_NAMES:
                if eval_results.get(name, {}).get("passed"):
                    pass_counts[name] += 1

            record = {
                "transcript_id": transcript_id,
                "evals": eval_results,
                "duration_sec": run_result.get("duration_sec"),
                "error": run_result.get("error"),
                "review_verdict": run_result["state"].get("review_verdict", ""),
            }
            details.append(record)

            if on_progress:
                on_progress({
                    "type": "eval_transcript_done",
                    "id": transcript_id,
                    "index": i,
                    "total": total,
                    "results": eval_results,
                    "error": run_result.get("error"),
                    "review_verdict": run_result["state"].get("review_verdict", ""),
                })

    finally:
        shutil.rmtree(run_dir, ignore_errors=True)

    pass_rates = {
        name: round(pass_counts[name] / total, 3) if total else 0.0
        for name in EVAL_NAMES
    }

    benchmark_record = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "template_path": template_path,
        "transcript_count": total,
        "pass_rates": pass_rates,
        "details": details,
    }

    with open(_RESULTS_FILE, "a") as f:
        f.write(json.dumps(benchmark_record) + "\n")

    if on_progress:
        on_progress({"type": "eval_done", "pass_rates": pass_rates, "run_id": run_id})

    return benchmark_record


def load_latest_results() -> Optional[dict]:
    """Load the most recent benchmark run from JSONL."""
    if not _RESULTS_FILE.exists():
        return None
    lines = _RESULTS_FILE.read_text().strip().splitlines()
    if not lines:
        return None
    return json.loads(lines[-1])


def load_all_results() -> list[dict]:
    """Load all benchmark runs from JSONL."""
    if not _RESULTS_FILE.exists():
        return []
    return [json.loads(line) for line in _RESULTS_FILE.read_text().strip().splitlines() if line]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python -m eval_agent.benchmark <template.pptx> <transcript_dir> [transcript_ids...]")
        sys.exit(1)

    template = sys.argv[1]
    tx_dir = Path(sys.argv[2])
    ids = sys.argv[3:] if len(sys.argv) > 3 else None

    if ids:
        paths = [str(tx_dir / f"{tid}_transcript.txt") for tid in ids]
        paths = [p for p in paths if Path(p).exists()]
    else:
        paths = sorted(str(p) for p in tx_dir.glob("*.txt"))

    print(f"Running benchmark: {len(paths)} transcripts, template={template}")

    def progress(ev: dict):
        t = ev.get("type")
        if t == "eval_transcript_done":
            rates = {k: "✓" if v.get("passed") else "✗" for k, v in ev.get("results", {}).items()}
            print(f"  [{ev['index']}/{ev['total']}] {ev['id']}: {rates}")
        elif t == "eval_done":
            print(f"\nPass rates: {ev['pass_rates']}")

    result = run_benchmark(template, paths, default_provider(), progress)
    print(f"\nRun ID: {result['run_id']}")
    print(f"Results saved to eval_agent/results/benchmark.jsonl")
