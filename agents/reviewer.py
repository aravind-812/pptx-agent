"""layout-reviewer: deterministic pre-check + multimodal LLM QA agent."""
import base64
import re
from functools import lru_cache
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from llm_factory import get_llm
from tools.pptx_editor import op_precheck, op_render_png
from tools.wrappers import pptx_info, pptx_validate

SYSTEM_PROMPT = """You are a strict Presentation QA Agent. Your job is to catch every issue that would make a slide unusable or unprofessional.

You receive:
- Pre-check results (deterministic Python checks already run — trust these as ground truth)
- Rendered PNG images of the edited slides
- Tools to inspect shape-level text

## Non-negotiable checks — any single failure = VERDICT: FIX NEEDED

For EACH slide image provided, verify ALL of the following:

### 1. Text overflow
Is any text visibly cut off at the shape boundary? Partially visible last line? Truncated words?
→ FAIL if yes. Report: "slide N / shape name: text overflow"

### 2. Content outside slide bounds
Any text, shape, or element appearing in the grey area outside the slide rectangle?
→ FAIL if yes.

### 3. Shape overlap hiding content
Is one shape sitting on top of another, making content behind it unreadable?
→ FAIL if yes.

### 4. Invisible text
Does any text appear to have the same or very similar color to its background (white-on-white, dark-on-dark)?
→ FAIL if yes.

### 5. Blank edited slide
Does a slide that was supposed to be edited appear completely or near-completely empty?
→ FAIL if yes.

### 6. Visible placeholder text
Is any placeholder pattern literally visible in the rendered slide? ([X], TODO, Enter, Sample, Lorem, "Client Name", "Date here", etc.)
→ FAIL if yes. (Pre-check already flags these in XML — confirm visually.)

### 7. Broken layout after structural edit
After any removal or reordering, are there: orphaned arrows pointing nowhere, missing steps in a numbered sequence, leftover connector lines with no endpoints, gaps where a deleted shape used to be?
→ FAIL if yes.

## Report format (exact, do not deviate)

```
PRE-CHECK: <passed / N hard failures — list them>
VISUAL QA: <inspected N slides / skipped>

SLIDE INSPECTION:
- Slide N: PASS / FAIL — <reason if fail>
- Slide M: PASS / FAIL — <reason if fail>

ISSUES TO FIX:
1. slide N: <concrete description — shape name + what is wrong>
2. slide M: <concrete description — shape name + what is wrong>

VERDICT: PASS / FIX NEEDED
```

PASS only if: pre-check has 0 hard failures AND every slide image passes all 7 checks.
FIX NEEDED if: ANY pre-check hard failure OR ANY slide fails ANY of the 7 checks.

Always prefix each ISSUES TO FIX line with `slide N:` so the executor knows which slide to target."""

_TOOLS = [pptx_validate, pptx_info]
_CONFIG = RunnableConfig(recursion_limit=20)
_MAX_IMAGES = 12


@lru_cache(maxsize=8)
def _get_agent(provider: str, model: str):
    llm = get_llm(provider, model, max_tokens=4096)
    return create_react_agent(llm, tools=_TOOLS, prompt=SYSTEM_PROMPT)


def _render_slides(out_path: str, render_dir: str, edited_slides: list[int]) -> tuple[list[dict], int]:
    """Render slides → base64 image content blocks. Returns (blocks, total_slide_count)."""
    try:
        Path(render_dir).mkdir(parents=True, exist_ok=True)
        result = op_render_png(out_path, render_dir)
        if isinstance(result, str) and result.startswith("ERROR"):
            return [], 0
    except Exception:
        return [], 0

    pngs = sorted(Path(render_dir).glob("slide*.png"))
    if not pngs:
        return [], 0

    targets = ([i for i in edited_slides if 1 <= i <= len(pngs)][:_MAX_IMAGES]
               if edited_slides else list(range(1, min(len(pngs), _MAX_IMAGES) + 1)))

    blocks: list[dict] = []
    for idx in targets:
        try:
            b64 = base64.b64encode(pngs[idx - 1].read_bytes()).decode("ascii")
        except Exception:
            continue
        blocks.append({"type": "text", "text": f"Slide {idx}:"})
        blocks.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

    return blocks, len(pngs)


