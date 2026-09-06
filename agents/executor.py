"""pptx-executor: hybrid Python+LLM executor for applying edit plans.

Phases:
  1. Python preflight — fuzzy-resolve shape names, pre-fetch spatial maps,
     predict overflow per edit. No LLM cost.
  2. Deterministic batch apply — op_apply_edits_batch handles set_text,
     set_cell, set_para in one transaction with autofit-truncate. No LLM cost.
  3. LLM fallback (ReAct) — invoked only for edits that failed in phase 2
     (ambiguous shapes, table layouts, complex cases).
  4. Deterministic finalisation — auto-relayout dense slides, placeholder
     sweep, verification pass. No LLM cost.

Goal: zero LLM cost on the typical happy path; LLM reserved for genuine ambiguity.
"""
import json
import re
import shutil
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import create_react_agent

from llm_factory import get_llm
from tools.pptx_editor import (
    load, op_apply_edits_batch, op_apply_structure, op_delete_shapes,
    op_fit_text_overflow, op_placeholder_sweep, op_relayout_slide,
    op_resolve_shape_name, op_verify_edits,
)
from tools.wrappers import (
    font_measure, layout_solve,
    pptx_apply_edits_batch, pptx_check_overflow, pptx_delete_shapes,
    pptx_info, pptx_fit_overflow, pptx_placeholder_sweep, pptx_relayout_slide,
    pptx_remove_slides, pptx_render_png, pptx_resolve_shape, pptx_set_cell,
    pptx_set_para, pptx_set_position, pptx_set_text, pptx_set_text_autofit,
    pptx_spatial_map, pptx_validate, pptx_verify_edits,
)

LLM_FALLBACK_PROMPT = """You are a PPTX Repair Agent. Phase 2 (deterministic batch apply) has finished but left a small number of edits unresolved (typically ambiguous shape names or complex tables). Fix only the edits listed below.

## Tools (use the batch tool whenever possible)
- pptx_apply_edits_batch: apply many edits at once (preferred — one call instead of many)
- pptx_set_text_autofit: set text with smart truncation, preserves bullets + punchline
- pptx_resolve_shape: fuzzy-match a shape name when plan name doesn't exist
- pptx_set_cell: set a table cell (row/col 0-based)
- pptx_info: inspect a slide's actual shapes if you need to see what's there
- pptx_validate: scan for leftover placeholders
- pptx_render_png, font_measure, layout_solve, pptx_set_position: reserved

## Rules
- Use pptx_apply_edits_batch with the full failed-edits list — don't loop one shape at a time.
- For each unresolved edit: call pptx_resolve_shape on its slide to find the correct shape name, then put the resolved name in the batch.
- Never invent shape names not present on the actual slide.
- Use pptx_set_text_autofit (not raw pptx_set_text) so bullets + punchlines are preserved when truncating.

End by reporting which edits you successfully applied and which still failed."""

_EMPTY_TITLE_RE = re.compile(
    r"slide\s+(\d+)\s*/\s*'([^']+)':\s*title shape is empty",
    re.IGNORECASE,
)


def _restore_empty_titles(output_path: str, template_path: str, review_issues: list[str]) -> dict:
    """
    Fix pass step 0a: restore empty title shapes from the original template.
    Triggered when reviewer reports 'title shape is empty after edit'.
    Opens original template, reads the title text from the same slide, resets it.
    """
    targets: list[tuple[int, str]] = []
    for iss in review_issues:
        m = _EMPTY_TITLE_RE.search(iss)
        if m:
            targets.append((int(m.group(1)), m.group(2)))
    if not targets:
        return {"ok": True, "restored": 0}

    try:
        tmpl_prs = load(template_path)
        out_prs = load(output_path)
    except Exception as e:
        return {"ok": False, "error": str(e), "restored": 0}

    from tools.pptx_editor import op_set_text, save as _save

    restores = []
    for slide_idx, shape_name in targets:
        if slide_idx < 1 or slide_idx > len(tmpl_prs.slides):
            continue
        orig_text = None
        for shape in tmpl_prs.slides[slide_idx - 1].shapes:
            if shape.name == shape_name and shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t and "click to edit" not in t.lower() and "master title" not in t.lower():
                    orig_text = t
                break
        if orig_text:
            msg = op_set_text(out_prs, slide_idx, shape_name, orig_text)
            restores.append({"slide": slide_idx, "shape": shape_name,
                             "text": orig_text[:50], "ok": not msg.startswith("ERROR")})

    if restores:
        try:
            _save(out_prs, output_path)
        except Exception as e:
            return {"ok": False, "error": f"save failed: {e}", "restored": 0}

    ok_count = sum(1 for r in restores if r["ok"])
    return {"ok": True, "restored": ok_count, "details": restores}


