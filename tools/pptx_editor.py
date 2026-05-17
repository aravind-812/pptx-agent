#!/usr/bin/env python3
"""Deterministic PPTX editing CLI used by the executor subagent.

Operations (pass --op):
  info          Print slide count and shape inventory as JSON
  set_text      Set text of a named shape on a slide
  set_para      Replace paragraph text in a text frame (preserves formatting)
  remove_slide  Delete a slide by 1-based index
  remove_slides Remove multiple slides (comma-separated indices, descending order)
  set_cell      Set text in a table cell
  set_position  Move/resize a shape using pre-solved coordinates (EMU)
  render_png    Convert PPTX to PNG via LibreOffice (requires libreoffice in PATH)
  validate      Check slide count and scan for forbidden strings
"""
import argparse
import json
import re
import sys
import zipfile
from copy import deepcopy
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.util import Emu, Pt

_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'


PLACEHOLDER_PATTERNS = [
    r"\[.*?\]",           # [CUSTOMER_NAME], [DATE], etc.
    r"\{\{.*?\}\}",       # {{variable}} template syntax
    r"<Enter [^>]+>",     # <Enter something here>
    r"Click to edit",
    r"Lorem ipsum",
    r"\bTODO\b",
    r"Your .{1,30} here",
    r"Sample [A-Z]",
]

# Generic corporate filler phrases. Flagged ONLY if absent from the user's
# source document — these signal the model paraphrased specifics into vagueness.
GENERIC_PHRASES = [
    "industry-leading", "industry leading",
    "best-in-class", "best in class",
    "world-class", "world class",
    "cutting-edge", "cutting edge",
    "state-of-the-art", "state of the art",
    "robust solution", "scalable solution", "innovative solution",
    "synergy", "synergies",
    "leverage", "leveraging",
    "seamless integration", "seamless experience",
    "next-generation", "next generation",
    "mission-critical",
    "best practices",
    "thought leader", "thought leadership",
    "value proposition",
]


def load(path: str) -> Presentation:
    return Presentation(path)


def save(prs: Presentation, path: str):
    prs.save(path)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------
def op_info(prs: Presentation) -> dict:
    slides = []
    for i, slide in enumerate(prs.slides, 1):
        shapes = []
        for shape in slide.shapes:
            entry = {
                "name": shape.name,
                "shape_id": shape.shape_id,
                "type": str(shape.shape_type),
                "width_emu": shape.width,
                "height_emu": shape.height,
                "width_px": round(shape.width * 96 / 914400) if shape.width else None,
                "height_px": round(shape.height * 96 / 914400) if shape.height else None,
            }
            if shape.has_text_frame:
                entry["text_preview"] = shape.text_frame.text[:120]
            if shape.has_table:
                entry["table_rows"] = len(shape.table.rows)
                entry["table_cols"] = len(shape.table.columns)
            shapes.append(entry)
        slides.append({"slide": i, "shapes": shapes})
    return {"slide_count": len(prs.slides), "slides": slides}


