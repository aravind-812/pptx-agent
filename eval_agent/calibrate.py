"""Calibration: compare LLM judge verdicts vs human labels → F1 per eval."""
import json
from pathlib import Path
from typing import Optional

from eval_agent.benchmark import EVAL_NAMES, load_all_results


def calibrate(
    eval_name: str,
    human_labels: list[dict],
) -> dict:
    """
    Compare stored LLM judge verdicts against human-provided labels.

    Args:
        eval_name:     one of EVAL_NAMES
        human_labels:  [{"transcript_id": "...", "passed": True/False}, ...]

    Returns:
        {eval_name, precision, recall, f1, tp, fp, fn, tn, details, trusted}
    """
    if eval_name not in EVAL_NAMES:
        return {"error": f"Unknown eval: {eval_name}. Valid: {EVAL_NAMES}"}

    # Load latest stored results to get LLM verdicts
    all_results = load_all_results()
    if not all_results:
        return {"error": "No benchmark results found. Run benchmark first."}

    latest = all_results[-1]
    llm_verdicts: dict[str, bool] = {}
    for detail in latest.get("details", []):
        tid = detail["transcript_id"]
        eval_result = detail.get("evals", {}).get(eval_name, {})
        if "passed" in eval_result:
            llm_verdicts[tid] = bool(eval_result["passed"])

    tp = fp = fn = tn = 0
    comparison_details: list[dict] = []

    for label in human_labels:
        tid = label["transcript_id"]
        human_pass = bool(label["passed"])
        llm_pass = llm_verdicts.get(tid)

        if llm_pass is None:
            continue

        if human_pass and llm_pass:
            tp += 1; outcome = "TP"
        elif not human_pass and llm_pass:
            fp += 1; outcome = "FP"
        elif human_pass and not llm_pass:
            fn += 1; outcome = "FN"
        else:
            tn += 1; outcome = "TN"

        comparison_details.append({
            "transcript_id": tid,
            "human": human_pass,
            "llm": llm_pass,
            "outcome": outcome,
            "llm_reasoning": latest.get("details", [{}])[0].get(
                "evals", {}).get(eval_name, {}).get("reasoning", ""),
        })

    total = tp + fp + fn + tn
    if total == 0:
        return {"error": "No matching transcript IDs between human labels and benchmark results"}

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    trusted = f1 >= 0.85

    return {
        "eval_name": eval_name,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "total_labeled": total,
        "trusted": trusted,
        "recommendation": (
            "Judge is reliable (F1 ≥ 0.85)" if trusted
            else "Consider upgrading this eval to Sonnet (F1 < 0.85)"
        ),
        "details": comparison_details,
    }


def save_human_labels(eval_name: str, labels: list[dict]) -> Path:
    """Save human labels to disk for reference."""
    labels_dir = Path(__file__).parent / "calibration_labels"
    labels_dir.mkdir(parents=True, exist_ok=True)
    path = labels_dir / f"{eval_name}_labels.json"
    path.write_text(json.dumps(labels, indent=2))
    return path