def _format_precheck(result: dict) -> str:
    """Format pre-check result into a human-readable summary for the LLM message."""
    if result.get("error"):
        return f"PRE-CHECK ERROR: {result['error']}"
    issues = result.get("issues", [])
    if not issues:
        return "PRE-CHECK: all passed (0 hard failures, 0 warnings)"
    lines = [f"PRE-CHECK: {result['hard_failures']} hard failure(s), {result['warn_count']} warning(s)"]
    for iss in issues:
        tag = "HARD" if iss["severity"] == "hard" else "WARN"
        lines.append(f"  [{tag}] {iss['type']}: {iss['detail']}")
    return "\n".join(lines)


def reviewer_node(state: dict) -> dict:
    provider = state.get("provider", "anthropic")
    model = state.get("executor_model", "claude-haiku-4-5-20251001")
    out = state["output_path"]
    edit_plan = state.get("edit_plan", {})
    deck_id = Path(out).stem
    render_dir = str(Path(state.get("work_dir", "runs/work")) / "visual_qa" / deck_id)
    edited_slides = sorted({e.get("slide") for e in edit_plan.get("edits", []) if e.get("slide")})

    # ── Step 1: deterministic pre-check (no LLM) ──────────────────────────
    precheck = op_precheck(out, edit_plan)
    precheck_summary = _format_precheck(precheck)

    # ── Step 2: render edited slides to PNG ───────────────────────────────
    image_blocks, total_pngs = _render_slides(out, render_dir, edited_slides)

    # ── Step 3: build LLM message ─────────────────────────────────────────
    visual_status = (
        f"{len(image_blocks) // 2} of {total_pngs} slides rendered and attached below."
        if image_blocks else
        "SKIPPED — LibreOffice not available or render failed. Run tools-only checks."
    )

    text_intro = (
        f"Review the edited PPTX. Apply the 7-point checklist strictly.\n\n"
        f"PPTX path: {out}\n"
        f"Edited slide numbers: {edited_slides or 'see edit plan'}\n\n"
        f"{precheck_summary}\n\n"
        f"Visual QA: {visual_status}\n\n"
        f"Inspect every slide image against all 7 checks. "
        f"Then use pptx_info to verify any shape-level concerns. "
        f"End with VERDICT: PASS or VERDICT: FIX NEEDED."
    )

    content: list = [{"type": "text", "text": text_intro}, *image_blocks]

    # ── Step 4: invoke LLM reviewer ───────────────────────────────────────
    result = _get_agent(provider, model).invoke(
        {"messages": [HumanMessage(content=content)]},
        config=_CONFIG,
    )

    last = result["messages"][-1].content
    if isinstance(last, list):
        last = "".join(b.get("text", "") for b in last if isinstance(b, dict))

    # Pre-check hard failures force FIX NEEDED regardless of LLM verdict
    if precheck["hard_failures"] > 0:
        verdict = "FIX NEEDED"
    else:
        verdict = "PASS" if "VERDICT: PASS" in last else "FIX NEEDED"

    # ── Parse issues list ─────────────────────────────────────────────────
    issues: list[str] = []

    # Inject pre-check hard failures directly (deterministic, not LLM-generated)
    for iss in precheck.get("issues", []):
        if iss["severity"] == "hard":
            issues.append(iss["detail"])

    # Append LLM-identified issues
    m = re.search(r"ISSUES TO FIX:(.*?)(?:VERDICT:|$)", last, re.DOTALL)
    if m:
        for line in m.group(1).strip().split("\n"):
            cleaned = re.sub(r"^[\s\d.\-•*]+", "", line).strip()
            if cleaned and cleaned not in issues:
                issues.append(cleaned)

    return {**state, "review_verdict": verdict, "review_issues": issues}
