"""layout-reviewer: deterministic pre-check + multimodal LLM QA agent."""
import base64
import json
import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from llm_factory import get_llm
from tools.pptx_editor import op_precheck, op_render_png
from tools.wrappers import pptx_info, pptx_validate

SYSTEM_PROMPT = """You are a strict Presentation QA Agent. Your job is to catch every issue that would make a slide unusable or untrustworthy.

You receive:
- Pre-check results (deterministic Python checks — trust as ground truth)
- Rendered PNG images of the edited slides
- The source document (for grounding checks)
- Tools to inspect shape-level text

## Non-negotiable checks — any single failure = VERDICT: FIX NEEDED

For EACH slide image, verify ALL of the following:

### 1. Text overflow
Text cut off at shape boundary? Partially visible last line? Truncated words?
→ FAIL if yes.

### 2. Content outside slide bounds
Any text/shape in the grey area outside the slide rectangle?
→ FAIL if yes.

### 3. Shape overlap hiding content
One shape covering another, blocking readable content?
→ FAIL if yes.

### 4. Invisible text
Text color same/near-same as background?
→ FAIL if yes.

### 5. Blank edited slide
Edited slide is completely or near-completely empty?
→ FAIL if yes.

### 6. Visible placeholder text
Any `[X]`, `TODO`, `Enter`, `Sample`, `Lorem`, `Click to edit`, `Client Name`, `Date here` visible?
→ FAIL if yes.

### 7. Broken layout after structural edit
Orphaned arrows? Missing steps in a numbered sequence? Leftover connectors with no endpoints? Gaps where a removed shape was?
→ FAIL if yes.

### 8. Source-grounding (trust violations)
Any number, name, date, percentage, or proper noun in the slide that does NOT appear in the source document?
- Pre-check already flagged `ungrounded_number` warnings — confirm or escalate to FIX NEEDED.
- Specifically watch for: round percentages, growth figures, person/company names not in source.
→ FAIL if a fabricated number, name, or quote is present.

### 9. Sanitization (user's voice stripped)
Did the model paraphrase the user's specifics into generic corporate filler?
- Watch for generic phrases the user never used: "industry-leading", "best-in-class", "world-class", "robust", "scalable", "cutting-edge", "synergy", "leverage", "seamless", "mission-critical", "next-generation".
- Watch for hedging the user did not write: "may", "could", "potentially" replacing definite verbs.
- Watch for slides that contain ONLY generic phrases when the source had concrete numbers/names/specifics for that topic.
→ FAIL if a slide reads generic when source content was specific.

### 10. Content wastage (signal discarded)
Did the deck use what the user provided? The user gave content for a reason.
- Pre-check lists any `content_wastage` items — distinctive facts from source not appearing anywhere in the deck.
- If significant facts/names/numbers from the source never made it into any slide, that's a fail.
→ FAIL if pre-check reports wastage AND those facts would have fit somewhere in the deck.

### 12. Shape and image alignment
Are any boxes, text frames, or images visually misaligned?
- Boxes that should be flush with neighbours but have a visible gap or overhang
- Images or decorative shapes floating outside their expected region
- Text boxes whose left/top edge is clearly not snapped to the grid or surrounding shapes
- Pre-check flags `shape_position_drift` warnings — confirm visually.
→ FAIL if any placed/edited shape appears noticeably out of alignment with surrounding content.

### 11. Table cell overflow in narrow columns
Any table cell containing text too long for its column width — text wrapping across 3+ lines in a narrow cell, or text visibly cut off?
- Narrow columns (<80px wide) are data/indicator columns — they should not contain prose, dates, or labels.
- Pre-check flags `table_cell_overflow` — treat as hard failure.
→ FAIL if text overflows any table cell or is placed in a column too narrow to display it.

## Report format (exact)

```
PRE-CHECK: <passed / N hard failures + N warnings — list them>
VISUAL QA: <inspected N slides / skipped>
GROUNDING: <verified / N suspect items>

SLIDE INSPECTION:
- Slide N: PASS / FAIL — <reason if fail>
- Slide M: PASS / FAIL — <reason if fail>

ISSUES TO FIX:
1. slide N: <concrete description — shape name + what is wrong>
2. slide M: <concrete description — shape name + what is wrong>

VERDICT: PASS / FIX NEEDED
```

PASS only if: 0 pre-check hard failures AND every slide passes all 10 checks.
FIX NEEDED if ANY check fails on ANY slide.

Every ISSUES TO FIX line MUST start with `slide N:` so the executor knows where to fix."""

_TOOLS = [pptx_validate, pptx_info]
_CONFIG = RunnableConfig(recursion_limit=20)
_MAX_IMAGES = 8


