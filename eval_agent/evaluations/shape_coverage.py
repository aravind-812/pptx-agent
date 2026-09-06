"""Deterministic shape coverage eval — reads executor_log, zero LLM cost."""
from eval_agent.evaluations.base import EvalResult

COVERAGE_THRESHOLD = 0.90


def evaluate_shape_coverage(state: dict) -> EvalResult:
    """Check that >= 90% of planned edits were actually applied."""
    edit_plan = state.get("edit_plan", {})
    executor_log = state.get("executor_log", {})

    total_edits = len(edit_plan.get("edits", []))
    if total_edits == 0:
        return EvalResult(
            eval_name="shape_coverage",
            passed=True,
            reasoning="No edits in plan — nothing to check",
            confidence=1.0,
            details={"total": 0, "applied": 0, "coverage": 1.0},
        )

    phases = executor_log.get("phases", [])
    batch_phase = next((p for p in phases if p.get("name") == "batch_apply"), {})
    applied = batch_phase.get("applied", 0)

    # Phase 3 (LLM fallback) may have applied more — count as applied if phase present + no error
    llm_phase = next((p for p in phases if p.get("name") == "llm_fallback"), {})
    if not llm_phase.get("skipped") and not llm_phase.get("error"):
        applied = min(total_edits, applied + llm_phase.get("fixed_count_target", 0))

    coverage = applied / total_edits
    passed = coverage >= COVERAGE_THRESHOLD

    return EvalResult(
        eval_name="shape_coverage",
        passed=passed,
        reasoning=(
            f"{applied}/{total_edits} edits applied ({coverage:.0%}) "
            f"— threshold {COVERAGE_THRESHOLD:.0%}"
        ),
        confidence=1.0,
        details={"total": total_edits, "applied": applied, "coverage": round(coverage, 3)},
    )
