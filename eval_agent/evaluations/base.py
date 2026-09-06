"""EvalResult dataclass + binary LLM judge harness."""
import json
import re
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage

from llm_factory import default_provider, executor_model, get_llm

JUDGE_SYSTEM = """You are a strict binary quality judge for AI-generated PowerPoint presentations.
Answer with ONLY a JSON object on a single line. No markdown, no extra text.
Format: {"passed": true/false, "reasoning": "one sentence", "confidence": 0.0-1.0}"""


@dataclass
class EvalResult:
    eval_name: str
    passed: bool
    reasoning: str
    confidence: float = 0.5
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "eval_name": self.eval_name,
            "passed": self.passed,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "details": self.details,
        }


def run_binary_judge(
    eval_name: str,
    system_prompt: str,
    user_prompt: str,
    provider: str | None = None,
) -> EvalResult:
    """Call Haiku as binary pass/fail judge. Returns EvalResult."""
    provider = provider or default_provider()
    model = executor_model(provider)
    llm = get_llm(provider, model, max_tokens=256)

    try:
        resp = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
        content = resp.content
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )

        m = re.search(r'\{[^{}]+\}', content, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                return EvalResult(
                    eval_name=eval_name,
                    passed=bool(data.get("passed", False)),
                    reasoning=str(data.get("reasoning", ""))[:400],
                    confidence=float(data.get("confidence", 0.5)),
                )
            except json.JSONDecodeError:
                pass

        # Fallback: keyword scan
        lower = content.lower()
        passed = '"passed": true' in lower or (
            "passed" in lower and "true" in lower and "false" not in lower
        )
        return EvalResult(
            eval_name=eval_name,
            passed=passed,
            reasoning=content.strip()[:300],
            confidence=0.3,
        )

    except Exception as exc:
        return EvalResult(
            eval_name=eval_name,
            passed=False,
            reasoning=f"Judge error: {exc}",
            confidence=0.0,
            details={"error": str(exc)},
        )