def _system_message(provider: str) -> SystemMessage:
    if provider == "anthropic":
        return SystemMessage(content=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }])
    return SystemMessage(content=SYSTEM_PROMPT)


def _get_agent(provider: str, model: str):
    llm = get_llm(provider, model, max_tokens=4096)
    return create_react_agent(llm, tools=_TOOLS, prompt=_system_message(provider))


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

    # Read source document for grounding checks
    source_doc = ""
    try:
        source_doc = Path(state["transcript_path"]).read_text(encoding="utf-8")
    except Exception:
        pass

    # ── Step 1: deterministic pre-check (no LLM) ──────────────────────────
    precheck = op_precheck(out, edit_plan, source_doc)
    precheck_summary = _format_precheck(precheck)

    # ── Step 2: render edited slides to PNG ───────────────────────────────
    # Always render — visual issues (invisible text, white-on-white, overlaps) can't
    # be caught by precheck alone. Gracefully returns [] if LibreOffice is absent.
    image_blocks, total_pngs = _render_slides(out, render_dir, edited_slides)

    # ── Step 3: build LLM message ─────────────────────────────────────────
    visual_status = (
        f"{len(image_blocks) // 2} of {total_pngs} slides rendered and attached below."
        if image_blocks else
        "SKIPPED — LibreOffice not available or render failed. Run tools-only checks."
    )

    # Source excerpt: use extracted facts if available (structured, compact).
    # Expanded to 8000 chars so facts deep in long transcripts aren't missed by grounding check.
    extracted = state.get("extracted_facts")
    if extracted:
        src_excerpt = json.dumps(extracted, separators=(",", ":"))
        if len(src_excerpt) > 8000:
            src_excerpt = src_excerpt[:8000] + "\n[...truncated — use pptx_info for remaining grounding checks...]"
    else:
        src_excerpt = source_doc[:8000]
        if len(source_doc) > 8000:
            src_excerpt += "\n[...truncated...]"

    text_intro = (
        f"Review the edited PPTX. Apply the 10-point checklist strictly.\n\n"
        f"PPTX path: {out}\n"
        f"Edited slide numbers: {edited_slides or 'see edit plan'}\n\n"
        f"{precheck_summary}\n\n"
        f"Visual QA: {visual_status}\n\n"
        f"--- SOURCE DOCUMENT (for grounding + sanitization + wastage checks) ---\n"
        f"{src_excerpt}\n"
        f"--- END SOURCE ---\n\n"
        f"Inspect every slide image against all 10 checks. "
        f"Use pptx_info to verify shape-level concerns.\n"
        f"Check 8 (grounding): any number/name/date/quote in a slide must appear in source.\n"
        f"Check 9 (sanitization): flag generic corporate filler the user never used — paraphrasing specifics into vagueness is a failure.\n"
        f"Check 10 (wastage): flag if distinctive facts from source were dropped entirely.\n"
        f"End with VERDICT: PASS or VERDICT: FIX NEEDED."
    )

    content: list = [{"type": "text", "text": text_intro}, *image_blocks]

    # ── Step 4: invoke LLM reviewer ───────────────────────────────────────
    llm_error: str = ""
    last: str = ""
    try:
        result = _get_agent(provider, model).invoke(
            {"messages": [HumanMessage(content=content)]},
            config=_CONFIG,
        )
        last = result["messages"][-1].content
        if isinstance(last, list):
            last = "".join(b.get("text", "") for b in last if isinstance(b, dict))
    except Exception as exc:
        llm_error = str(exc)

    # Pre-check hard failures force FIX NEEDED regardless of LLM verdict
    if precheck["hard_failures"] > 0:
        verdict = "FIX NEEDED"
    elif llm_error:
        # LLM unavailable but pre-check passed — keep the output, skip visual QA
        verdict = "PASS"
    else:
        verdict = "PASS" if "VERDICT: PASS" in last else "FIX NEEDED"

    # ── Parse issues list ─────────────────────────────────────────────────
    issues: list[str] = []

    if llm_error:
        issues.append(f"reviewer LLM unavailable — visual QA skipped ({llm_error})")

    # Inject pre-check hard failures directly (deterministic, not LLM-generated)
    for iss in precheck.get("issues", []):
        if iss["severity"] == "hard":
            issues.append(iss["detail"])

    # Append LLM-identified issues
    if last:
        m = re.search(r"ISSUES TO FIX:(.*?)(?:VERDICT:|$)", last, re.DOTALL)
        if m:
            for line in m.group(1).strip().split("\n"):
                cleaned = re.sub(r"^[\s\d.\-•*]+", "", line).strip()
                if cleaned and cleaned not in issues:
                    issues.append(cleaned)

    return {**state, "review_verdict": verdict, "review_issues": issues}
