"""LangChain tool wrappers around the deterministic PPTX editing functions."""
import json

from langchain_core.tools import tool

from .pptx_editor import (
    load, save,
    op_info, op_spatial_map, op_check_overflow,
    op_set_text, op_set_para, op_remove_slides, op_delete_shapes,
    op_set_cell, op_set_position, op_validate, op_render_png, op_precheck,
    op_relayout_slide,
    op_resolve_shape_name, op_set_text_autofit,
    op_apply_edits_batch, op_verify_edits, op_placeholder_sweep, op_fit_text_overflow,
)
from .font_measurer import measure
from .layout_solver import solve


@tool
def pptx_info(pptx_path: str) -> str:
    """Get slide count and full shape inventory of a PPTX file as JSON."""
    prs = load(pptx_path)
    return json.dumps(op_info(prs), indent=2)


@tool
def pptx_spatial_map(pptx_path: str, slide: int) -> str:
    """
    Get spatial map for a slide — returns available_h_px, needs_expand, expand_to_emu,
    and EMU coords for every text shape. Run before set_position to know safe limits.
    slide: 1-based index.
    """
    prs = load(pptx_path)
    return json.dumps(op_spatial_map(prs, slide), indent=2)


@tool
def pptx_check_overflow(pptx_path: str, slide: int, shape: str,
                        text: str, font_size: int = 14) -> str:
    """
    Check whether text will overflow a shape before setting it.
    Returns overflows (bool) and overflow_by_px.
    slide: 1-based. font_size: points (default 14).
    """
    prs = load(pptx_path)
    return json.dumps(op_check_overflow(prs, slide, shape, text, font_size), indent=2)


@tool
def pptx_set_text(pptx_path: str, slide: int, shape: str, text: str) -> str:
    """
    Set text in a named shape on a slide (in-place edit).
    Each \\n in text creates a new paragraph. Use check_overflow first.
    slide: 1-based. shape: exact case-sensitive name from pptx_info.
    """
    prs = load(pptx_path)
    msg = op_set_text(prs, slide, shape, text)
    if not msg.startswith("ERROR"):
        save(prs, pptx_path)
    return msg


@tool
def pptx_set_para(pptx_path: str, slide: int, shape: str, para: int, text: str) -> str:
    """
    Replace a single paragraph in a text frame, preserving run-level formatting.
    slide: 1-based. para: 0-based paragraph index.
    """
    prs = load(pptx_path)
    msg = op_set_para(prs, slide, shape, para, text)
    if not msg.startswith("ERROR"):
        save(prs, pptx_path)
    return msg


@tool
def pptx_set_cell(pptx_path: str, slide: int, shape: str,
                  row: int, col: int, text: str) -> str:
    """
    Set text in a table cell.
    slide: 1-based. row, col: 0-based indices.
    """
    prs = load(pptx_path)
    msg = op_set_cell(prs, slide, shape, row, col, text)
    if not msg.startswith("ERROR"):
        save(prs, pptx_path)
    return msg


@tool
def pptx_remove_slides(pptx_path: str, slides: str) -> str:
    """
    Remove slides by comma-separated 1-based indices (e.g. '13,14').
    Handles index shifting automatically.
    """
    prs = load(pptx_path)
    indices = [int(x.strip()) for x in slides.split(",")]
    msg = op_remove_slides(prs, indices)
    if not msg.startswith("ERROR"):
        save(prs, pptx_path)
    return msg


@tool
def pptx_set_position(pptx_path: str, slide: int, shape: str,
                      top: int, left: int, width: int, height: int) -> str:
    """
    Move/resize a shape. All values in EMU (1 inch = 914400 EMU).
    Use spatial_map to get expand_to_emu before calling this.
    slide: 1-based.
    """
    prs = load(pptx_path)
    msg = op_set_position(prs, slide, shape, top, left, width, height)
    if not msg.startswith("ERROR"):
        save(prs, pptx_path)
    return msg


@tool
def pptx_validate(pptx_path: str) -> str:
    """
    Validate PPTX: scan for unfilled placeholder patterns (TODO, [X], Lorem ipsum, etc.).
    Returns JSON with ok (bool), slide_count, and unfilled_placeholders list.
    """
    return json.dumps(op_validate(pptx_path), indent=2)


@tool
def pptx_apply_edits_batch(pptx_path: str, edits_json: str, autofit: bool = True) -> str:
    """
    Apply many edits in one transaction. ~10× fewer LLM round-trips than per-shape calls.
    edits_json: JSON array of {slide, op, shape, text [, row, col, para]}.
    op ∈ {set_text, set_cell, set_para}.
    autofit: if true, set_text wraps in smart-truncate (preserves bullets + punchline).
    Auto-resolves fuzzy shape names. Returns JSON with per-edit status.
    """
    import json as _json
    edits = _json.loads(edits_json)
    return _json.dumps(op_apply_edits_batch(pptx_path, edits, autofit=autofit), indent=2)