_TOOLS = [
    pptx_apply_edits_batch, pptx_set_text_autofit, pptx_resolve_shape,
    pptx_set_cell, pptx_set_para, pptx_set_text,
    pptx_info, pptx_check_overflow, pptx_spatial_map,
    pptx_delete_shapes, pptx_remove_slides, pptx_set_position, pptx_relayout_slide,
    pptx_verify_edits, pptx_placeholder_sweep, pptx_validate, pptx_fit_overflow,
    pptx_render_png, font_measure, layout_solve,
]

_CONFIG = RunnableConfig(recursion_limit=60)
_SLIDE_NUM_RE = re.compile(r"slide\s+(\d+)", re.IGNORECASE)


def _get_agent(provider: str, model: str):
    llm = get_llm(provider, model, max_tokens=4096)
    return create_react_agent(llm, tools=_TOOLS, prompt=LLM_FALLBACK_PROMPT)


def _extract_flagged_slides(issues: list[str]) -> set[int]:
    flagged = set()
    for iss in issues:
        for m in _SLIDE_NUM_RE.finditer(iss):
            flagged.add(int(m.group(1)))
    return flagged


def _is_title_shape(shape_name: str) -> bool:
    n = shape_name.lower()
    return "title" in n or "heading" in n


def _prioritise_edits(edits: list[dict]) -> list[dict]:
    """Title and exec-summary edits first so partial execution still gives high-visibility content."""
    def _slide_key(e: dict) -> int:
        s = e.get("slide")
        return s if isinstance(s, int) else 99_999  # new-slide keys (str) sort last
    return sorted(edits, key=lambda e: (
        0 if _is_title_shape(e.get("shape", "")) else 1,
        _slide_key(e),
    ))


