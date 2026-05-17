"""pptx-executor: applies edit plan to PPTX via a LangGraph react agent."""
import json
import re
import shutil
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from llm_factory import get_llm
from tools.wrappers import (
    font_measure, layout_solve, pptx_check_overflow, pptx_info,
    pptx_remove_slides, pptx_render_png, pptx_set_cell, pptx_set_para,
    pptx_set_position, pptx_set_text, pptx_spatial_map, pptx_validate,
)

SYSTEM_PROMPT = """You are a universal PPTX Execution Agent.

You receive a JSON edit plan for any PPTX file and apply every change using the available tools. You never guess coordinates — you always measure first.

## Tools
- pptx_info: inspect all shapes on all slides
- pptx_spatial_map: get available height + EMU coordinates for text shapes on a slide
- pptx_check_overflow: check whether text fits a shape before setting it
- pptx_set_text: set text in a named shape (\\n = new paragraph / bullet)
- pptx_set_para: replace one paragraph while preserving its formatting
- pptx_set_cell: set a table cell (row/col 0-based)
- pptx_remove_slides: remove slides by comma-separated 1-based indices
- pptx_set_position: move/resize a shape (EMU: 1 inch = 914400)
- pptx_validate: scan for unfilled placeholder strings and report them
- pptx_render_png: render slides to PNG (requires LibreOffice)
- font_measure: measure text pixel dimensions
- layout_solve: compute stacked layout positions

## Workflow

The template is already copied to the output path. Start at step 1.

1. **pptx_spatial_map** every slide you will edit BEFORE touching any text.
2. **pptx_remove_slides** for any indices listed in `slides_to_remove`.
3. **Overflow-safe loop** for every `set_text` edit on a non-table shape:
   - pptx_check_overflow → if `overflows: false` → pptx_set_text
   - if `overflows: true` → shorten the text → retry until it fits
4. **Apply all edits** from the plan (set_text, set_cell, set_para as appropriate).
5. **pptx_validate** at the end. If it reports any unfilled placeholders (patterns like `[X]`, `TODO`, `Enter`, `Sample`, `Lorem`, `Click to edit`), fix them now.

## Rules
- Shape names are case-sensitive — always use the exact name from pptx_info.
- Only edit slides listed in the plan — do not touch slides not mentioned.
- EMU: 1 inch = 914400, standard slide = 9144000 × 5143500.
- If a shape name from the plan is not found, call pptx_info to get the correct name and use the closest match."""

_TOOLS = [
    pptx_info, pptx_spatial_map, pptx_check_overflow,
    pptx_set_text, pptx_set_para, pptx_set_cell,
    pptx_remove_slides, pptx_set_position, pptx_validate,
    pptx_render_png, font_measure, layout_solve,
]

_CONFIG = RunnableConfig(recursion_limit=150)


@lru_cache(maxsize=8)
def _get_agent(provider: str, model: str):
    llm = get_llm(provider, model, max_tokens=4096)
    return create_react_agent(llm, tools=_TOOLS, prompt=SYSTEM_PROMPT)


_SLIDE_NUM_RE = re.compile(r"slide\s+(\d+)", re.IGNORECASE)


def _extract_flagged_slides(issues: list[str]) -> set[int]:
    flagged = set()
    for iss in issues:
        for m in _SLIDE_NUM_RE.finditer(iss):
            flagged.add(int(m.group(1)))
    return flagged


def executor_node(state: dict) -> dict:
    provider = state.get("provider", "anthropic")
    fix_attempts = state.get("fix_attempts", 0)
    is_fix_pass = fix_attempts > 0

    # Fix pass uses the stronger planner model (Sonnet) — better reasoning for harder cases.
    if is_fix_pass:
        model = state.get("planner_model", "claude-sonnet-4-6")
    else:
        model = state.get("executor_model", "claude-haiku-4-5-20251001")

    out = state["output_path"]

    if not is_fix_pass:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(state["template_path"], out)

    edit_plan = state["edit_plan"]
    fix_note = ""

    if is_fix_pass and state.get("review_issues"):
        flagged = _extract_flagged_slides(state["review_issues"])
        if flagged:
            # Filter edits to ONLY those slides the reviewer flagged.
            edit_plan = {
                **edit_plan,
                "edits": [e for e in edit_plan.get("edits", []) if e.get("slide") in flagged],
                "slides_to_remove": [],  # never re-attempt removals on fix pass
            }
        issues_str = "\n".join(f"  {i+1}. {iss}" for i, iss in enumerate(state["review_issues"]))
        fix_note = (
            f"\n\nFIX PASS (attempt {fix_attempts + 1}). Reviewer flagged these issues:\n"
            f"{issues_str}\n\n"
            f"Touch ONLY the slides listed above. Do not re-edit slides not in the list."
        )

    plan_str = json.dumps(edit_plan, indent=2)
    prompt = (
        f"Apply the edit plan to the PPTX.\n\n"
        f"Output PPTX path: {out}\n"
        f"{'(File already partially edited — fix the flagged issues only.)' if is_fix_pass else '(Template already copied — start from step 1, spatial_map.)'}\n\n"
        f"Edit plan:\n{plan_str}{fix_note}"
    )

    _get_agent(provider, model).invoke(
        {"messages": [HumanMessage(content=prompt)]}, config=_CONFIG
    )
    return {**state, "fix_attempts": fix_attempts + 1}