def op_spatial_map(prs: Presentation, slide_idx: int) -> list[dict]:
    """
    For every text shape on a slide, compute the safe available height —
    the vertical gap to the nearest shape below it in the same column.
    This tells the executor how tall it can safely expand a shape before
    it would collide with the next element.

    Returns shapes sorted top-to-bottom with:
      available_h_px: safe usable height (gap to next shape in same column - 8px margin)
      needs_expand: True if declared height < available height (shape should be expanded)
    """
    MARGIN = 8  # px safety gap to leave between shapes
    COLUMN_TOLERANCE = 40  # px: shapes within this x-range are considered same column
    MIN_WIDTH_PX = 80  # ignore tiny shapes (numbered circles, connectors, arrows)

    slide = prs.slides[slide_idx - 1]
    shapes_raw = []
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        if shape.top is None or shape.height is None or shape.width is None:
            continue
        if round(shape.width * 96 / 914400) < MIN_WIDTH_PX:
            continue  # skip tiny circles, arrows, connectors
        shapes_raw.append({
            "name": shape.name,
            "top_px": round(shape.top * 96 / 914400),
            "height_px": round(shape.height * 96 / 914400),
            "left_px": round(shape.left * 96 / 914400),
            "width_px": round(shape.width * 96 / 914400),
            "top_emu": shape.top,
            "height_emu": shape.height,
            "left_emu": shape.left,
            "width_emu": shape.width,
        })

    shapes_raw.sort(key=lambda s: s["top_px"])

    result = []
    for i, s in enumerate(shapes_raw):
        # Find the nearest shape below in roughly the same column
        next_top = None
        for j in range(i + 1, len(shapes_raw)):
            other = shapes_raw[j]
            if abs(other["left_px"] - s["left_px"]) <= COLUMN_TOLERANCE:
                next_top = other["top_px"]
                break

        if next_top is not None:
            available_h_px = max(s["height_px"], next_top - s["top_px"] - MARGIN)
        else:
            available_h_px = s["height_px"]  # last in column — keep declared height

        result.append({
            "name": s["name"],
            "top_px": s["top_px"],
            "height_px": s["height_px"],
            "available_h_px": available_h_px,
            "needs_expand": available_h_px > s["height_px"],
            "expand_to_emu": round(available_h_px * 914400 / 96),
            "left_px": s["left_px"],
            "width_px": s["width_px"],
            "top_emu": s["top_emu"],
            "left_emu": s["left_emu"],
            "width_emu": s["width_emu"],
        })

    return result


