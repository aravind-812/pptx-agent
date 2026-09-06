"""Hallucination eval — LLM judge: PPTX contains numbers/names NOT in transcript?"""
from eval_agent.evaluations.base import EvalResult, run_binary_judge

_SYSTEM = """You are a strict binary evaluation judge for AI-generated PowerPoint presentations.

Your task: detect if the PPTX contains fabricated numbers, names, or claims not found in the transcript.

PASS if: every specific number, percentage, dollar amount, person name, company name, date, and quote in the PPTX can be traced back to the transcript.
FAIL if: the PPTX contains ANY number, name, date, or statistic that does NOT appear in the transcript (fabrication).

Focus on: percentages, dollar amounts, growth figures, year numbers, proper nouns.
Ignore: generic words, templates phrases, slide titles that describe the slide.

Answer with ONLY a JSON object on one line:
{"passed": true/false, "reasoning": "one sentence citing the specific fabricated item if found", "confidence": 0.0-1.0}"""


def evaluate_hallucination(
    transcript: str,
    pptx_all_text: str,
    provider: str | None = None,
) -> EvalResult:
    user = (
        f"SOURCE TRANSCRIPT (first 4000 chars):\n{transcript[:4000]}\n\n"
        f"PPTX SLIDE TEXT (all slides combined):\n{pptx_all_text[:3000]}\n\n"
        f"QUESTION: Does the PPTX contain any number, name, date, or statistic NOT found in the transcript?\n"
        f"Respond with JSON only: "
        f'{"{"}"passed": true/false, "reasoning": "one sentence", "confidence": 0.0-1.0{"}"}'
    )

    return run_binary_judge("hallucination", _SYSTEM, user, provider)
