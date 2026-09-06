"""Deterministic placeholder eval — uses op_validate(), zero LLM cost."""
import sys
from eval_agent.evaluations.base import EvalResult
from tools.pptx_editor import op_validate


def evaluate_placeholder(pptx_path: str) -> EvalResult:
    """Check if any [X]/TODO/Enter placeholders remain in the output PPTX."""
    if not pptx_path:
        return EvalResult(
            eval_name="placeholder",
            passed=False,
            reasoning="No output PPTX path provided",
            confidence=1.0,
        )

    result = op_validate(pptx_path)

    if result.get("error"):
        return EvalResult(
            eval_name="placeholder",
            passed=False,
            reasoning=f"Validation error: {result['error']}",
            confidence=0.0,
        )

    hits = result.get("unfilled_placeholders", [])
    passed = result.get("ok", False)

    if passed:
        reasoning = f"No unfilled placeholders found across {result.get('slide_count', '?')} slides"
    else:
        reasoning = f"{len(hits)} unfilled placeholder(s): {'; '.join(hits[:3])}"

    return EvalResult(
        eval_name="placeholder",
        passed=passed,
        reasoning=reasoning,
        confidence=1.0,
        details={"hits": hits, "slide_count": result.get("slide_count")},
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m eval_agent.evaluations.placeholder <pptx_path>")
        sys.exit(1)
    r = evaluate_placeholder(sys.argv[1])
    print(f"{'PASS' if r.passed else 'FAIL'}: {r.reasoning}")