def op_check_overflow(prs: Presentation, slide_idx: int, shape_name: str,
                      text: str, font_size: int) -> dict:
    """Estimate whether text will overflow a shape before setting it."""
    slide = prs.slides[slide_idx - 1]
    for shape in slide.shapes:
        if shape.name == shape_name:
            shape_w_px = round(shape.width * 96 / 914400) if shape.width else 0
            shape_h_px = round(shape.height * 96 / 914400) if shape.height else 0
            chars_per_line = max(1, shape_w_px // max(1, int(font_size * 0.55)))
            lines = sum(
                max(1, -(-len(p) // chars_per_line))
                for p in text.split("\n")
            )
            estimated_h = int(lines * font_size * 1.3)
            overflows = estimated_h > shape_h_px
            return {
                "shape": shape_name,
                "shape_w_px": shape_w_px,
                "shape_h_px": shape_h_px,
                "estimated_text_h_px": estimated_h,
                "overflows": overflows,
                "overflow_by_px": max(0, estimated_h - shape_h_px),
            }
    return {"error": f"shape '{shape_name}' not found on slide {slide_idx}"}


# ---------------------------------------------------------------------------
# set_text  — creates one proper paragraph per line, preserving run formatting
# ---------------------------------------------------------------------------
def op_set_text(prs: Presentation, slide_idx: int, shape_name: str, text: str) -> str:
    slide = prs.slides[slide_idx - 1]
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_text_frame:
            tf = shape.text_frame
            txBody = tf._txBody
            lines = text.split('\n')

            # Locate a reference paragraph with runs to copy formatting from
            ref_para_elem = None
            ref_run_elem = None
            for p in tf.paragraphs:
                if p.runs:
                    ref_para_elem = p._p
                    ref_run_elem = p._p.findall(f'{{{_NS}}}r')[0]
                    break

            # Remove all existing <a:p> elements
            for p_elem in txBody.findall(f'{{{_NS}}}p'):
                txBody.remove(p_elem)

            # Re-add one <a:p> per line with proper run formatting
            for line in lines:
                if ref_para_elem is not None:
                    new_p = deepcopy(ref_para_elem)
                    # Strip all runs from the copy
                    for r in new_p.findall(f'{{{_NS}}}r'):
                        new_p.remove(r)
                    # Add a single run carrying the line text
                    new_r = deepcopy(ref_run_elem)
                    t_elem = new_r.find(f'{{{_NS}}}t')
                    if t_elem is None:
                        t_elem = etree.SubElement(new_r, f'{{{_NS}}}t')
                    t_elem.text = line
                    new_p.append(new_r)
                else:
                    new_p = etree.Element(f'{{{_NS}}}p')
                    new_r = etree.SubElement(new_p, f'{{{_NS}}}r')
                    new_t = etree.SubElement(new_r, f'{{{_NS}}}t')
                    new_t.text = line
                txBody.append(new_p)

            return f"Set text ({len(lines)} lines) on slide {slide_idx} shape '{shape_name}'"
    return f"ERROR: shape '{shape_name}' not found on slide {slide_idx}"


# ---------------------------------------------------------------------------
# set_para — replace full paragraphs, preserving run-level formatting
# ---------------------------------------------------------------------------
def op_set_para(prs: Presentation, slide_idx: int, shape_name: str,
                para_idx: int, text: str) -> str:
    slide = prs.slides[slide_idx - 1]
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_text_frame:
            paras = shape.text_frame.paragraphs
            if para_idx >= len(paras):
                return f"ERROR: paragraph index {para_idx} out of range (slide has {len(paras)} paras)"
            para = paras[para_idx]
            if para.runs:
                para.runs[0].text = text
                for r in para.runs[1:]:
                    r.text = ""
            else:
                para.text = text
            return f"Set para {para_idx} on slide {slide_idx} shape '{shape_name}'"
    return f"ERROR: shape '{shape_name}' not found on slide {slide_idx}"


# ---------------------------------------------------------------------------
# remove_slide
# ---------------------------------------------------------------------------
def op_remove_slide(prs: Presentation, slide_idx: int) -> str:
    xml_slides = prs.slides._sldIdLst
    slides = prs.slides
    if slide_idx < 1 or slide_idx > len(slides):
        return f"ERROR: slide index {slide_idx} out of range (1–{len(slides)})"
    slide = slides[slide_idx - 1]
    rId = slides._sldIdLst[slide_idx - 1].get("r:id")
    slides._sldIdLst.remove(slides._sldIdLst[slide_idx - 1])
    del prs.part.related_parts[rId]
    return f"Removed slide {slide_idx}"


# ---------------------------------------------------------------------------
# remove_slides (multiple, largest index first to avoid index shifting)
# ---------------------------------------------------------------------------
def op_remove_slides(prs: Presentation, indices: list[int]) -> str:
    for idx in sorted(set(indices), reverse=True):
        msg = op_remove_slide(prs, idx)
        if msg.startswith("ERROR"):
            return msg
    return f"Removed slides {sorted(indices)}"


# ---------------------------------------------------------------------------
# set_cell
# ---------------------------------------------------------------------------
def op_set_cell(prs: Presentation, slide_idx: int, shape_name: str,
                row: int, col: int, text: str) -> str:
    slide = prs.slides[slide_idx - 1]
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_table:
            cell = shape.table.cell(row, col)
            if cell.text_frame.paragraphs and cell.text_frame.paragraphs[0].runs:
                cell.text_frame.paragraphs[0].runs[0].text = text
            else:
                cell.text_frame.paragraphs[0].text = text
            return f"Set cell ({row},{col}) on slide {slide_idx} shape '{shape_name}'"
    return f"ERROR: table shape '{shape_name}' not found on slide {slide_idx}"


# ---------------------------------------------------------------------------
# set_position
# ---------------------------------------------------------------------------
def op_set_position(prs: Presentation, slide_idx: int, shape_name: str,
                    top: int, left: int, width: int, height: int) -> str:
    slide = prs.slides[slide_idx - 1]
    for shape in slide.shapes:
        if shape.name == shape_name:
            shape.top = Emu(top)
            shape.left = Emu(left)
            shape.width = Emu(width)
            shape.height = Emu(height)
            return f"Repositioned '{shape_name}' on slide {slide_idx}"
    return f"ERROR: shape '{shape_name}' not found on slide {slide_idx}"


# ---------------------------------------------------------------------------
# render_png (requires libreoffice)
# ---------------------------------------------------------------------------
def op_render_png(pptx_path: str, output_dir: str) -> str:
    import subprocess, shutil
    lo = shutil.which("libreoffice") or shutil.which("soffice")
    if not lo:
        return "ERROR: libreoffice not found in PATH — install LibreOffice for visual QA"
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(
            [lo, "--headless", "--convert-to", "pdf", "--outdir", tmp, pptx_path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return f"ERROR: libreoffice conversion failed:\n{r.stderr}"
        pdfs = list(Path(tmp).glob("*.pdf"))
        if not pdfs:
            return "ERROR: no PDF produced by libreoffice"
        pdf = pdfs[0]
        r2 = subprocess.run(
            ["pdftoppm", "-png", "-r", "120", str(pdf), str(out / "slide")],
            capture_output=True, text=True, timeout=120,
        )
        if r2.returncode != 0:
            return f"ERROR: pdftoppm failed:\n{r2.stderr}"
    pngs = sorted(out.glob("slide*.png"))
    return f"Rendered {len(pngs)} slides to {output_dir}"


# ---------------------------------------------------------------------------
# relayout — constraint-solver based re-distribution for dense slides
# ---------------------------------------------------------------------------
def op_relayout_slide(pptx_path: str, slide_idx: int,
                       shape_order: list[str],
                       margin_top_emu: int = 457200,
                       margin_left_emu: int = 457200,
                       gap_emu: int = 137160) -> dict:
    """
    Re-distribute named shapes vertically on a slide using the layout solver.
    Use after edits change shape heights and other shapes need to reflow.

    shape_order: list of shape names in the order they should stack top→bottom.
                 Only these shapes are repositioned; others are left alone.
    margin_top_emu / margin_left_emu / gap_emu: spacing in EMU.

    Reads current heights from PPTX, solves vertical positions, applies set_position.
    """
    from .layout_solver import solve

    try:
        prs = load(pptx_path)
        if slide_idx < 1 or slide_idx > len(prs.slides):
            return {"ok": False, "error": f"slide {slide_idx} out of range"}

        slide = prs.slides[slide_idx - 1]
        slide_w = prs.slide_width
        slide_h = prs.slide_height

        # Collect target shapes in the requested order
        by_name = {shape.name: shape for shape in slide.shapes}
        boxes = []
        missing = []
        for name in shape_order:
            if name not in by_name:
                missing.append(name)
                continue
            shape = by_name[name]
            if not shape.height:
                continue
            boxes.append({"name": name, "h": int(shape.height)})

        if missing:
            return {"ok": False, "error": f"shapes not found: {missing}"}
        if not boxes:
            return {"ok": False, "error": "no valid shapes to lay out"}

        layout = solve(slide_w, slide_h, boxes,
                       margin_top=margin_top_emu,
                       margin_left=margin_left_emu,
                       margin_right=margin_left_emu,
                       gap=gap_emu)

        # Apply new positions, preserving each shape's original width
        applied = []
        for item in layout:
            shape = by_name[item["name"]]
            original_left = int(shape.left) if shape.left else item["left"]
            original_width = int(shape.width) if shape.width else item["width"]
            msg = op_set_position(prs, slide_idx, item["name"],
                                  top=item["top"], left=original_left,
                                  width=original_width, height=item["height"])
            if msg.startswith("ERROR"):
                return {"ok": False, "error": msg, "applied": applied}
            applied.append({
                "shape": item["name"],
                "top": item["top"],
                "height": item["height"],
            })

        save(prs, pptx_path)
        return {"ok": True, "applied": applied, "slide": slide_idx}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# precheck — deterministic QA before LLM review
# ---------------------------------------------------------------------------
def op_precheck(pptx_path: str, edit_plan: dict, source_document: str = "") -> dict:
    """
    Run deterministic checks on the edited PPTX.
    Returns hard failures that don't need LLM judgement.
    Checks:
      1. Unfilled placeholder patterns (XML scan)
      2. Empty title/heading shapes on edited slides
      3. Empty body shapes that were explicitly edited
      4. Likely text overflow (heuristic: estimated lines vs shape height)
      5. Source-grounding: numbers/percentages in edits that don't appear in source document
    """
    issues = []

    try:
        prs = load(pptx_path)
        slide_count = len(prs.slides)

        # 1. Placeholder scan via XML
        val = op_validate(pptx_path)
        for hit in val.get("unfilled_placeholders", []):
            issues.append({"type": "unfilled_placeholder", "severity": "hard", "detail": hit})

        # Build set of (slide_num, shape_name) pairs that were edited
        edited: dict[int, set[str]] = {}
        for e in edit_plan.get("edits", []):
            sn = e.get("slide")
            sh = e.get("shape")
            if sn and sh:
                edited.setdefault(sn, set()).add(sh)

        for slide_idx, slide in enumerate(prs.slides, 1):
            if slide_idx not in edited:
                continue

            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue

                tf = shape.text_frame
                text = tf.text.strip()
                name = shape.name
                name_lower = name.lower()

                # 2. Empty title on edited slide
                if "title" in name_lower or "heading" in name_lower:
                    if not text:
                        issues.append({
                            "type": "empty_title",
                            "severity": "hard",
                            "detail": f"slide {slide_idx} / '{name}': title shape is empty after edit",
                        })

                # 3. Shape explicitly edited but now empty
                if name in edited.get(slide_idx, set()) and not text:
                    issues.append({
                        "type": "empty_edited_shape",
                        "severity": "hard",
                        "detail": f"slide {slide_idx} / '{name}': was edited but is now empty",
                    })

                # 4. Rough overflow heuristic
                if shape.height and shape.width and text:
                    height_px = shape.height * 96 // 914400
                    width_px  = shape.width  * 96 // 914400
                    chars_per_line = max(1, width_px // 7)   # ~7px per char
                    line_height_px = 20

                    total_lines = 0
                    for para in text.split("\n"):
                        total_lines += max(1, (len(para) + chars_per_line - 1) // chars_per_line)

                    est_height = total_lines * line_height_px
                    if height_px > 0 and est_height > height_px * 0.95:
                        issues.append({
                            "type": "likely_overflow",
                            "severity": "warn",
                            "detail": (
                                f"slide {slide_idx} / '{name}': "
                                f"~{total_lines} lines estimated, shape is {height_px}px tall"
                            ),
                        })

        # 5. Source-grounding + sanitization checks on edit plan text
        if source_document:
            src_norm = source_document.lower()
            src_digits = src_norm.replace(",", "")
            num_pattern = re.compile(r"\b\d[\d,]*\.?\d*\s*%?\b")

            for e in edit_plan.get("edits", []):
                txt = (e.get("text") or "").strip()
                if not txt:
                    continue
                slide_no = e.get("slide")
                shape_name = e.get("shape", "?")
                txt_lower = txt.lower()

                # 5a. Ungrounded numbers / percentages
                for m in num_pattern.finditer(txt):
                    token = m.group(0).strip()
                    bare = token.replace(",", "").rstrip("%").strip()
                    if len(bare) < 2 and "%" not in token:
                        continue
                    if bare.lower() not in src_digits:
                        issues.append({
                            "type": "ungrounded_number",
                            "severity": "warn",
                            "detail": (
                                f"slide {slide_no} / '{shape_name}': '{token}' "
                                f"not found in source"
                            ),
                        })

                # 5b. Generic corporate filler not in source = sanitization
                for phrase in GENERIC_PHRASES:
                    if phrase in txt_lower and phrase not in src_norm:
                        issues.append({
                            "type": "sanitization",
                            "severity": "warn",
                            "detail": (
                                f"slide {slide_no} / '{shape_name}': generic phrase "
                                f"'{phrase}' added by model — user never used it"
                            ),
                        })

            # 5c. Content wastage — high-signal source facts that appear nowhere in edits
            edits_blob = " ".join((e.get("text") or "").lower() for e in edit_plan.get("edits", []))
            ctx = edit_plan.get("strategic_context", {})
            for fact in (ctx.get("all_key_facts") or []):
                if not isinstance(fact, str) or len(fact) < 8:
                    continue
                # Extract the most distinctive token (longest word ≥6 chars) from the fact
                tokens = [w.strip(".,!?:;\"'()") for w in fact.split()]
                distinctive = [t for t in tokens if len(t) >= 6 and not t.isdigit()]
                if not distinctive:
                    continue
                anchor = max(distinctive, key=len).lower()
                if anchor not in edits_blob:
                    issues.append({
                        "type": "content_wastage",
                        "severity": "warn",
                        "detail": f"source fact never used in deck: '{fact[:80]}'",
                    })

    except Exception as exc:
        return {"ok": False, "slide_count": 0, "issues": [], "error": str(exc)}

    hard = [i for i in issues if i["severity"] == "hard"]
    return {
        "ok": len(hard) == 0,
        "slide_count": slide_count,
        "hard_failures": len(hard),
        "warn_count": len(issues) - len(hard),
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------
def op_validate(pptx_path: str) -> dict:
    hits = []
    slide_count = None
    try:
        with zipfile.ZipFile(pptx_path) as z:
            slide_names = sorted(
                [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                key=lambda n: int(re.search(r"slide(\d+)\.xml", n).group(1)),
            )
            slide_count = len(slide_names)
            compiled = [re.compile(p, re.IGNORECASE) for p in PLACEHOLDER_PATTERNS]
            for name in slide_names:
                data = z.read(name).decode("utf-8", errors="ignore")
                slide_no = re.search(r"slide(\d+)\.xml", name).group(1)
                for pat in compiled:
                    m = pat.search(data)
                    if m:
                        hits.append(f"slide {slide_no}: unfilled placeholder '{m.group(0)[:40]}'")
    except Exception as e:
        return {"error": str(e)}
    ok = slide_count is not None and not hits
    return {"slide_count": slide_count, "unfilled_placeholders": hits, "ok": ok}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Deterministic PPTX editor")
    ap.add_argument("--pptx", required=True, help="Path to PPTX file (read/write)")
    ap.add_argument("--op", required=True, choices=[
        "info", "set_text", "set_para", "remove_slide", "remove_slides",
        "set_cell", "set_position", "render_png", "validate", "check_overflow",
        "spatial_map",
    ])
    ap.add_argument("--out", help="Output PPTX path (defaults to --pptx for in-place edits)")
    ap.add_argument("--slide", type=int, help="1-based slide index")
    ap.add_argument("--slides", help="Comma-separated 1-based slide indices for remove_slides")
    ap.add_argument("--shape", help="Shape name (case-sensitive)")
    ap.add_argument("--text", help="Text to set")
    ap.add_argument("--para", type=int, default=0, help="0-based paragraph index for set_para")
    ap.add_argument("--row", type=int, help="0-based table row")
    ap.add_argument("--col", type=int, help="0-based table column")
    ap.add_argument("--top", type=int, help="Top position in EMU")
    ap.add_argument("--left", type=int, help="Left position in EMU")
    ap.add_argument("--width", type=int, help="Width in EMU")
    ap.add_argument("--height", type=int, help="Height in EMU")
    ap.add_argument("--output-dir", help="Output directory for render_png")
    ap.add_argument("--font-size", type=int, default=14, help="Font size in pt for check_overflow")
    args = ap.parse_args()

    # Interpret literal \n sequences from shell as real newlines
    if args.text:
        args.text = args.text.replace('\\n', '\n')

    out_path = args.out or args.pptx

    if args.op == "validate":
        print(json.dumps(op_validate(args.pptx), indent=2))
        return

    if args.op == "render_png":
        print(op_render_png(args.pptx, args.output_dir or "rendered"))
        return

    if args.op == "info":
        prs = load(args.pptx)
        print(json.dumps(op_info(prs), indent=2))
        return

    if args.op == "check_overflow":
        prs = load(args.pptx)
        print(json.dumps(op_check_overflow(prs, args.slide, args.shape, args.text, args.font_size), indent=2))
        return

    if args.op == "spatial_map":
        prs = load(args.pptx)
        print(json.dumps(op_spatial_map(prs, args.slide), indent=2))
        return

    prs = load(args.pptx)

    if args.op == "set_text":
        msg = op_set_text(prs, args.slide, args.shape, args.text)
    elif args.op == "set_para":
        msg = op_set_para(prs, args.slide, args.shape, args.para, args.text)
    elif args.op == "remove_slide":
        msg = op_remove_slide(prs, args.slide)
    elif args.op == "remove_slides":
        indices = [int(x.strip()) for x in args.slides.split(",")]
        msg = op_remove_slides(prs, indices)
    elif args.op == "set_cell":
        msg = op_set_cell(prs, args.slide, args.shape, args.row, args.col, args.text)
    elif args.op == "set_position":
        msg = op_set_position(prs, args.slide, args.shape,
                              args.top, args.left, args.width, args.height)
    else:
        msg = f"ERROR: unknown op {args.op}"

    print(msg)
    if not msg.startswith("ERROR"):
        save(prs, out_path)


if __name__ == "__main__":
    main()
