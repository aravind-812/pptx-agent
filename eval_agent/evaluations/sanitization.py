"""Sanitization eval — LLM judge: generic corporate filler the user never wrote?"""
from eval_agent.evaluations.base import EvalResult, run_binary_judge

_FORBIDDEN = [
    "industry-leading", "best-in-class", "world-class", "robust", "scalable",
    "cutting-edge", "synergy", "leverage", "seamless", "mission-critical",
    "next-generation", "innovative solution", "game-changing", "disruptive",
    "holistic approach", "value-added", "best practices", "thought leader",
]

_SYSTEM = """You are a strict binary evaluation judge for AI-generated PowerPoint presentations.

Your task: detect if the PPTX replaced the user's specific language with generic corporate filler.

PASS if: the PPTX uses the user's actual phrasing, specific numbers, and concrete details.
FAIL if: the PPTX contains generic marketing filler that the user never wrote — phrases like
  "industry-leading", "best-in-class", "world-class", "robust", "scalable", "cutting-edge",
  "synergy", "leverage", "seamless", "mission-critical", "next-generation"
  — especially when the source document had specific, concrete language for that topic.

Also FAIL if: the PPTX uses hedging the user didn't write ("may", "could", "potentially")
where the user's original was definitive ("will", "is", "saves").

Answer with ONLY a JSON object on one line:
{"passed": true/false, "reasoning": "one sentence citing the specific filler phrase found", "confidence": 0.0-1.0}"""


def evaluate_sanitization(
    transcript: str,
    pptx_all_text: str,
    provider: str | None = None,
) -> EvalResult:
    # Quick deterministic pre-check: flag obvious forbidden words
    lower_pptx = pptx_all_text.lower()
    found_forbidden = [w for w in _FORBIDDEN if w in lower_pptx]

    # If none found deterministically, still run LLM to catch subtler cases
    user = (
        f"SOURCE TRANSCRIPT (first 3000 chars):\n{transcript[:3000]}\n\n"
        f"PPTX SLIDE TEXT:\n{pptx_all_text[:3000]}\n\n"
        + (f"FLAGGED WORDS (found in PPTX): {found_forbidden}\n\n" if found_forbidden else "")
        + f"QUESTION: Did the PPTX replace user's specific language with generic corporate filler?\n"
        f"Respond with JSON only: "
        f'{"{"}"passed": true/false, "reasoning": "one sentence", "confidence": 0.0-1.0{"}"}'
    )

    result = run_binary_judge("sanitization", _SYSTEM, user, provider)

    if found_forbidden and result.passed:
        result.details["flagged_words"] = found_forbidden

    return result
