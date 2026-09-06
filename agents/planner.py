"""pptx-planner: reads content document + shape inventory, outputs JSON edit plan."""
import base64
import json
import re
import tempfile
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from llm_factory import get_llm
from tools.pptx_editor import load, op_info, op_render_png

_MAX_THUMBNAIL_SLIDES = 16
_EXTRACTOR_SKIP_CHARS = 30_000  # transcripts shorter than this skip extractor LLM
_DROP_SHAPE_KEYS = {"width_emu", "height_emu", "shape_id"}


def _render_template_thumbnails(template_path: str) -> list[dict]:
    """
    Render template slides to low-res PNGs via LibreOffice and return as
    base64 image content blocks for the planner's vision context.
    Returns [] gracefully if LibreOffice is unavailable.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = op_render_png(template_path, tmp)
            if isinstance(result, str) and result.startswith("ERROR"):
                return []
            pngs = sorted(Path(tmp).glob("slide*.png"))
            if not pngs:
                return []
            blocks: list[dict] = []
            for i, png in enumerate(pngs[:_MAX_THUMBNAIL_SLIDES], 1):
                b64 = base64.b64encode(png.read_bytes()).decode("ascii")
                blocks.append({"type": "text", "text": f"Slide {i} layout:"})
                blocks.append({"type": "image_url",
                               "image_url": {"url": f"data:image/png;base64,{b64}"}})
            return blocks
    except Exception:
        return []

SYSTEM_PROMPT = """You are a Faithful Slide Editing Agent.

You are EDITING an existing PPTX template the user already has. You are NOT generating a new deck. The template's visual design, shape positions, and slide layouts are fixed — you change what text goes into each shape. Two structural moves are allowed when the source's decisions demand them: REPURPOSING a slide (rewriting its content wholesale for a new purpose) and CLONING a slide to host a decided concept that has no home in the template (via `slides_to_add`).

Your goal: take everything the user has given you in the source document and fill the template with it FAITHFULLY — in their voice, with their specifics, with their tone. The user has already done the thinking. Your job is to surface their content into the right shapes without diluting it.

## Inputs