@tool
def pptx_set_text_autofit(pptx_path: str, slide: int, shape: str, text: str,
                           font_size: int = 0) -> str:
    """
    Set text on a shape; if text would overflow, smart-truncate while preserving
    bullets and punchline endings. Does NOT shrink font (preserves template design).
    Use this instead of pptx_set_text when you're unsure about overflow.
    font_size=0 means use the shape's existing font size.
    """
    import json as _json
    prs = load(pptx_path)
    result = op_set_text_autofit(prs, slide, shape, text, font_size=font_size)
    if result.get("ok"):
        save(prs, pptx_path)
    return _json.dumps(result, indent=2)


@tool
def pptx_resolve_shape(pptx_path: str, slide: int, target_name: str) -> str:
    """
    Fuzzy-match a target shape name against actual shapes on a slide.
    Returns the best match + available alternatives. Use when a shape name from
    your plan doesn't match any actual shape exactly.
    """
    import json as _json
    prs = load(pptx_path)
    return _json.dumps(op_resolve_shape_name(prs, slide, target_name), indent=2)


@tool
def pptx_verify_edits(pptx_path: str, edits_json: str) -> str:
    """
    Re-open PPTX and verify each edit's target shape now contains the expected text.
    Returns list of mismatches (silent edit failures).
    edits_json: same format as apply_edits_batch.
    """
    import json as _json
    edits = _json.loads(edits_json)
    return _json.dumps(op_verify_edits(pptx_path, edits), indent=2)


@tool
def pptx_placeholder_sweep(pptx_path: str, only_slides_json: str = "[]",
                            replacement: str = "") -> str:
    """
    Deterministic sweep — replace any shape containing a leftover placeholder
    pattern ([X], TODO, Lorem, etc.) with `replacement` (default empty).
    only_slides_json: JSON array of slide indices, e.g. "[1,3,7]"; empty = all slides.
    """
    import json as _json
    only = _json.loads(only_slides_json) or None
    return _json.dumps(op_placeholder_sweep(pptx_path, only_slides=only, replacement=replacement), indent=2)


@tool
def pptx_fit_overflow(pptx_path: str, targets_json: str) -> str:
    """
    Deterministic safety net — ensure every listed shape's ACTUAL text fits its
    box by shrinking the font (floor 8pt). Use after applying edits if you're
    unsure whether text overflows. NEVER truncates content.
    targets_json: JSON array of [{"slide": int, "shape": "<name>"}, ...].
    Returns JSON with per-shape from_size/to_size + fitted count.
    """
    import json as _json
    targets = _json.loads(targets_json)
    return _json.dumps(op_fit_text_overflow(pptx_path, targets), indent=2)


@tool
def pptx_relayout_slide(pptx_path: str, slide: int, shape_order_json: str) -> str:
    """
    Re-distribute shapes on a dense slide vertically using the constraint solver.
    Call AFTER editing text on a dense slide if shape heights changed.
    slide: 1-based.
    shape_order_json: JSON array of shape names in top-to-bottom order, e.g. '["Title 1","Body 1","Body 2"]'.
    Only listed shapes are repositioned; others are left alone.
    Returns JSON with ok + applied positions.
    """
    import json as _json
    order = _json.loads(shape_order_json)
    return _json.dumps(op_relayout_slide(pptx_path, slide, order), indent=2)


@tool
def pptx_precheck(pptx_path: str, edit_plan_json: str) -> str:
    """
    Deterministic pre-QA check before visual review.
    Catches: unfilled placeholders, empty titles, empty edited shapes, likely overflow.
    edit_plan_json: JSON string of the edit plan dict.
    Returns JSON with ok, hard_failures, warn_count, and issues list.
    """
    import json as _json
    plan = _json.loads(edit_plan_json)
    return _json.dumps(op_precheck(pptx_path, plan), indent=2)


@tool
def pptx_render_png(pptx_path: str, output_dir: str) -> str:
    """
    Render all slides to PNG using LibreOffice (requires libreoffice in PATH).
    output_dir: directory where slide*.png files will be written.
    """
    return op_render_png(pptx_path, output_dir)


@tool
def pptx_delete_shapes(pptx_path: str, slide: int, shape_names: str) -> str:
    """
    Delete named shapes from a slide (text boxes, images, grouped shapes, connectors).
    Use when a template has more content slots than the source provides — delete the
    entire shape group (image + text together), not just blank the text.
    shape_names: JSON array of exact shape name strings, e.g. '["Picture 3","TextBox 7"]'
    slide: 1-based index.
    """
    names = json.loads(shape_names)
    return json.dumps(op_delete_shapes(pptx_path, slide, names), indent=2)


@tool
def font_measure(text: str, font_size: int, font_path: str = "") -> str:
    """
    Measure rendered text dimensions in pixels.
    font_path: optional path to .ttf file. Returns JSON with width, height, source.
    """
    return json.dumps(measure(text, font_size, font_path or None), indent=2)


@tool
def layout_solve(boxes: str, margin_top: int = 457200,
                 margin_left: int = 457200, gap: int = 228600) -> str:
    """
    Solve vertical stacking layout.
    boxes: JSON array like [{"name":"title","h":800000},{"name":"body","h":2000000}].
    Returns list of {name, top, left, width, height} in EMU for use with set_position.
    """
    boxes_list = json.loads(boxes)
    result = solve(9144000, 5143500, boxes_list, margin_top, margin_left, margin_left, gap)
    return json.dumps(result, indent=2)