def _preflight_resolve(pptx_path: str, edits: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Phase 1: open the PPTX once, resolve every plan shape_name against actual shapes.
    Returns (resolved_edits, unresolvable_edits).
    Resolved edits have their `shape` field replaced with the actual matched name.
    """
    try:
        prs = load(pptx_path)
    except Exception:
        return [], edits  # let phase 2 surface the open error

    resolved: list[dict] = []
    unresolvable: list[dict] = []

    for e in edits:
        slide_idx = e.get("slide")
        shape_name = e.get("shape")
        if not slide_idx or not shape_name:
            unresolvable.append({**e, "preflight_reason": "missing slide or shape"})
            continue
        if not isinstance(slide_idx, int):
            unresolvable.append({**e, "preflight_reason": f"non-integer slide {slide_idx!r} — missing structure remap?"})
            continue
        if slide_idx < 1 or slide_idx > len(prs.slides):
            unresolvable.append({**e, "preflight_reason": f"slide {slide_idx} out of range"})
            continue

        match = op_resolve_shape_name(prs, slide_idx, shape_name)
        if match.get("ok"):
            actual = match["matched_name"]
            new_edit = {**e, "shape": actual}
            if actual != shape_name:
                new_edit["_original_shape"] = shape_name
                new_edit["_resolution_confidence"] = match.get("confidence")
            resolved.append(new_edit)
        else:
            unresolvable.append({
                **e,
                "preflight_reason": match.get("error"),
                "available_names": match.get("available_names", []),
            })

    return resolved, unresolvable


def _shapes_overlap_horizontally(s1, s2) -> bool:
    """True if two shapes share any horizontal range (i.e. they're side-by-side rather than stacked)."""
    if s1.left is None or s2.left is None or s1.width is None or s2.width is None:
        return False
    a_left, a_right = int(s1.left), int(s1.left) + int(s1.width)
    b_left, b_right = int(s2.left), int(s2.left) + int(s2.width)
    return not (a_right <= b_left or b_right <= a_left)


def _shapes_vertically_overlap(s1, s2) -> bool:
    """True if two shapes' bounding boxes vertically overlap (broken layout, not by design)."""
    if s1.top is None or s2.top is None or s1.height is None or s2.height is None:
        return False
    a_top, a_bot = int(s1.top), int(s1.top) + int(s1.height)
    b_top, b_bot = int(s2.top), int(s2.top) + int(s2.height)
    return not (a_bot <= b_top or b_bot <= a_top)


def _detect_columns(shapes: list) -> bool:
    """
    True if a slide has a column/grid layout that must NOT be vertically reflowed.
    Detected either when:
      - two text shapes are side-by-side (share Y range, separated by X), OR
      - the shapes span >1 distinct X band (multi-column or grid arrangement).
    A slide that looks like a grid is left alone — collapsing it into a single
    vertical stack would destroy the template's design.
    """
    if len(shapes) < 2:
        return False

    for i, s1 in enumerate(shapes):
        for s2 in shapes[i + 1:]:
            same_row = _shapes_vertically_overlap(s1, s2)
            side_by_side = not _shapes_overlap_horizontally(s1, s2)
            if same_row and side_by_side:
                return True

    # Detect multi-column / grid: cluster left edges into X bands (tolerance 60px).
    tol = 60
    lefts = sorted(int(s.left) for s in shapes if s.left is not None)
    if len(lefts) >= 2:
        bands = 1
        for a, b in zip(lefts, lefts[1:]):
            if b - a > tol:
                bands += 1
        if bands > 1:
            return True

    return False


def _auto_relayout_dense(pptx_path: str, dense_slides: list[int], edited_slides: set[int]) -> list[dict]:
    """
    Phase 4a: re-distribute text shapes on dense slides ONLY if:
      - the slide has a single vertical stack (no columns / grid)
      - shapes actually overlap each other after edits (broken layout)

    Slides with side-by-side columns are skipped — reflowing them would destroy the design.
    """
    results = []
    if not dense_slides:
        return results
    try:
        prs = load(pptx_path)
    except Exception:
        return [{"ok": False, "error": "could not open file for relayout"}]

    for slide_idx in dense_slides:
        if slide_idx not in edited_slides:
            continue
        if slide_idx < 1 or slide_idx > len(prs.slides):
            continue
        slide = prs.slides[slide_idx - 1]

        text_shape_objs = [
            shape for shape in slide.shapes
            if shape.has_text_frame and shape.top is not None and shape.height is not None
            and shape.width and round(shape.width * 96 / 914400) >= 80
        ]
        if len(text_shape_objs) < 2:
            continue

        # Skip if slide has columns / grid layout — vertical reflow would break it
        if _detect_columns(text_shape_objs):
            results.append({"slide": slide_idx, "skipped": "column_layout_detected"})
            continue

        # Only reflow if shapes actually overlap each other (genuinely broken)
        sorted_shapes = sorted(text_shape_objs, key=lambda s: int(s.top))
        has_overlap = any(
            _shapes_vertically_overlap(sorted_shapes[i], sorted_shapes[i + 1])
            for i in range(len(sorted_shapes) - 1)
        )
        if not has_overlap:
            results.append({"slide": slide_idx, "skipped": "no_overlap_to_fix"})
            continue

        order = [s.name for s in sorted_shapes]
        r = op_relayout_slide(pptx_path, slide_idx, order)
        results.append({"slide": slide_idx, **r})
    return results


def executor_node(state: dict) -> dict:
    provider = state.get("provider", "anthropic")
    fix_attempts = state.get("fix_attempts", 0)
    is_fix_pass = fix_attempts > 0

    # Fix pass uses the stronger planner model (Sonnet)
    if is_fix_pass:
        model = state.get("planner_model", "claude-sonnet-4-6")
    else:
        model = state.get("executor_model", "claude-haiku-4-5-20251001")

    out = state["output_path"]

    # First pass: copy the template
    if not is_fix_pass:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(state["template_path"], out)
        except Exception as e:
            raise RuntimeError(f"Cannot copy template to output path: {e}") from e

    edit_plan = state.get("edit_plan", {})
    edits = list(edit_plan.get("edits", []))
    slides_to_remove = list(edit_plan.get("slides_to_remove", []))
    dense_slides = list(edit_plan.get("dense_slides", []))
    delete_ops = list(edit_plan.get("delete_shapes", []))
    slides_to_add = list(edit_plan.get("slides_to_add", []) or [])

    # Fix pass: filter edits to only flagged slides
    flagged: set[int] = set()
    if is_fix_pass and state.get("review_issues"):
        flagged = _extract_flagged_slides(state["review_issues"])
        if flagged:
            edits = [e for e in edits if e.get("slide") in flagged]
        slides_to_remove = []  # never re-attempt removals on fix pass
        slides_to_add = []

    phase_log: dict = {"phases": []}

    # ── Step 0a: restore empty titles from template (fix pass only) ──────────
    if is_fix_pass and state.get("review_issues"):
        restore_result = _restore_empty_titles(
            out, state["template_path"], state["review_issues"]
        )
        phase_log["title_restore"] = restore_result

    # ── Step 0: structural ops — removals + additions, with index remap ────
    # Runs BEFORE any edit. op_apply_structure clones base slides and removes
    # slides in one pass, then returns a map ORIGINAL index → FINAL position.
    # All downstream references (edits, delete_shapes, dense_slides) are
    # remapped so they land on the right slides in the FINAL deck — this also
    # fixes the old bug where edits after a removed slide landed one off.
    if (slides_to_remove or slides_to_add) and not is_fix_pass:
        struct = op_apply_structure(out, slides_to_remove, slides_to_add)
        struct_log = {
            "removed": struct.get("removed", []),
            "added": struct.get("added", []),
        }
        if not struct.get("ok"):
            struct_log["error"] = struct.get("error", "structure apply failed")
        phase_log["structure"] = struct_log

        if struct.get("ok"):
            imap = struct["index_map"]

            def _remap(v):
                if isinstance(v, bool):
                    return v
                if isinstance(v, int):
                    return imap.get(f"orig:{v}", v)
                s = str(v)
                return imap.get(f"new:{s}", imap.get(f"orig:{s}", v))

            edits = [
                {**e, "slide": _remap(e["slide"])} if e.get("slide") is not None else e
                for e in edits
            ]
            delete_ops = [
                {**op_, "slide": _remap(op_["slide"])} if op_.get("slide") is not None else op_
                for op_ in delete_ops
            ]
            dense_slides = [_remap(s) for s in dense_slides]
            # Downstream stages (reviewer pre-check, fix pass) run against the
            # FINAL deck — store the remapped plan so indices stay consistent.
            edit_plan = {
                **edit_plan,
                "edits": edits,
                "delete_shapes": delete_ops,
                "dense_slides": dense_slides,
                "slides_to_remove": [],
                "slides_to_add": [],
            }
        slides_to_remove = []
        slides_to_add = []

    # ── Step 0b: deterministic shape group deletions (no LLM) ────────────────
    if delete_ops:
        ops_to_run = delete_ops
        if is_fix_pass:
            # Re-apply only for slides flagged by reviewer (orphan shape cleanup)
            ops_to_run = [op for op in delete_ops if op.get("slide") in flagged] if flagged else []
        if ops_to_run:
            deletion_log = []
            for op in ops_to_run:
                slide_idx = op.get("slide")
                shapes = op.get("shapes", [])
                if slide_idx and shapes:
                    r = op_delete_shapes(out, slide_idx, shapes)
                    deletion_log.append({"slide": slide_idx, **r})
            phase_log["shape_deletions"] = deletion_log

    # ── Phase 1: preflight shape resolution (no LLM) ──────────────────────
    edits_prioritised = _prioritise_edits(edits)
    resolved, unresolvable_preflight = _preflight_resolve(out, edits_prioritised)
    phase_log["phases"].append({
        "phase": 1,
        "name": "preflight",
        "resolved": len(resolved),
        "unresolvable": len(unresolvable_preflight),
        "fuzzy_resolutions": [
            {"slide": e["slide"], "from": e["_original_shape"], "to": e["shape"],
             "confidence": e.get("_resolution_confidence")}
            for e in resolved if "_original_shape" in e
        ],
    })

    # ── Phase 2: deterministic batch apply (no LLM) ───────────────────────
    if resolved:
        clean_edits = [{k: v for k, v in e.items() if not k.startswith("_")} for e in resolved]
        try:
            batch = op_apply_edits_batch(out, clean_edits, autofit=True)
        except Exception as e:
            batch = {"ok": False, "applied": 0, "failed": len(clean_edits), "results": [],
                     "error": str(e)}
            phase_log["phases"].append({"phase": 2, "name": "batch_apply", "error": str(e)})
    else:
        batch = {"ok": True, "applied": 0, "failed": 0, "results": []}

    phase_log["phases"].append({
        "phase": 2,
        "name": "batch_apply",
        "applied": batch.get("applied", 0),
        "failed": batch.get("failed", 0),
    })

    # Collect edits that failed in batch apply
    failed_in_batch = []
    for r in batch.get("results", []):
        if not r.get("ok"):
            idx = r.get("index")
            if idx is not None and idx < len(resolved):
                failed_in_batch.append({
                    **resolved[idx],
                    "phase2_error": r.get("error"),
                    "available_names": r.get("available_names", []),
                })

    needs_llm = unresolvable_preflight + failed_in_batch

    # ── Phase 3: LLM fallback (only if needed) ────────────────────────────
    llm_used = False
    llm_result = {}
    if needs_llm:
        llm_used = True
        # Build a focused fix-list for the LLM
        fix_list = json.dumps(needs_llm, indent=2)
        prompt = (
            f"Repair these unresolved edits in the PPTX.\n\n"
            f"PPTX path: {out}\n\n"
            f"Each edit lists why phase 2 failed (preflight_reason or phase2_error) "
            f"and any available shape names on its slide.\n\n"
            f"Failed edits ({len(needs_llm)}):\n{fix_list}\n\n"
            f"Use pptx_apply_edits_batch where possible. For each edit: "
            f"call pptx_resolve_shape on its slide if needed, then batch-apply with the corrected shape names."
        )
        try:
            result = _get_agent(provider, model).invoke(
                {"messages": [HumanMessage(content=prompt)]}, config=_CONFIG
            )
            # Capture the agent's final text so the fix is not assumed-silent.
            final_msg = result.get("messages", [])[-1]
            final_text = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            if isinstance(final_text, list):
                final_text = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in final_text if not (isinstance(b, dict) and b.get("type") == "thinking")
                )
            llm_result = {"final_message": (final_text or "")[:2000]}
        except Exception as e:
            llm_result = {"error": str(e)}
            phase_log["phases"].append({"phase": 3, "name": "llm_fallback", "error": str(e)})

        # Re-verify the previously-failed targets after the LLM pass, so we
        # actually know what got fixed instead of trusting the agent's word.
        resolved_after_llm = _preflight_resolve(out, [e for e in needs_llm if e.get("slide") and e.get("shape")])[0]
        if resolved_after_llm:
            try:
                verify_after = op_verify_edits(out, [
                    {k: v for k, v in e.items() if not k.startswith("_")}
                    for e in resolved_after_llm
                ])
                llm_result["verified"] = verify_after.get("verified", 0)
                llm_result["still_mismatched"] = len(verify_after.get("mismatches", []))
            except Exception as e:
                llm_result["verify_error"] = str(e)

        phase_log["phases"].append({
            "phase": 3, "name": "llm_fallback",
            "target_count": len(needs_llm),
            "result": llm_result,
        })
    else:
        phase_log["phases"].append({"phase": 3, "name": "llm_fallback", "skipped": True})

    # ── Phase 4a: auto-relayout dense slides (no LLM) ─────────────────────
    edited_slide_set = {e.get("slide") for e in edits if e.get("slide")}
    if dense_slides:
        relayout_results = _auto_relayout_dense(out, dense_slides, edited_slide_set)
        phase_log["phases"].append({
            "phase": "4a", "name": "auto_relayout",
            "results": relayout_results,
        })

    # ── Phase 4b: placeholder sweep (no LLM) ──────────────────────────────
    sweep_targets = sorted(edited_slide_set) or None
    try:
        sweep = op_placeholder_sweep(out, only_slides=sweep_targets, replacement=" ")
        phase_log["phases"].append({
            "phase": "4b", "name": "placeholder_sweep",
            "swept": sweep.get("swept", 0),
        })
    except Exception as e:
        phase_log["phases"].append({"phase": "4b", "name": "placeholder_sweep", "error": str(e)})

    # ── Phase 4c: verification (no LLM) ───────────────────────────────────
    verify_edits = [{k: v for k, v in e.items() if not k.startswith("_")} for e in resolved]
    try:
        verification = op_verify_edits(out, verify_edits)
        phase_log["phases"].append({
            "phase": "4c", "name": "verify",
            "verified": verification.get("verified", 0),
            "mismatches": len(verification.get("mismatches", [])),
        })
    except Exception as e:
        phase_log["phases"].append({"phase": "4c", "name": "verify", "error": str(e)})

    # ── Phase 4d: fit pass — guarantee edited shapes don't overflow (no LLM) ──
    # Safety net that ANY write path (batch apply, LLM fallback tools, set_para,
    # fix pass) feeds into. Reads back each edited shape's ACTUAL text, measures
    # its real capacity (incl. wrap='none' + inherited font size), and shrinks
    # the run font until it fits. Never truncates content.
    fit_targets = [
        {"slide": e.get("slide"), "shape": e.get("shape")}
        for e in edits if e.get("slide") and e.get("shape")
    ]
    if fit_targets:
        try:
            fit = op_fit_text_overflow(out, fit_targets)
            phase_log["phases"].append({
                "phase": "4d", "name": "fit_overflow",
                "fitted": fit.get("fitted", 0),
                "checked": len(fit.get("results", [])),
            })
        except Exception as e:
            phase_log["phases"].append({"phase": "4d", "name": "fit_overflow", "error": str(e)})

    return {
        **state,
        "edit_plan": edit_plan,
        "fix_attempts": fix_attempts + 1,
        "executor_log": phase_log,
        "executor_llm_used": llm_used,
    }