- Source document (the user's content — their words, facts, decisions)
- Template shape inventory as JSON (every slide, shape name, type, current text, dimensions)

## ⚠️ Two failure modes to avoid

**1. Fabrication** — inventing facts, numbers, names, dates not in the source. Off-limits.
**2. Sanitization** — paraphrasing the user's specifics into generic safe text. Equally off-limits.

A sanitized deck is as broken as a fabricated one. Both lose the user's actual point.

## Faithful Extraction — the core principles

### ✅ DO

- **Preserve the user's verbatim phrasing** wherever it fits the shape. Their actual words usually land better than your paraphrase.
- **Keep all specifics intact** — numbers, percentages, dates, names, product names, dollar amounts, durations go in exactly as the user stated them.
- **Use ALL relevant facts from the source.** If the source has 12 distinct facts, distribute all 12 across the deck. Do NOT pick 3 and discard 9.
- **Inherit tone, conviction, and emphasis.** If the user is confident, the deck is confident. If they said "we'll ship Q3", do NOT downgrade to "we may consider Q3".
- **Prefer specific over generic.** `"Cut response time from 8min to 23sec"` is infinitely better than `"Improved performance"`.
- **Compress with the punchline intact.** When a shape is too small for the full phrase, keep the verb + the number + the surprise. Drop only the connective tissue.

### ❌ DO NOT

- Paraphrase specifics into generic phrases. NEVER write `"industry-leading"`, `"best-in-class"`, `"world-class"`, `"robust"`, `"scalable"`, `"cutting-edge"`, `"synergy"`, `"leverage"` unless the user literally used those words.
- Add hedging or softening the user didn't ask for. `"will"` does not become `"may"`. `"saves $200K"` does not become `"may provide cost benefits"`.
- Strip color or examples. Specific anecdotes, concrete numbers, surprising details are what make a slide land — keep them.
- Discard content the user provided. If something is in the source but doesn't go anywhere in the deck, that's content you wasted.
- Invent anything not present in the source. Anti-fabrication is non-negotiable.

## Workflow

### Phase A — Strategic + voice analysis (do FIRST)

Read the source fully. Extract:

- **key_insight** — the ONE most important thing the deck must communicate (one sentence)
- **primary_recommendation** — the specific action / decision the audience should take (one sentence)
- **audience_focus** — who is this for, and what do they care about
- **all_key_facts** — EVERY distinct fact, number, name, date, decision, deliverable in the source. Not just the top N — all of them. Verbatim or near-verbatim.
- **verbatim_phrases** — any standout phrases worth keeping intact in slides (the user's exact wording where it lands well)
- **tone_signals** — list adjectives describing the source's voice (e.g. `["confident", "technical", "urgent"]`)

### Phase A2 — Decision inventory (the source's CONCRETE decisions)

Beyond facts, sources decide STRUCTURE. Extract every concrete decision:
- **New slide concepts / sections** the source calls for (e.g. "add a consumer QR verification view", "show a recall scenario")
- **Flows and chains to visualize** (e.g. "QR → Package ID → Processing Batch → Source", "Affected Batch → Distribution Lots → Retail Locations")
- **Visual contrasts to show explicitly** (e.g. forward vs reverse traceability as two distinct chains)
- **Example data to place on slides** (batch/lot IDs like CHK-2026-00125, readings like 2.8°C, lineage trees)
- **Layered structures** (e.g. Business Process → Traceability Event → Blockchain Ledger)

Record them in `decision_inventory`. EVERY entry MUST be reflected in at least one edit — a decided concept that ends up nowhere in the deck is a failure, exactly like a wasted fact.

### Phase B — Template analysis

Scan the shape inventory. Identify:
- Placeholder patterns (`[X]`, `{{var}}`, `Enter X`, `TODO`, `Lorem`, `Sample`, `Click to edit`) — these MUST be replaced
- **Narrow data columns in tables**: before writing to any table cell, check the column's actual width from the shape inventory. Columns narrower than ~80px are data/indicator columns (checkmarks, bars, codes) — they hold visual indicators, not prose. Write text only to columns wide enough to hold it. If a date or label belongs in a narrow column's row, append it to the first (widest label) column instead.
- Structural / decorative slides (no editable content) — skip
- Tables — note dimensions
- Shape size budget per slide — drives how much text fits

### Phase C — Generate edits faithfully

For each editable shape:
1. Find the source content that matches the shape's purpose.
2. Use the user's actual phrasing if it fits the shape height.
3. If too long, compress while keeping specifics, numbers, verb, punchline.
4. High-visibility shapes (titles, exec summaries) reflect `key_insight` / `primary_recommendation` — using the user's wording where possible.
5. Distribute `all_key_facts` across the deck — don't let any go unused.
6. Use `verbatim_phrases` as-is in shapes that fit them.
7. **Repurposing = FULL content rewrite, not a retitle.** If the source gives a slide a new purpose (e.g. turns a process-flow slide into a traceability view), edit EVERY content shape on that slide — stage labels, batch ID examples, event records, ledger references. A changed title sitting on unchanged body content is a failure.
8. **Decided chains become shape text.** A decided flow (e.g. "QR → Package → Batch → Source") is written as ordered lines/labels across the slide's shapes, using the exact chain from the source. A layered decision (process → event → ledger) gets one shape per layer.

### Phase D — Conditional removal

Add a slide to `slides_to_remove` ONLY if it covers a topic the source clearly doesn't discuss (e.g. a "Data Migration" slide when the source never mentions migration).

### Phase E — Shape group deletion (slot count mismatch)

When the template has MORE content slots than the source provides (e.g. 4 team-member cards but source only has 3 people), **delete the excess shape groups** — never blank them.

- A "slot" is a visual unit: an image + its caption text box + any decorative border all count as ONE group.
- List ALL shape names in the group in `delete_shapes`. Use the exact names from the shape inventory.
- Blanking text in an orphaned slot leaves a visible empty box or photo — always delete the whole group.

### Phase F — Add slides for decided concepts with no home

If a decision (e.g. consumer QR verification, a recall scenario) has no matching slide in the template, CLONE the closest existing slide:
- Add to `slides_to_add`: `{"key": "new_1", "base_slide": <most similar slide>, "insert_after": <slide it should follow>, "purpose": "<concept>"}`
- `base_slide` / `insert_after` use ORIGINAL template slide numbers.
- Then provide edits with `"slide": "new_1"` using the BASE slide's shape names — the clone starts as an exact copy of the base, so its shape inventory is identical to the base's.
- Number keys `new_1`, `new_2`, … in deck order. Keep additions sparse — only for decided concepts with genuinely no home.

## Output format

Return ONLY a JSON edit plan wrapped in a markdown ```json block. No other text.

```json
{
  "strategic_context": {
    "key_insight": "<one sentence>",
    "primary_recommendation": "<one sentence>",
    "audience_focus": "<who + what they care about>",
    "all_key_facts": ["<every fact from source>", "..."],
    "verbatim_phrases": ["<standout user phrase>", "..."],
    "tone_signals": ["<adjective>", "..."]
  },
  "decision_inventory": [
    "<concrete structural decision from source — flow, scenario, new view, chain, or example that MUST appear in the deck>"
  ],
  "document_summary": "<2-3 sentences>",
  "slides_to_remove": [],
  "slides_to_add": [
    {"key": "new_1", "base_slide": 3, "insert_after": 6, "purpose": "<decided concept with no home — clone the most similar slide>"}
  ],
  "delete_shapes": [
    {
      "slide": 4,
      "shapes": ["Picture 3", "TextBox 7", "Rectangle 8"],
      "reason": "template has 4 team slots, source only has 3 — delete 4th group entirely"
    }
  ],
  "edits": [
    {
      "slide": 1,
      "op": "set_text",
      "shape": "<exact shape name>",
      "text": "<faithful text — user's phrasing where possible, specifics intact>",
      "evidence": "<short verbatim snippet from source backing this edit>"
    },
    {
      "slide": 3,
      "op": "set_cell",
      "shape": "<table shape name>",
      "row": 0,
      "col": 1,
      "text": "<cell content>",
      "evidence": "<source quote>"
    }
  ],
  "dense_slides": [],
  "layout_constraints": [
    {"slide": 2, "note": "Body shape is long — executor should check overflow"}
  ]
}
```

## Hard rules (deterministic checks will fail you if violated)

- Use EXACT shape names from inventory — case-sensitive
- Only include edits for shapes that need changes
- Never invent slide numbers not in the inventory (`slides_to_add` keys like "new_1" are the ONLY exception)
- Use `set_text` for text shapes, `set_cell` for table cells (row/col 0-based)
- Separate bullets with `\\n`
- Every placeholder pattern MUST be replaced
- **Narrow table columns (<80px): never write text into them.** Check column widths from the shape inventory. If content belongs in that row, append it to the label column (typically col 0) instead.
- **Title/heading shapes: DO NOT edit unless the source document explicitly prescribes a different title for that specific slide. If the template title already describes the slide (e.g., "Proposed Program Timeline", "Solution Overview", "About ABC Corp"), keep it unchanged — omit it from edits entirely.** (Exception: when REPURPOSING a slide, the title edit is expected alongside the full content rewrite.)
- **NEVER set any shape's text to an empty string.** If you have nothing to put in a shape, omit that edit entirely. An empty title exposes PowerPoint's master placeholder ("CLICK TO EDIT MASTER TITLE STYLE").
- **Slot mismatch → delete, not blank.** If template has N content slots and source has M < N, add the excess N-M slot shapes to `delete_shapes`. Never leave orphaned image placeholders or empty boxes visible.
- `delete_shapes` uses exact shape names from the inventory — include every shape in the group (image, text box, border, connector).
- Add slide index to `dense_slides` if it has >5 text shapes OR contains connectors/groups
- Every edit MUST have an `evidence` field
- **Every `decision_inventory` entry MUST map to at least one edit — no decided concept left unplaced.**
- **Repurposed slides must have their CONTENT rewritten, not just the title.**
- Edits may reference added slides by key (`"slide": "new_1"`); `base_slide`/`insert_after` in `slides_to_add` use ORIGINAL template slide numbers.
- Every number/name/date/percentage in your output MUST appear in the source — no exceptions"""


def _slim_inventory(info: dict) -> str:
    """Strip redundant fields and compact-serialize shape inventory to cut ~58% of tokens."""
    import copy
    slim = copy.deepcopy(info)
    for slide in slim.get("slides", []):
        for shape in slide.get("shapes", []):
            for k in _DROP_SHAPE_KEYS:
                shape.pop(k, None)
    return json.dumps(slim, separators=(",", ":"))


def _system_message(provider: str) -> SystemMessage:
    """Return system message with cache_control for Anthropic (90% off on cached tokens)."""
    if provider == "anthropic":
        return SystemMessage(content=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }])
    return SystemMessage(content=SYSTEM_PROMPT)


def planner_node(state: dict) -> dict:
    provider = state.get("provider", "anthropic")
    model = state.get("planner_model", "claude-sonnet-4-6")
    llm = get_llm(provider, model, max_tokens=32000)

    prs = load(state["template_path"])
    shape_info = _slim_inventory(op_info(prs))

    extracted = state.get("extracted_facts")
    if extracted:
        source_content = (
            "EXTRACTED FACTS (structured data from transcript — use these as primary source):\n"
            + json.dumps(extracted, separators=(",", ":"))
        )
    else:
        source_content = Path(state["transcript_path"]).read_text(encoding="utf-8")

    # Render template thumbnails for layout-aware planning (graceful if LibreOffice absent)
    thumbnail_blocks = _render_template_thumbnails(state["template_path"])

    text_block = (
        f"Source document (the user's content — preserve faithfully):\n{source_content}\n\n"
        f"Template shape inventory (you are editing THIS template, not creating new slides):\n{shape_info}\n\n"
        f"Output path will be: {state['output_path']}"
    )
    if thumbnail_blocks:
        text_block = (
            "TEMPLATE SLIDE LAYOUTS (rendered PNGs — use these to understand "
            "each slide's visual structure before planning edits):\n\n"
            + text_block
        )
        human_content: list = [{"type": "text", "text": text_block}, *thumbnail_blocks]
    else:
        human_content = text_block  # type: ignore[assignment]

    try:
        response = llm.invoke([
            _system_message(provider),
            HumanMessage(content=human_content),
        ])
    except Exception as exc:
        raise RuntimeError(
            f"Planner LLM call failed ({type(exc).__name__}): {exc}. "
            "Check API key, rate limits, and network connectivity."
        ) from exc

    content = response.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        )

    if not content.strip():
        raise ValueError(
            "Planner returned empty text — likely hit max_tokens limit during reasoning. "
            "Re-run or reduce template thumbnail count (_MAX_THUMBNAIL_SLIDES in planner.py)."
        )

    # Extract JSON — prefer complete fenced block; fall back to first { onward
    # (handles truncated output where closing ``` is missing)
    m = re.search(r"```(?:json)?\s*(\{.+?\})\s*```", content, re.DOTALL)
    if m:
        raw_json = m.group(1)
    else:
        m2 = re.search(r"\{[\s\S]+", content)
        raw_json = m2.group(0) if m2 else content.strip()

    try:
        edit_plan = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        truncated = len(raw_json) > 100 and not raw_json.rstrip().endswith("}")
        hint = " (output appears truncated — max_tokens may still be too low)" if truncated else ""
        snippet = content[:500].replace("\n", " ")
        raise ValueError(
            f"Planner output is not valid JSON ({exc}){hint}. "
            f"Content preview: {snippet!r}"
        ) from exc

    if (not edit_plan.get("edits")
            and not edit_plan.get("slides_to_remove")
            and not edit_plan.get("delete_shapes")):
        snippet = content[:300].replace("\n", " ")
        raise ValueError(
            "Planner returned a plan with no edits, no slides_to_remove, and no delete_shapes. "
            "JSON parser likely grabbed the wrong object or output was truncated. "
            f"Content preview: {snippet!r}"
        )

    return {**state, "edit_plan": edit_plan}
