"""Faithfulness eval — LLM judge: key facts from transcript appear in PPTX?"""
from eval_agent.evaluations.base import EvalResult, run_binary_judge

_SYSTEM = """You are a strict binary evaluation judge for AI-generated PowerPoint presentations.

Your task: determine if the key facts from the source transcript appear in the output PPTX.

PASS if: all major facts, numbers, names, decisions, and claims from the transcript are represented somewhere in the PPTX.
FAIL if: significant facts from the transcript are completely absent from the PPTX.

Small omissions are acceptable. Missing multiple major points is a FAIL.

Answer with ONLY a JSON object on one line:
{"passed": true/false, "reasoning": "one sentence citing what's present or missing", "confidence": 0.0-1.0}"""


def evaluate_faithfulness(
    transcript: str,
    pptx_all_text: str,
    scenario: dict,
    provider: str | None = None,
) -> EvalResult:
    expected_facts = scenario.get("expected_facts", [])
    facts_block = (
        "\n".join(f"- {f}" for f in expected_facts[:20])
        if expected_facts
        else "(not pre-extracted — use transcript)"
    )

    user = (
        f"SOURCE TRANSCRIPT (first 3000 chars):\n{transcript[:3000]}\n\n"
        f"EXPECTED KEY FACTS (pre-extracted from transcript):\n{facts_block}\n\n"
        f"PPTX SLIDE TEXT (all slides combined):\n{pptx_all_text[:4000]}\n\n"
        f"QUESTION: Do the key facts from the transcript appear in the PPTX?\n"
        f"Respond with JSON only: "
        f'{"{"}"passed": true/false, "reasoning": "one sentence", "confidence": 0.0-1.0{"}"}'
    )

    return run_binary_judge("faithfulness", _SYSTEM, user, provider)
