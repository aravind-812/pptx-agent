"""One-time scenario generator: extracts ground-truth fixtures from transcripts."""
import json
from pathlib import Path
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from llm_factory import default_provider, executor_model, get_llm

_SCENARIOS_DIR = Path(__file__).parent / "scenarios"

_SYSTEM = """You are a ground-truth extractor for an evaluation system.
Given a transcript, extract structured facts that will be used to evaluate AI-generated PowerPoints.

Return ONLY a JSON object (no markdown):
{
  "expected_facts": ["specific fact or claim from transcript", ...],
  "critical_numbers": ["12%", "$2.4M", "Q3 2025", ...],
  "forbidden_phrases": ["phrase that would indicate generic filler replacing user's specifics"],
  "key_names": ["Person Name", "Company Name", "Product Name", ...],
  "tone": ["confident", "technical", "urgent", ...],
  "expected_topics": ["topic the deck should cover", ...]
}

Rules:
- expected_facts: 5-15 most important, specific facts from transcript (verbatim or near-verbatim)
- critical_numbers: every number, %, $, date that must appear in the deck
- forbidden_phrases: generic phrases a LLM might substitute for the user's specific language
- key_names: every proper noun in the transcript
- tone: 2-4 adjectives describing voice/style
- expected_topics: main topics the deck should address"""


def generate_scenario(
    transcript_path: str,
    provider: Optional[str] = None,
    force: bool = False,
) -> dict:
    """
    Extract ground-truth fixtures for one transcript. Cached to disk.
    Returns the scenario dict.
    """
    provider = provider or default_provider()
    transcript_id = Path(transcript_path).stem
    cache_path = _SCENARIOS_DIR / f"{transcript_id}.json"

    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text())

    transcript = Path(transcript_path).read_text(encoding="utf-8")
    model = executor_model(provider)
    llm = get_llm(provider, model, max_tokens=1024)

    excerpt = transcript[:6000]

    try:
        resp = llm.invoke([
            SystemMessage(content=_SYSTEM),
            HumanMessage(content=f"TRANSCRIPT:\n{excerpt}\n\nExtract the JSON scenario object:"),
        ])
        content = resp.content
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))

        import re
        m = re.search(r'\{[\s\S]+\}', content)
        if m:
            scenario = json.loads(m.group(0))
        else:
            scenario = json.loads(content.strip())

    except Exception as exc:
        scenario = {
            "expected_facts": [],
            "critical_numbers": [],
            "forbidden_phrases": [],
            "key_names": [],
            "tone": [],
            "expected_topics": [],
            "error": str(exc),
        }

    scenario["transcript_id"] = transcript_id
    _SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(scenario, indent=2))
    return scenario


def generate_all_scenarios(
    transcript_dir: str,
    provider: Optional[str] = None,
    force: bool = False,
) -> dict[str, dict]:
    """Generate scenarios for all transcripts in a directory. Returns id→scenario map."""
    results: dict[str, dict] = {}
    paths = sorted(Path(transcript_dir).glob("*.txt"))
    for p in paths:
        print(f"  scenario: {p.stem} ...", end=" ", flush=True)
        scenario = generate_scenario(str(p), provider, force)
        results[p.stem] = scenario
        cached = "(cached)" if not scenario.get("error") else "(error)"
        print(cached)
    return results


if __name__ == "__main__":
    import sys
    transcript_dir = sys.argv[1] if len(sys.argv) > 1 else "eval_transcripts"
    print(f"Generating scenarios from {transcript_dir} ...")
    scenarios = generate_all_scenarios(transcript_dir)
    print(f"Done. {len(scenarios)} scenarios written to eval_agent/scenarios/")
