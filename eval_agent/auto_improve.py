"""Auto-improve: cluster failures → Sonnet suggests prompt patch → human review."""
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from eval_agent.benchmark import EVAL_NAMES, load_latest_results
from llm_factory import default_provider, get_llm, planner_model

_PATCHES_DIR = Path(__file__).parent / "staged_patches"
_PLANNER_PATH = Path(__file__).parent.parent / "agents" / "planner.py"

_IMPROVE_SYSTEM = """You are a prompt engineer for an AI-powered PowerPoint editing system.
You will be shown failures from a quality evaluation, the current system prompt, and asked to suggest a targeted improvement.

Return a JSON object (no markdown):
{
  "patch_text": "The exact text to ADD to the system prompt (a new rule, example, or constraint)",
  "insert_after": "The exact line in the current prompt AFTER which to insert the patch (quote it exactly)",
  "reasoning": "Why this specific patch addresses the failure pattern",
  "affected_pattern": "The failure pattern this patch targets",
  "estimated_impact": "Which transcripts this would likely fix"
}

Rules:
- patch_text must be a concrete, specific instruction, not vague advice
- patch_text should be 1-3 sentences maximum
- Do NOT suggest removing existing text — only ADD
- Focus on the most common failure pattern across all failed cases
- The patch must address WHY cases fail, not just repeat that they should pass"""


def cluster_failures(eval_name: str, results: Optional[dict] = None) -> list[dict]:
    """Extract all failed cases for an eval from latest benchmark results."""
    if results is None:
        results = load_latest_results()
    if not results:
        return []

    failures = []
    for detail in results.get("details", []):
        eval_result = detail.get("evals", {}).get(eval_name, {})
        if not eval_result.get("passed", True):
            failures.append({
                "transcript_id": detail["transcript_id"],
                "reasoning": eval_result.get("reasoning", ""),
                "confidence": eval_result.get("confidence", 0.0),
                "review_verdict": detail.get("review_verdict", ""),
                "error": detail.get("error"),
            })

    return failures


def _read_current_prompt() -> str:
    """Read SYSTEM_PROMPT from agents/planner.py."""
    text = _PLANNER_PATH.read_text(encoding="utf-8")
    import re
    m = re.search(r'SYSTEM_PROMPT\s*=\s*"""([\s\S]+?)"""', text)
    return m.group(1) if m else text


def auto_improve(
    eval_name: str,
    provider: Optional[str] = None,
    results: Optional[dict] = None,
) -> dict:
    """
    Cluster failures for eval_name, ask Sonnet for a targeted prompt patch.
    NEVER auto-applies — writes patch to staged_patches/ for human review.

    Returns: {patch_text, insert_after, reasoning, affected_cases, patch_id, patch_path}
    """
    if eval_name not in EVAL_NAMES:
        return {"error": f"Unknown eval: {eval_name}"}

    provider = provider or default_provider()
    failures = cluster_failures(eval_name, results)

    if not failures:
        return {
            "error": f"No failures found for eval '{eval_name}' in latest benchmark run",
            "eval_name": eval_name,
            "affected_cases": 0,
        }

    current_prompt = _read_current_prompt()
    failures_text = json.dumps(failures[:20], indent=2)

    user = (
        f"EVAL TYPE: {eval_name}\n"
        f"FAILED CASES ({len(failures)} total, showing up to 20):\n{failures_text}\n\n"
        f"CURRENT PLANNER SYSTEM PROMPT:\n{current_prompt[:8000]}\n\n"
        f"Suggest a targeted patch to add to the system prompt to fix this failure pattern.\n"
        f"Return JSON only."
    )

    model = planner_model(provider)
    llm = get_llm(provider, model, max_tokens=1024)

    try:
        resp = llm.invoke([
            SystemMessage(content=_IMPROVE_SYSTEM),
            HumanMessage(content=user),
        ])
        content = resp.content
        if isinstance(content, list):
            content = "".join(b.get("text", "") for b in content if isinstance(b, dict))

        import re
        m = re.search(r'\{[\s\S]+\}', content)
        suggestion = json.loads(m.group(0)) if m else {"patch_text": content[:500]}

    except Exception as exc:
        return {"error": f"Sonnet call failed: {exc}", "eval_name": eval_name}

    patch_id = str(uuid.uuid4())[:8]
    patch_record = {
        "patch_id": patch_id,
        "eval_name": eval_name,
        "affected_cases": len(failures),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "suggestion": suggestion,
        "failures_sample": failures[:5],
    }

    _PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    patch_path = _PATCHES_DIR / f"{patch_id}_{eval_name}.json"
    patch_path.write_text(json.dumps(patch_record, indent=2))

    return {
        "patch_id": patch_id,
        "eval_name": eval_name,
        "affected_cases": len(failures),
        "patch_text": suggestion.get("patch_text", ""),
        "insert_after": suggestion.get("insert_after", ""),
        "reasoning": suggestion.get("reasoning", ""),
        "affected_pattern": suggestion.get("affected_pattern", ""),
        "patch_path": str(patch_path),
    }


def apply_patch(patch_id: str) -> dict:
    """
    Apply an approved patch to agents/planner.py SYSTEM_PROMPT.
    Creates a backup first. NEVER called automatically — human triggers this.

    Returns: {applied, backup_path, error}
    """
    patch_file = next((_PATCHES_DIR.glob(f"{patch_id}_*.json")), None)
    if not patch_file:
        return {"applied": False, "error": f"Patch {patch_id} not found"}

    patch_record = json.loads(patch_file.read_text())
    suggestion = patch_record.get("suggestion", {})
    patch_text = suggestion.get("patch_text", "")
    insert_after = suggestion.get("insert_after", "")

    if not patch_text:
        return {"applied": False, "error": "Empty patch_text in patch record"}

    # Backup planner.py first
    backup_path = _PATCHES_DIR / f"planner_backup_{patch_id}.py"
    shutil.copy(_PLANNER_PATH, backup_path)

    current = _PLANNER_PATH.read_text(encoding="utf-8")

    if insert_after and insert_after in current:
        new_content = current.replace(
            insert_after,
            insert_after + "\n" + patch_text,
            1,
        )
    else:
        import re
        m = re.search(r'(SYSTEM_PROMPT\s*=\s*"""[\s\S]+?)(""")', current)
        if m:
            new_content = current[:m.start(2)] + "\n" + patch_text + "\n" + current[m.start(2):]
        else:
            return {"applied": False, "error": "Could not locate SYSTEM_PROMPT in planner.py"}

    _PLANNER_PATH.write_text(new_content, encoding="utf-8")

    patch_record["applied"] = True
    patch_record["applied_at"] = datetime.now(timezone.utc).isoformat()
    patch_file.write_text(json.dumps(patch_record, indent=2))

    return {
        "applied": True,
        "backup_path": str(backup_path),
        "patch_id": patch_id,
        "eval_name": patch_record.get("eval_name"),
    }
