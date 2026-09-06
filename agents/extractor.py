"""transcript-extractor: distils raw sales-call transcripts into structured facts for the planner.

Runs before the planner. Solves two problems:
  1. Large transcripts (>60K chars) exceed planner context — extractor summarises each chunk.
  2. Raw chatter (scheduling, smalltalk, logistics) pollutes the planner input — extractor
     filters to signal only: decisions, timeline, budget, modules, stakeholders, facts.
"""
import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from llm_factory import get_llm

_CHUNK_SIZE = 60_000  # chars per extraction chunk
_SHORT_TRANSCRIPT_CHARS = 30_000  # skip LLM below this — planner handles raw directly

SYSTEM_PROMPT = """You are a Sales Intelligence Extractor.

Input: a raw sales / implementation call transcript (one or many merged calls). It contains scheduling talk, smalltalk, logistics, and technical discussions. Your job is to extract ONLY the signal — facts that belong in a customer proposal or project status deck.

Output a single JSON object — no markdown wrapper, no commentary.

{
  "customer_profile": {
    "industry": "<industry if stated>",
    "size": "<company size / number of sites / user count if stated>",
    "current_state": "<what they have now, pain points, legacy systems>"
  },
  "project": {
    "name": "<project name or type, e.g. eQMS Implementation>",
    "scope": "<modules, sites, integrations, user count in scope>",
    "phases": ["<phase 1 description>", "<phase 2>"],
    "out_of_scope": ["<anything explicitly excluded>"]
  },
  "timeline": [
    {"milestone": "<event name>", "date": "<exact date or relative — verbatim from transcript>"}
  ],
  "budget": {
    "total": "<total amount if stated>",
    "remaining": "<remaining if stated>",
    "notes": "<caveats, burn rate, etc.>"
  },
  "stakeholders": [
    {"role": "<title / role>", "responsibility": "<what they own>"}
  ],
  "key_decisions": [
    "<decision made — verbatim or near-verbatim>"
  ],
  "modules_features": [
    "<specific module or capability discussed>"
  ],
  "pain_points": [
    "<specific problem the customer has — verbatim>"
  ],
  "success_criteria": [
    "<how customer defines success — verbatim>"
  ],
  "verbatim_highlights": [
    "<standout quote worth keeping in slides>"
  ],
  "all_facts": [
    "<every distinct fact, number, date, name, decision — one per entry>"
  ]
}

Rules:
- Extract ONLY what appears in the transcript. No fabrication. No inference.
- Preserve exact numbers, dates, percentages, proper nouns verbatim.
- all_facts must be exhaustive — every number, date, name, % in the text.
- Omit scheduling logistics, smalltalk, hold music, technical call issues.
- If a field has no data, use null or [].
- Output ONLY valid JSON."""


def _system_message(provider: str) -> SystemMessage:
    if provider == "anthropic":
        return SystemMessage(content=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }])
    return SystemMessage(content=SYSTEM_PROMPT)


def extractor_node(state: dict) -> dict:
    provider = state.get("provider", "anthropic")
    model = state.get("executor_model", "claude-haiku-4-5-20251001")

    raw = Path(state["transcript_path"]).read_text(encoding="utf-8")

    # Short transcript — planner can handle raw directly, skip LLM cost
    if len(raw) <= _SHORT_TRANSCRIPT_CHARS:
        return {**state, "extracted_facts": {}}

    llm = get_llm(provider, model, max_tokens=4096)
    chunks = _split_transcript(raw)
    sys_msg = _system_message(provider)

    extractions: list[dict] = []
    for chunk in chunks:
        try:
            resp = llm.invoke([
                sys_msg,
                HumanMessage(content=f"Extract structured facts:\n\n{chunk}"),
            ])
            content = resp.content
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                    if not (isinstance(b, dict) and b.get("type") == "thinking")
                )
            parsed = _parse_json(content)
            if parsed:
                extractions.append(parsed)
        except Exception:
            pass  # chunk failure → skip; planner falls back to raw transcript

    merged = _merge(extractions)
    return {**state, "extracted_facts": merged}


def _split_transcript(raw: str) -> list[str]:
    """Split on call boundaries if transcript is large."""
    if len(raw) <= _CHUNK_SIZE:
        return [raw]

    # Try to split on call separator lines (====...====)
    boundaries = [0]
    for m in re.finditer(r"={20,}", raw):
        pos = m.start()
        if pos - boundaries[-1] >= _CHUNK_SIZE:
            boundaries.append(pos)
    boundaries.append(len(raw))

    chunks = []
    for i in range(len(boundaries) - 1):
        seg = raw[boundaries[i]:boundaries[i + 1]].strip()
        if seg:
            chunks.append(seg)
    return chunks or [raw]


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _merge(extractions: list[dict]) -> dict:
    if not extractions:
        return {}
    if len(extractions) == 1:
        return extractions[0]

    merged = extractions[0].copy()
    LIST_KEYS = (
        "timeline", "stakeholders", "key_decisions", "modules_features",
        "pain_points", "success_criteria", "verbatim_highlights", "all_facts",
    )
    DICT_KEYS = ("customer_profile", "project", "budget")

    for ext in extractions[1:]:
        if not ext:
            continue
        for key in LIST_KEYS:
            existing = merged.get(key) or []
            seen = {json.dumps(x, sort_keys=True) for x in existing}
            for item in (ext.get(key) or []):
                k = json.dumps(item, sort_keys=True)
                if k not in seen:
                    existing.append(item)
                    seen.add(k)
            merged[key] = existing
        for key in DICT_KEYS:
            base = merged.get(key) or {}
            for subkey, val in (ext.get(key) or {}).items():
                if val and not base.get(subkey):
                    base[subkey] = val
            merged[key] = base

    return merged
