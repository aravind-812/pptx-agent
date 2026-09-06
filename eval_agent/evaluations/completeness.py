"""Completeness eval — semi-deterministic: >80% of all_key_facts used in deck?"""
from eval_agent.evaluations.base import EvalResult, run_binary_judge

_THRESHOLD = 0.80

_SYSTEM = """You are a binary evaluation judge for AI-generated PowerPoint presentations.

Your task: determine if the PPTX deck used at least 80% of the key facts identified in the edit plan.

PASS if: most (≥80%) of the listed key facts appear somewhere in the PPTX text.
FAIL if: significant facts from the list are entirely absent from the PPTX — content was wasted.

Answer with ONLY a JSON object on one line:
{"passed": true/false, "reasoning": "one sentence with count of matched vs total", "confidence": 0.0-1.0}"""


def evaluate_completeness(
    state: dict,
    pptx_all_text: str,
    provider: str | None = None,
) -> EvalResult:
    edit_plan = state.get("edit_plan", {})
    strategic = edit_plan.get("strategic_context", {})
    all_key_facts = strategic.get("all_key_facts", [])

    if not all_key_facts:
        return EvalResult(
            eval_name="completeness",
            passed=True,
            reasoning="No all_key_facts in edit plan — skipping completeness check",
            confidence=0.5,
            details={"total_facts": 0},
        )

    lower_pptx = pptx_all_text.lower()

    matched = []
    missed = []
    for fact in all_key_facts:
        # Simple substring match (case-insensitive, first 60 chars of fact)
        snippet = fact.lower()[:60]
        # Try multi-word key phrase match: check if most words appear near each other
        words = [w for w in snippet.split() if len(w) > 3]
        if not words:
            matched.append(fact)
            continue
        hit_count = sum(1 for w in words if w in lower_pptx)
        if hit_count / len(words) >= 0.6:
            matched.append(fact)
        else:
            missed.append(fact)

    total = len(all_key_facts)
    coverage = len(matched) / total if total > 0 else 1.0

    # Deterministic result if coverage is clear
    if coverage >= _THRESHOLD:
        return EvalResult(
            eval_name="completeness",
            passed=True,
            reasoning=f"{len(matched)}/{total} key facts found in PPTX ({coverage:.0%})",
            confidence=0.85,
            details={"total": total, "matched": len(matched), "missed": missed[:5], "coverage": round(coverage, 3)},
        )

    if coverage < 0.5:
        return EvalResult(
            eval_name="completeness",
            passed=False,
            reasoning=f"Only {len(matched)}/{total} key facts found ({coverage:.0%}) — threshold 80%",
            confidence=0.85,
            details={"total": total, "matched": len(matched), "missed": missed[:5], "coverage": round(coverage, 3)},
        )

    # Borderline (50-80%): use LLM to verify
    facts_block = "\n".join(f"- {f}" for f in all_key_facts[:25])
    missed_block = "\n".join(f"- {f}" for f in missed[:15])
    user = (
        f"KEY FACTS from edit plan ({total} total):\n{facts_block}\n\n"
        f"POSSIBLY MISSED ({len(missed)} by string match):\n{missed_block}\n\n"
        f"PPTX SLIDE TEXT:\n{pptx_all_text[:3000]}\n\n"
        f"String match found {len(matched)}/{total} ({coverage:.0%}). "
        f"Verify: do the 'possibly missed' facts actually appear in the PPTX (maybe paraphrased)?\n"
        f"PASS if ≥80% of all facts appear (including paraphrased). FAIL if significant facts are absent.\n"
        f"Respond with JSON only: "
        f'{"{"}"passed": true/false, "reasoning": "one sentence with count", "confidence": 0.0-1.0{"}"}'
    )

    result = run_binary_judge("completeness", _SYSTEM, user, provider)
    result.details = {"total": total, "matched": len(matched), "missed": missed[:5], "coverage": round(coverage, 3)}
    return result
