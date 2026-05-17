"""LangChain tool wrappers around the deterministic PPTX editing functions."""
import json

from langchain_core.tools import tool

from .pptx_editor import (
    load, save,
    op_info, op_spatial_map, op_check_overflow,
    op_set_text, op_set_para, op_remove_slides,
    op_set_cell, op_set_position, op_validate, op_render_png, op_precheck,
    op_relayout_slide,
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
