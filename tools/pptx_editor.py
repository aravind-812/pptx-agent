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
    # Square-bracket placeholders — TOKEN-SHAPED only. Real content lives in
    # brackets too (e.g. "[Q3 2024]", "[Customer: ACME]"), so we must NOT treat
    # every `[...]` as a placeholder. A bracketed placeholder is one of two
    # shapes: (a) an all-caps template token like [CUSTOMER_NAME] / [DATE],
    # (b) short bracketed content starting with an instruction verb like
    # [Enter Project Name]. Anything containing a colon + inner value, or
    # sentence-ish text, is treated as real content and left alone.
    r"\[(?:(?=[^\]\n]*?\b(?:enter|insert|add\s|edit\s|type|your|here|sample|lorem|todo|tbd|example)\b[^\]\n]*)$|[A-Z][A-Z0-9_]*(?:\s+[A-Z][A-Z0-9_]*){0,3})\]$",
    r"\{\{.*?\}\}",       # {{variable}} template syntax
    r"<Enter [^>]+>",     # <Enter something here>
    r"(?i)^Enter \w",     # "Enter name", "Enter date" without angle brackets
    r"(?i)Click to edit",
    r"(?i)Lorem ipsum",
    r"\bTODO\b",
    r"(?i)Your .{1,30} here",
    r"(?i)^Sample [A-Z]",
    r"(?i)^Type here",
    r"(?i)^Add text",
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
def _strip_template_tuned_attrs(run_elem) -> None:
    """
    Remove character-spacing / kerning attributes from a cloned run's <a:rPr>.

    Templates often tune `spc` (character spacing) and `kern` (kerning) for
    the original text length. Carrying those values to NEW text of a different
    length produces broken visuals like 'TH AN K Y O U' (letters spread out
    because the original 'Thank You' had wide spacing to fill the box).
    """
    rPr = run_elem.find(f'{{{_NS}}}rPr')
    if rPr is None:
        return
    for attr in ("spc", "kern"):
        if attr in rPr.attrib:
            del rPr.attrib[attr]


def _set_paragraph_text(p_elem, line: str) -> None:
    """
    Rewrite the text of an EXISTING <a:p> element in place, preserving its
    paragraph properties (<a:pPr>) and the first run's formatting.
    Runs beyond the first are zeroed (kept intact for their size/bold/latin
    props where a template uses them) — never dropped, so font does not drift.
    """
    runs = p_elem.findall(f'{{{_NS}}}r')
    if not runs:
        # No runs at all: create a bare run. Font will come from placeholder/theme
        # defaults, which is the template's own fallback (best we can do).
        new_r = etree.SubElement(p_elem, f'{{{_NS}}}r')
        t = etree.SubElement(new_r, f'{{{_NS}}}t')
        t.text = line
        return

    # First run carries the text; strip template-tuned spacing/kerning.
    ref_run = runs[0]
    _strip_template_tuned_attrs(ref_run)
    t_elems = ref_run.findall(f'{{{_NS}}}t')
    if t_elems:
        t_elem = t_elems[0]
        for extra in t_elems[1:]:
            ref_run.remove(extra)
    else:
        t_elem = etree.SubElement(ref_run, f'{{{_NS}}}t')
    t_elem.text = line

    # Zero out any remaining runs so stray glyphs from other runs don't reappear.
    for r in runs[1:]:
        for t in r.findall(f'{{{_NS}}}t'):
            t.text = ""
        if not r.findall(f'{{{_NS}}}t'):
            etree.SubElement(r, f'{{{_NS}}}t').text = ""


def op_set_text(prs: Presentation, slide_idx: int, shape_name: str, text: str) -> str:
    """
    Set the text of a named shape, PRESERVING the template's paragraph/run
    structure. Each \\n in `text` becomes one paragraph. Existing paragraphs are
    reused (keeping their <a:pPr> + run formatting, so font/size/bullets stay
    correct); extra paragraphs are appended by cloning the last one. Excess
    paragraphs are removed.
    """
    slide = prs.slides[slide_idx - 1]
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_text_frame:
            txBody = shape.text_frame._txBody
            lines = text.split('\n')
            paras = txBody.findall(f'{{{_NS}}}p')

            if not paras:
                # Empty frame: build paragraphs from scratch.
                for line in lines:
                    new_p = etree.Element(f'{{{_NS}}}p')
                    new_r = etree.SubElement(new_p, f'{{{_NS}}}r')
                    etree.SubElement(new_r, f'{{{_NS}}}t').text = line
                    txBody.append(new_p)
                return f"Set text ({len(lines)} lines) on slide {slide_idx} shape '{shape_name}'"

            # Reuse as many existing paragraphs as we have lines for.
            for i, line in enumerate(lines):
                if i < len(paras):
                    _set_paragraph_text(paras[i], line)
                else:
                    # Clone the last paragraph for structure-consistent new lines.
                    anchor = paras[len(paras) - 1]
                    new_p = deepcopy(anchor)
                    anchor.addnext(new_p)
                    _set_paragraph_text(new_p, line)
                    paras.append(new_p)

            # Remove extra paragraphs beyond the lines we wrote.
            for p_elem in paras[len(lines):]:
                txBody.remove(p_elem)

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
# duplicate_slide — clone a slide (shapes, formatting, images) for repurposing
# ---------------------------------------------------------------------------
def op_duplicate_slide(prs: Presentation, slide_idx: int) -> dict:
    """
    Clone slide `slide_idx` (1-based) and append it at the END of the deck.
    Copies every shape (deepcopy of XML) plus image/media relationships, with
    rIds remapped so embedded pictures still resolve. Used by slides_to_add:
    a decided concept (e.g. Consumer QR view) with no home in the template
    clones the most similar slide, then its shapes get re-texted by the plan.

    Returns {"ok": True, "new_index": N} — caller repositions via op_apply_structure.
    """
    from copy import deepcopy
    from pptx.oxml.ns import qn

    if slide_idx < 1 or slide_idx > len(prs.slides):
        return {"ok": False, "error": f"slide {slide_idx} out of range (1–{len(prs.slides)})"}
    try:
        src = prs.slides[slide_idx - 1]
        dest = prs.slides.add_slide(src.slide_layout)

        # Drop the layout placeholders add_slide auto-created — we want an
        # exact copy of the SOURCE slide, not a fresh layout skeleton.
        spTree = dest.shapes._spTree
        for shp in list(dest.shapes):
            spTree.remove(shp._element)

        # Copy every shape from the source slide.
        for shape in src.shapes:
            spTree.append(deepcopy(shape._element))

        # Copy relationships (images, media, hyperlinks) and remap rIds in the
        # copied XML so pictures/links resolve against the new slide part.
        rid_map = {}
        for rId, rel in list(src.part.rels.items()):
            if rel.reltype.endswith("/slideLayout") or rel.reltype.endswith("/notesSlide"):
                continue
            try:
                if rel.is_external:
                    new_rid = dest.part.rels.get_or_add_ext_rel(rel.reltype, rel.target_ref)
                else:
                    new_rid = dest.part.relate_to(rel.target_part, rel.reltype)
                if new_rid:
                    rid_map[rId] = new_rid
            except Exception:
                pass
        for el in spTree.iter():
            for attr in (qn("r:embed"), qn("r:id"), qn("r:link")):
                v = el.get(attr)
                if v and v in rid_map:
                    el.set(attr, rid_map[v])

        return {"ok": True, "new_index": len(prs.slides)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# apply_structure — removals + slide additions in one pass, with index remap
# ---------------------------------------------------------------------------
def op_apply_structure(pptx_path: str, slides_to_remove: list,
                       slides_to_add: list) -> dict:
    """
    Apply the plan's structural changes to the deck in a single open/save cycle:
      - remove slides (original 1-based indices)
      - add slides: clone `base_slide`, insert after `insert_after`
        (both in ORIGINAL template numbering)

    Returns an index_map used to remap every downstream slide reference from
    ORIGINAL template numbering to FINAL deck positions:
      {"orig:<n>": <final>, "new:<key>": <final>}
    This fixes the classic off-by-N bug where edits after a removed/inserted
    slide land on the wrong slide.

    slides_to_add entries: {"key": "new_1", "base_slide": 3, "insert_after": 6}
    """
    from pptx.oxml.ns import qn

    try:
        prs = load(pptx_path)
    except Exception as e:
        return {"ok": False, "error": str(e), "index_map": {}}

    n = len(prs.slides)

    remove_set = set()
    for s in (slides_to_remove or []):
        try:
            s = int(s)
        except (TypeError, ValueError):
            continue
        if 1 <= s <= n:
            remove_set.add(s)

    additions = []
    for a in (slides_to_add or []):
        try:
            base = int(a.get("base_slide", 0))
        except (TypeError, ValueError):
            continue
        if not (1 <= base <= n):
            continue
        key = str(a.get("key") or f"new_{len(additions) + 1}")
        try:
            after = int(a.get("insert_after", n))
        except (TypeError, ValueError):
            after = n
        additions.append({"key": key, "base": base, "after": after})

    if not remove_set and not additions:
        return {"ok": True, "removed": [], "added": [],
                "index_map": {f"orig:{i}": i for i in range(1, n + 1)}}

    sldIdLst = prs.slides._sldIdLst
    orig_elems = list(sldIdLst)

    # 1) Clone bases first (deck still pristine) — clones append at the end.
    clone_elems = {}
    for a in additions:
        r = op_duplicate_slide(prs, a["base"])
        if r.get("ok"):
            clone_elems[a["key"]] = list(sldIdLst)[r["new_index"] - 1]

    # 2) Compute the final deck order (original slide numbers + clone keys).
    adds_by_after = {}
    for a in additions:
        if a["key"] in clone_elems:
            adds_by_after.setdefault(a["after"], []).append(a["key"])

    final_order = []
    for i in range(1, n + 1):
        if i in remove_set:
            continue
        final_order.append(("orig", i))
        for k in adds_by_after.pop(i, []):
            final_order.append(("new", k))
    # Leftovers (insert_after pointed at a removed/unknown slide) go at the end.
    for after_val in sorted(adds_by_after):
        for k in adds_by_after[after_val]:
            final_order.append(("new", k))

    # 3) Reorder sldIdLst to the final order; removed originals drop out.
    elems = {("orig", i): orig_elems[i - 1] for i in range(1, n + 1)}
    for k, el in clone_elems.items():
        elems[("new", k)] = el
    for el in list(sldIdLst):
        sldIdLst.remove(el)
    for key in final_order:
        sldIdLst.append(elems[key])

    # 4) Drop relationships of removed slides so orphan parts don't persist.
    for i in remove_set:
        rId = orig_elems[i - 1].get(qn("r:id"))
        if rId:
            try:
                prs.part.drop_rel(rId)
            except Exception:
                pass

    index_map = {}
    for pos, key in enumerate(final_order, 1):
        kind, v = key
        index_map[f"{kind}:{v}"] = pos

    try:
        save(prs, pptx_path)
    except Exception as e:
        return {"ok": False, "error": f"save failed: {e}", "index_map": {}}

    return {"ok": True, "removed": sorted(remove_set), "added": list(clone_elems),
            "final_count": len(final_order), "index_map": index_map}


# ---------------------------------------------------------------------------
# delete_shapes — remove individual named shapes from a slide
# ---------------------------------------------------------------------------
def op_delete_shapes(pptx_path: str, slide_idx: int, shape_names: list[str]) -> dict:
    """
    Delete named shapes (text boxes, images, grouped shapes, connectors) from a slide
    by removing their XML element from the slide's spTree.
    Use this when a template has more content slots than the source provides —
    delete the entire shape group (image + text box together) rather than blanking text.
    Returns count deleted and any names not found.
    """
    try:
        prs = load(pptx_path)
    except Exception as e:
        return {"ok": False, "error": str(e), "deleted": 0}

    if slide_idx < 1 or slide_idx > len(prs.slides):
        return {"ok": False, "error": f"slide {slide_idx} out of range", "deleted": 0}

    slide = prs.slides[slide_idx - 1]
    sp_tree = slide.shapes._spTree

    deleted, not_found = [], []
    for name in shape_names:
        found = False
        for shape in list(slide.shapes):
            if shape.name == name:
                sp_tree.remove(shape._element)
                deleted.append(name)
                found = True
                break
        if not found:
            not_found.append(name)

    if deleted:
        try:
            save(prs, pptx_path)
        except Exception as e:
            return {"ok": False, "error": f"save failed: {e}", "deleted": len(deleted)}

    return {"ok": True, "deleted": len(deleted), "deleted_names": deleted, "not_found": not_found}


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
# Serialize LibreOffice invocations. Multiple agents (planner thumbnails,
# reviewer QA, backend before/after) call op_render_png; if they overlap or run
# back-to-back, soffice collides on its shared default user profile and the
# render fails (which is why the app shows "slide preview requires LibreOffice"
# while a standalone render works fine).
import threading
_RENDER_LOCK = threading.Lock()


def op_render_png(pptx_path: str, output_dir: str, dpi: int = 96) -> str:
    """
    Render a PPTX to per-slide PNGs via LibreOffice (headless) + pdftoppm.

    Robust for in-process use:
      - Each call gets its own isolated LibreOffice user profile
        (-env:UserInstallation) so concurrent/back-to-back renders never collide
        on the shared profile lock.
      - Invocations are serialized with a process-wide lock.
      - Longer timeout (300s) so large decks don't get cut off.
      - dpi: 96 for QA/viewer (good balance), pass 120 if crisper text needed.

    Returns a status string; "Rendered N slides to <dir>" on success, else a
    string starting with "ERROR".
    """
    import subprocess, shutil, tempfile, os, uuid

    lo = shutil.which("libreoffice") or shutil.which("soffice")
    if not lo:
        return "ERROR: libreoffice not found in PATH — install LibreOffice for visual QA"
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return "ERROR: pdftoppm not found in PATH — install poppler"

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Isolated profile dir per invocation to avoid the shared-profile lock.
    profile = os.path.join(tempfile.gettempdir(), f"lo_profile_{uuid.uuid4().hex[:12]}")
    os.makedirs(profile, exist_ok=True)
    profile_url = "file://" + profile

    with _RENDER_LOCK:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = subprocess.run(
                    [lo, "-env:UserInstallation=" + profile_url,
                     "--headless", "--norestore", "--convert-to", "pdf",
                     "--outdir", tmp, pptx_path],
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode != 0:
                    return f"ERROR: libreoffice conversion failed:\n{r.stderr[:500]}"
                pdfs = list(Path(tmp).glob("*.pdf"))
                if not pdfs:
                    return "ERROR: no PDF produced by libreoffice"
                pdf = pdfs[0]
                r2 = subprocess.run(
                    [pdftoppm, "-png", "-r", str(dpi), str(pdf), str(out / "slide")],
                    capture_output=True, text=True, timeout=300,
                )
                if r2.returncode != 0:
                    return f"ERROR: pdftoppm failed:\n{r2.stderr[:500]}"
        except subprocess.TimeoutExpired:
            return "ERROR: LibreOffice render timed out (deck too large)"
        except Exception as e:
            return f"ERROR: render failed: {e}"
        finally:
            shutil.rmtree(profile, ignore_errors=True)

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

                # 4. Rough overflow heuristic — reuse the executor's real capacity
                #    metrics (margins + Pillow font metrics + line_spacing) so
                #    precheck and autofit agree and we don't demand spurious fixes.
                if shape.height and shape.width and text:
                    try:
                        chars_per_line, max_lines = _get_shape_capacity(shape)
                    except Exception:
                        chars_per_line, max_lines = 20, 3
                    total_lines = sum(
                        max(1, (len(para) + chars_per_line - 1) // chars_per_line)
                        for para in text.split("\n")
                    )
                    if total_lines > max_lines:
                        issues.append({
                            "type": "likely_overflow",
                            "severity": "warn",
                            "detail": (
                                f"slide {slide_idx} / '{name}': "
                                f"~{total_lines} lines estimated, shape holds ~{max_lines}"
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

        # 6. Table cell text overflow in narrow columns (any table)
        _NARROW_COL_PX = 80   # columns < 80px wide are data/indicator columns
        for g_slide_idx, g_slide in enumerate(prs.slides, 1):
            for g_shape in g_slide.shapes:
                if not g_shape.has_table:
                    continue
                tbl = g_shape.table
                for ri in range(len(tbl.rows)):
                    for ci in range(len(tbl.columns)):
                        col_w_px = tbl.columns[ci].width * 96 // 914400
                        if col_w_px >= _NARROW_COL_PX:
                            continue
                        cell_text = tbl.cell(ri, ci).text_frame.text.strip()
                        if not cell_text or len(cell_text) < 3:
                            continue
                        chars_fit = max(1, col_w_px // 7)
                        if len(cell_text) > chars_fit:
                            issues.append({
                                "type": "table_cell_overflow",
                                "severity": "hard",
                                "detail": (
                                    f"slide {g_slide_idx}: table '{g_shape.name}' "
                                    f"cell ({ri},{ci}) has '{cell_text[:30]}' "
                                    f"in narrow column ({col_w_px}px, ~{chars_fit} chars fit)"
                                ),
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


# ===========================================================================
# Hybrid-executor ops — fuzzy resolve, auto-fit, batch apply, verify, sweep
# ===========================================================================

# ---------------------------------------------------------------------------
# resolve_shape_name — fuzzy match plan shape name → actual shape on slide
# ---------------------------------------------------------------------------
def _normalize_name(name: str) -> str:
    """Lowercase and collapse whitespace so visually-identical names match."""
    return re.sub(r"\s+", " ", name).strip().lower()


def op_resolve_shape_name(prs: Presentation, slide_idx: int, target_name: str) -> dict:
    """
    Match a target shape name against actual shape names on a slide.
    Returns: {ok, matched_name, confidence, candidates}.

    Match priority (deliberately conservative — a wrong shape is far worse
    than a deferred edit):
      exact  → case-insensitive → whitespace/case-normalized → prefix token
    Substring and diff-lib fuzzy matching have been REMOVED: they silently land
    edits on the wrong shape (e.g. "Body 1" matching "Body 10"). When no safe
    match is found the edit is returned as a miss so the caller can defer it to
    the LLM fallback rather than corrupting the deck.
    """
    if slide_idx < 1 or slide_idx > len(prs.slides):
        return {"ok": False, "error": f"slide {slide_idx} out of range"}

    slide = prs.slides[slide_idx - 1]
    actual_names = [s.name for s in slide.shapes]

    if target_name in actual_names:
        return {"ok": True, "matched_name": target_name, "confidence": "exact"}

    lower_map = {n.lower(): n for n in actual_names}
    if target_name.lower() in lower_map:
        return {"ok": True, "matched_name": lower_map[target_name.lower()], "confidence": "case-insensitive"}

    norm_map = {_normalize_name(n): n for n in actual_names}
    if _normalize_name(target_name) in norm_map:
        return {"ok": True, "matched_name": norm_map[_normalize_name(target_name)], "confidence": "normalized"}

    # Prefix-token match: "Body" matches "Body 1" — but ONLY if unambiguous.
    # This handles real templates that suffix shape names with indices while
    # never matching "Body 1" → "Body 10" (the supplied token is the full name,
    # not a substring that a longer sibling also contains).
    target_tokens = _normalize_name(target_name).split()
    if target_tokens:
        prefix = target_tokens[0]
        prefix_matches = [n for n in actual_names if _normalize_name(n).split()[:1] == [prefix]]
        if len(prefix_matches) == 1:
            return {"ok": True, "matched_name": prefix_matches[0], "confidence": "prefix-token"}

    return {
        "ok": False,
        "error": f"no unique shape match for '{target_name}' on slide {slide_idx}",
        "available_names": actual_names,
    }


# ---------------------------------------------------------------------------
# get_shape_font_size — read the actual font size of a shape's first run
# ---------------------------------------------------------------------------
def _get_shape_font_size(shape) -> int:
    """
    Best-effort read of the shape's EFFECTIVE font size in points (hundredths →
    pts). Returns the LARGEST explicit size found so capacity is budgeted by the
    biggest text, not the first run.

    Many templates store no run-level <a:rPr sz> on title/metric shapes — the
    size lives in the shape's own text-body defaults (<a:defPPr><a:lvlNpPr>
    <a:defRPr sz="8800">). If we ignore that we fall back to 14, massively
    overestimate capacity, and never shrink-to-fit → text blows out of the box.
    So we walk the inheritance chain: run rPr → paragraph pPr/defRPr → txBody
    lvlNpPr defRPr → endParaRPr.

    Precedence: an EXPLICIT run size wins over any inherited default. Inherited
    (defRPr) sizes are only consulted when no run carries an explicit size.
    Falls back to 14 only if nothing is set anywhere.
    """
    NS = _NS
    best = 0
    inherited = 0

    def _cand(pt) -> None:
        nonlocal best
        if pt and pt > best:
            best = int(pt)

    try:
        if not shape.has_text_frame:
            return 14
        for p in shape.text_frame.paragraphs:
            for r in p.runs:
                if r.font.size is not None:
                    _cand(r.font.size.pt)
            # paragraph-level pPr default (paragraphs can set their own defRPr)
            for sz in p._p.iter(f'{{{NS}}}defRPr'):
                if sz.get("sz"):
                    inherited = max(inherited, int(int(sz.get("sz")) / 100))
        txBody = shape.text_frame._txBody
        # shape-level defaults: <a:defPPr><a:lvlNpPr><a:defRPr sz>
        for def_rpr in txBody.iter(f'{{{NS}}}defRPr'):
            if def_rpr.get("sz"):
                inherited = max(inherited, int(int(def_rpr.get("sz")) / 100))
        for ep in txBody.iter(f'{{{NS}}}endParaRPr'):
            if ep.get("sz"):
                inherited = max(inherited, int(int(ep.get("sz")) / 100))
    except Exception:
        pass
    # Explicit run sizes win; only fall back to inherited if none set.
    return best or inherited or 14


# ---------------------------------------------------------------------------
# Per-shape capacity helpers — read actual PPTX metrics instead of guessing
# ---------------------------------------------------------------------------
def _find_font_path(font_name: str) -> str | None:
    """Locate a .ttf/.otf file for a given font family name on the host OS."""
    import glob
    import os
    search = font_name.lower().replace(" ", "")
    dirs = [
        "/System/Library/Fonts",
        "/Library/Fonts",
        os.path.expanduser("~/Library/Fonts"),
        "/usr/share/fonts",
        "/usr/local/share/fonts",
    ]
    for d in dirs:
        for ext in ("*.ttf", "*.otf"):
            for path in glob.glob(os.path.join(d, "**", ext), recursive=True):
                base = os.path.basename(path).lower().replace(" ", "")
                if search in base or base.startswith(search[:6]):
                    return path
    return None


def _get_char_width_px(shape, font_size: int) -> float:
    """
    Average character width in pixels for this shape's font, measured with Pillow.
    Falls back to 0.55 * font_size if Pillow or font not available.
    """
    font_name = None
    try:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                if run.font.name:
                    font_name = run.font.name
                    break
            if font_name:
                break
    except Exception:
        pass

    font_path = _find_font_path(font_name) if font_name else None
    try:
        from .font_measurer import measure
    except ImportError:
        try:
            from tools.font_measurer import measure
        except ImportError:
            measure = None

    if measure:
        try:
            result = measure("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ", font_size, font_path)
            return max(1.0, result["width"] / 52)
        except Exception:
            pass
    # Fallback: typical proportional font ≈ 0.55x font size in px at screen res
    return max(1.0, font_size * 0.55 * 96 / 72)


def _get_line_height_px(shape, font_size: int) -> int:
    """
    Line height in pixels from the shape's paragraph line_spacing setting.
    Falls back to 1.2x font_size if not set. Uses actual EMU→px conversion so
    the estimate tracks real PowerPoint line metrics rather than a hardcoded 20px.
    """
    try:
        for para in shape.text_frame.paragraphs:
            ls = para.line_spacing
            if ls is None:
                continue
            # Pt object → convert to px at 96dpi
            if hasattr(ls, "pt"):
                return max(1, int(ls.pt * 96 / 72))
            # Numeric ratio (e.g. 1.5 = 1.5× line height)
            if isinstance(ls, (int, float)) and ls > 0:
                return max(1, int(font_size * ls * 96 / 72))
    except Exception:
        pass
    # Real PowerPoint single-space line height ≈ 1.2 × font-size, in px at 96dpi.
    return max(1, int(font_size * 1.2 * 96 / 72))


def _get_shape_wrap_mode(shape) -> str:
    """Return 'none' | 'square' — the shape's <a:bodyPr wrap> setting."""
    try:
        A = _NS
        txb = shape.text_frame._txBody
        for ch in txb:
            if ch.tag == f'{{{A}}}bodyPr':
                return (ch.get("wrap") or "square").lower()
    except Exception:
        pass
    return "square"


def _get_shape_capacity(shape, font_size_override: int = 0) -> tuple[int, int]:
    """
    Return (chars_per_line, max_lines) for a shape using its actual metrics:
    - Available width/height after internal text frame margins
    - Char width measured with Pillow for the shape's font
    - Line height from paragraph line_spacing

    font_size_override: when set, compute capacity at this size (used to predict
    post-shrink capacity) instead of the shape's current size.

    For wrap='none' shapes (titles, big numbers) the text never wraps — they are
    single-line and auto-grow. Capacity there is measured by WIDTH only, treating
    the shape as one line, so shrink-to-fit can still shrink an over-wide title.
    """
    font_size = font_size_override or _get_shape_font_size(shape)

    # Internal padding from text frame
    try:
        tf = shape.text_frame
        ml = (tf.margin_left  or 91440) * 96 / 914400   # default ~0.1 inch
        mr = (tf.margin_right or 91440) * 96 / 914400
        mt = (tf.margin_top   or 45720) * 96 / 914400
        mb = (tf.margin_bottom or 45720) * 96 / 914400
    except Exception:
        ml = mr = mt = mb = 9.6  # ~0.1 inch in px

    avail_w = max(10, (shape.width  or 0) * 96 / 914400 - ml - mr)
    avail_h = max(10, (shape.height or 0) * 96 / 914400 - mt - mb)

    char_w   = _get_char_width_px(shape, font_size)
    line_h   = _get_line_height_px(shape, font_size)

    wrap = _get_shape_wrap_mode(shape)
    if wrap == "none":
        # Single-line: no wrapping. One line's worth of budget = full width.
        chars_per_line = max(1, int(avail_w / char_w))
        # max_lines is the number of explicit \n paragraphs we can hold; height
        # still bounds it, but a wrap=none title is effectively 1 rendered line
        # per paragraph.
        max_lines = max(1, int((avail_h + line_h - 1) / line_h))
    else:
        chars_per_line = max(1, int(avail_w / char_w))
        max_lines      = max(1, int(avail_h / line_h))
    return chars_per_line, max_lines


def _apply_font_size(prs: Presentation, slide_idx: int, shape_name: str,
                     font_size: int) -> None:
    """
    Set the run font size to `font_size` (points) on every run of a shape.
    No-op if the size is unchanged. Used by shrink-to-fit to keep text inside
    the shape without dropping content.
    """
    try:
        slide = prs.slides[slide_idx - 1]
        for shape in slide.shapes:
            if shape.name == shape_name and shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        if run.font.size is not None and int(run.font.size.pt) == font_size:
                            continue
                        run.font.size = Pt(font_size)
    except Exception:
        pass


def _estimated_total_lines(text: str, chars_per_line: int) -> int:
    """Estimated wrapped line count for `text` given chars_per_line (>0)."""
    if chars_per_line <= 0:
        chars_per_line = 1
    return sum(
        max(1, (len(line) + chars_per_line - 1) // chars_per_line)
        for line in text.split("\n")
    )


def op_fit_text_overflow(pptx_path: str, targets: list[dict]) -> dict:
    """
    Deterministic safety net — guarantee every edited shape's ACTUAL text fits
    its capacity, regardless of which write path produced it (batch apply,
    LLM fallback tools, set_para, manual). Runs AFTER writes.

    targets: [{"slide": int, "shape": str}, ...]
    For each, reads back the real text, measures the shape's capacity, and if
    the text overflows, shrinks the run font (floor 8pt) until it fits. NEVER
    truncates — the planner/reviewer handle content decisions; this only makes
    sure what's written is visible and inside the box.

    Returns per-target status + a count of shapes that needed shrinking.
    """
    try:
        prs = load(pptx_path)
    except Exception as e:
        return {"ok": False, "error": str(e), "fitted": 0, "results": []}

    FONT_FLOOR_PT = 8
    results = []
    fitted = 0
    for t in targets:
        slide_idx = int(t.get("slide", 0))
        shape_name = t.get("shape", "")
        if slide_idx < 1 or slide_idx > len(prs.slides):
            results.append({"slide": slide_idx, "shape": shape_name,
                            "ok": False, "error": "slide out of range"})
            continue

        target = None
        slide = prs.slides[slide_idx - 1]
        for shape in slide.shapes:
            if shape.name == shape_name and shape.has_text_frame:
                target = shape
                break
        if target is None:
            results.append({"slide": slide_idx, "shape": shape_name,
                            "ok": False, "error": "shape not found"})
            continue

        text = target.text_frame.text
        if not text.strip():
            results.append({"slide": slide_idx, "shape": shape_name, "ok": True,
                            "needs_shrink": False, "reason": "empty"})
            continue

        actual_size = _get_shape_font_size(target)
        chars_per_line, max_lines = _get_shape_capacity(target)
        total_lines = _estimated_total_lines(text, chars_per_line)

        needs_shrink = total_lines > max_lines
        applied_size = actual_size
        if needs_shrink:
            # line count ≈ ∝ 1/size; scale down toward probe until it fits.
            shrink_scale = min(1.0, max_lines / max(total_lines, 1))
            applied_size = max(FONT_FLOOR_PT, int(round(actual_size * shrink_scale)))
            cpl2, ml2 = _get_shape_capacity(target, font_size_override=applied_size)
            total2 = _estimated_total_lines(text, cpl2)
            while total2 > ml2 and applied_size > FONT_FLOOR_PT:
                applied_size = max(FONT_FLOOR_PT, applied_size - 1)
                cpl2, ml2 = _get_shape_capacity(target, font_size_override=applied_size)
                total2 = _estimated_total_lines(text, cpl2)
            _apply_font_size(prs, slide_idx, shape_name, applied_size)
            fitted += 1

        results.append({
            "slide": slide_idx, "shape": shape_name, "ok": True,
            "needs_shrink": needs_shrink,
            "from_size": actual_size, "to_size": applied_size,
            "estimated_lines": total_lines, "max_lines": max_lines,
        })

    try:
        save(prs, pptx_path)
    except Exception as e:
        return {"ok": False, "error": f"save failed: {e}", "fitted": fitted, "results": results}

    return {"ok": True, "fitted": fitted, "results": results}


# ---------------------------------------------------------------------------
# set_text_autofit — set text, smart-truncate if overflows (no font shrinking)
# ---------------------------------------------------------------------------
def _truncate_at_word(text: str, max_chars: int) -> str:
    """Truncate text at the last word boundary <= max_chars, append ellipsis."""
    if len(text) <= max_chars:
        return text
    cut = max(1, max_chars - 1)
    slice_ = text[:cut]
    last_space = slice_.rfind(" ")
    if last_space > cut * 0.5:
        slice_ = slice_[:last_space]
    return slice_.rstrip() + "…"


def _smart_truncate(text: str, max_chars: int) -> str:
    """
    Truncate text to roughly max_chars, preserving line structure (bullets).
    Always cuts from the END (not middle) so phrases like "Presented to Last Mile"
    don't get mangled into "Presented to…t Mile".
    """
    if len(text) <= max_chars:
        return text

    lines = text.split("\n")
    n_lines = len(lines)

    if n_lines == 1:
        return _truncate_at_word(text, max_chars)

    # Multi-line: distribute chars proportionally; truncate each line from end
    chars_per_line = max(20, (max_chars - n_lines) // n_lines)
    return "\n".join(_truncate_at_word(line, chars_per_line) for line in lines)


def op_set_text_autofit(prs: Presentation, slide_idx: int, shape_name: str,
                         text: str, font_size: int = 0) -> dict:
    """
    Set text on a shape, fitting it inside the shape's real capacity:
      1. If the text fits — set it as-is.
      2. If it overflows — apply PowerPoint-style shrink-to-fit: scale the font
         size down (capped at a floor) until it fits, honoring the template's
         design intent (bullets stay bullets, no content dropped).
      3. Only if shrinking fails AND overflow is severe (>~1.9x) do we
         smart-truncate — and only the tail, keeping the punchline.
    Returns applied text, whether it was truncated, and the effective font size.
    """
    slide = prs.slides[slide_idx - 1]
    target = None
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_text_frame:
            target = shape
            break
    if target is None:
        return {"ok": False, "error": f"shape '{shape_name}' not found on slide {slide_idx}"}

    actual_font = font_size if font_size > 0 else _get_shape_font_size(target)

    # Skip if text is unchanged (avoids re-running set_text and losing run-level styling)
    try:
        current = target.text_frame.text.strip()
        if current == text.strip():
            return {
                "ok": True, "applied_text": text, "truncated": False,
                "original_length": len(text), "applied_length": len(text),
                "font_size_pt": actual_font, "skipped": "unchanged",
            }
    except Exception:
        pass

    # Use per-shape capacity (real margins + Pillow font metrics, not generic constants)
    chars_per_line, max_lines = _get_shape_capacity(target)

    total_lines = sum(
        max(1, (len(line) + chars_per_line - 1) // chars_per_line)
        for line in text.split("\n")
    )

    applied_font = actual_font
    applied_text = text
    truncated = False

    if total_lines > max_lines:
        # Too much text for the current size. Try shrink-to-fit first.
        FONT_FLOOR_PT = 8
        MIN_SCALE = FONT_FLOOR_PT / max(actual_font, 1)
        ideal_scale = max_lines / max(total_lines, 1)   # lines ≈ ∝ 1/size
        # Shrink as far as needed (down to the floor) so we preserve max content.
        shrink_scale = min(1.0, ideal_scale)
        if shrink_scale < MIN_SCALE:
            shrink_scale = MIN_SCALE
        applied_font = max(FONT_FLOOR_PT, int(round(actual_font * shrink_scale)))
        # Recompute capacity at the shrunk size — the scaled size always fits
        # fewer lines, so gauge what's left after shrinking.
        chars_for_trunc, max_for_trunc = _get_shape_capacity(target, font_size_override=applied_font)
        total_after_shrink = sum(
            max(1, (len(line) + chars_for_trunc - 1) // chars_for_trunc)
            for line in text.split("\n")
        )
        fits_after_shrink = total_after_shrink <= max_for_trunc

        if fits_after_shrink:
            # Shrinking alone fits — apply text at the scaled font, no truncation.
            msg = op_set_text(prs, slide_idx, shape_name, applied_text)
            if msg.startswith("ERROR"):
                return {"ok": False, "error": msg,
                        "font_size_pt": actual_font, "truncated": False}
            _apply_font_size(prs, slide_idx, shape_name, applied_font)
            return {
                "ok": True, "applied_text": applied_text, "truncated": False,
                "original_length": len(text), "applied_length": len(applied_text),
                "font_size_pt": applied_font, "font_scaled": applied_font != actual_font,
            }

        # Shrink helps but still overflows: apply shrunk font, then truncate only
        # the remaining excess (never drops the whole tail unnecessarily).
        if applied_font != actual_font:
            _apply_font_size(prs, slide_idx, shape_name, applied_font)
        if total_after_shrink > max_for_trunc * 1.9:
            max_chars = chars_for_trunc * max_for_trunc
            applied_text = _smart_truncate(text, max_chars)
            truncated = True

    msg = op_set_text(prs, slide_idx, shape_name, applied_text)
    if msg.startswith("ERROR"):
        return {"ok": False, "error": msg, "font_size_pt": applied_font, "truncated": False}
    if applied_font != actual_font:
        _apply_font_size(prs, slide_idx, shape_name, applied_font)

    return {
        "ok": True,
        "applied_text": applied_text,
        "truncated": truncated,
        "original_length": len(text),
        "applied_length": len(applied_text),
        "font_size_pt": applied_font,
        "font_scaled": applied_font != actual_font,
    }


# ---------------------------------------------------------------------------
# apply_edits_batch — apply many edits in one transaction
# ---------------------------------------------------------------------------
def op_apply_edits_batch(pptx_path: str, edits: list[dict],
                          autofit: bool = True) -> dict:
    """
    Apply a list of edits in one open/save cycle.
    Each edit dict: {slide, op, shape, text [, row, col, para]}.
    op ∈ {set_text, set_cell, set_para}.

    Auto-resolves shape names (fuzzy match).
    autofit=True wraps set_text in smart-truncate.

    Returns per-edit status and counts.
    """
    try:
        prs = load(pptx_path)
    except Exception as e:
        return {"ok": False, "error": str(e), "applied": 0, "results": []}

    results = []
    applied = 0
    failed = 0

    for i, e in enumerate(edits):
        try:
            slide_idx = int(e.get("slide", 0))
            shape_name = e.get("shape", "")
            op_kind = e.get("op", "set_text")
            text = e.get("text", "")

            if slide_idx < 1 or slide_idx > len(prs.slides):
                results.append({"index": i, "ok": False, "error": f"slide {slide_idx} out of range"})
                failed += 1
                continue

            # Resolve shape name (fuzzy)
            resolved = op_resolve_shape_name(prs, slide_idx, shape_name)
            if not resolved.get("ok"):
                results.append({
                    "index": i, "slide": slide_idx, "shape": shape_name,
                    "ok": False, "error": resolved.get("error"),
                    "available_names": resolved.get("available_names", []),
                })
                failed += 1
                continue

            actual_shape = resolved["matched_name"]

            if op_kind == "set_text":
                if not text or not text.strip():
                    results.append({"index": i, "slide": slide_idx, "shape": actual_shape,
                                    "ok": False, "error": "empty text — skipped to preserve template content"})
                    failed += 1
                    continue
                if autofit:
                    r = op_set_text_autofit(prs, slide_idx, actual_shape, text)
                    if not r["ok"]:
                        results.append({"index": i, "slide": slide_idx, "shape": actual_shape,
                                        "ok": False, "error": r["error"]})
                        failed += 1
                        continue
                    results.append({"index": i, "slide": slide_idx, "shape": actual_shape,
                                    "ok": True, "truncated": r["truncated"],
                                    "resolved_from": shape_name if shape_name != actual_shape else None})
                else:
                    msg = op_set_text(prs, slide_idx, actual_shape, text)
                    if msg.startswith("ERROR"):
                        results.append({"index": i, "slide": slide_idx, "shape": actual_shape,
                                        "ok": False, "error": msg})
                        failed += 1
                        continue
                    results.append({"index": i, "slide": slide_idx, "shape": actual_shape, "ok": True})

            elif op_kind == "set_cell":
                row = int(e.get("row", 0))
                col = int(e.get("col", 0))
                col_warn = None
                try:
                    tbl_shape = next(
                        s for s in prs.slides[slide_idx - 1].shapes
                        if s.name == actual_shape and s.has_table
                    )
                    col_w_px = tbl_shape.table.columns[col].width * 96 // 914400
                    if col_w_px < 80:
                        chars_fit = max(1, col_w_px // 7)
                        if len(text) > chars_fit:
                            col_warn = (
                                f"cell ({row},{col}) '{text[:20]}' likely overflows "
                                f"narrow column ({col_w_px}px, ~{chars_fit} chars fit)"
                            )
                except Exception:
                    pass
                msg = op_set_cell(prs, slide_idx, actual_shape, row, col, text)
                if msg.startswith("ERROR"):
                    results.append({"index": i, "slide": slide_idx, "shape": actual_shape,
                                    "ok": False, "error": msg})
                    failed += 1
                    continue
                entry = {"index": i, "slide": slide_idx, "shape": actual_shape, "ok": True}
                if col_warn:
                    entry["warn"] = col_warn
                results.append(entry)

            elif op_kind == "set_para":
                para = int(e.get("para", 0))
                msg = op_set_para(prs, slide_idx, actual_shape, para, text)
                if msg.startswith("ERROR"):
                    results.append({"index": i, "slide": slide_idx, "shape": actual_shape,
                                    "ok": False, "error": msg})
                    failed += 1
                    continue
                results.append({"index": i, "slide": slide_idx, "shape": actual_shape, "ok": True})
            else:
                results.append({"index": i, "ok": False, "error": f"unknown op '{op_kind}'"})
                failed += 1
                continue

            applied += 1

        except Exception as exc:
            results.append({"index": i, "ok": False, "error": str(exc)})
            failed += 1

    try:
        save(prs, pptx_path)
    except Exception as e:
        return {"ok": False, "error": f"save failed: {e}", "applied": applied, "results": results}

    return {"ok": failed == 0, "applied": applied, "failed": failed, "results": results}


# ---------------------------------------------------------------------------
# verify_edits — read back text from edited shapes, compare to expected
# ---------------------------------------------------------------------------
def op_verify_edits(pptx_path: str, edits: list[dict]) -> dict:
    """
    Re-open PPTX and verify that each edit's target shape now contains the
    expected (or near-expected) text. Returns mismatches.
    """
    try:
        prs = load(pptx_path)
    except Exception as e:
        return {"ok": False, "error": str(e), "mismatches": []}

    mismatches = []
    verified = 0

    for i, e in enumerate(edits):
        slide_idx = int(e.get("slide", 0))
        shape_name = e.get("shape", "")
        expected = (e.get("text") or "").strip()
        op_kind = e.get("op", "set_text")

        if slide_idx < 1 or slide_idx > len(prs.slides):
            continue
        slide = prs.slides[slide_idx - 1]

        # Find the shape (allow fuzzy if exact missing)
        target = None
        for s in slide.shapes:
            if s.name == shape_name:
                target = s
                break
        if target is None:
            resolved = op_resolve_shape_name(prs, slide_idx, shape_name)
            if resolved.get("ok"):
                for s in slide.shapes:
                    if s.name == resolved["matched_name"]:
                        target = s
                        break
        if target is None:
            mismatches.append({"index": i, "slide": slide_idx, "shape": shape_name,
                               "issue": "shape not found"})
            continue

        if op_kind == "set_cell":
            row = int(e.get("row", 0))
            col = int(e.get("col", 0))
            if not target.has_table:
                mismatches.append({"index": i, "slide": slide_idx, "shape": shape_name,
                                   "issue": "not a table"})
                continue
            actual = target.table.cell(row, col).text_frame.text.strip()
        elif target.has_text_frame:
            actual = target.text_frame.text.strip()
        else:
            mismatches.append({"index": i, "slide": slide_idx, "shape": shape_name,
                               "issue": "no text frame"})
            continue

        # Match if expected is contained in actual (handles truncation) OR vice versa
        exp_norm = " ".join(expected.split()).lower()
        act_norm = " ".join(actual.split()).lower()
        if not exp_norm:
            verified += 1
            continue

        # Take first 25 chars (or 60% of expected) as the must-match signature
        sig_len = max(15, min(len(exp_norm) * 6 // 10, 80))
        sig = exp_norm[:sig_len]
        if sig in act_norm or act_norm in exp_norm or exp_norm == act_norm:
            verified += 1
        else:
            mismatches.append({
                "index": i, "slide": slide_idx, "shape": shape_name,
                "issue": "text mismatch",
                "expected_start": expected[:60],
                "actual_start": actual[:60],
            })

    return {"ok": len(mismatches) == 0, "verified": verified, "mismatches": mismatches}


# ---------------------------------------------------------------------------
# placeholder_sweep — replace any leftover placeholder pattern with empty
# ---------------------------------------------------------------------------
def op_placeholder_sweep(pptx_path: str,
                          only_slides: list[int] | None = None,
                          replacement: str = "") -> dict:
    """
    Scan all (or only listed) slides for leftover placeholder patterns.

    Behavior:
      - If shape contains ONLY placeholder text (no real content) → replace whole shape with `replacement`
      - If shape has placeholders mixed with real content → strip ONLY the matched patterns,
        keep the real content intact. Never wipe a shape that has user content.

    Returns count of swept shapes and per-slide details.
    """
    try:
        prs = load(pptx_path)
    except Exception as e:
        return {"ok": False, "error": str(e), "swept": 0}

    compiled = [re.compile(p, re.IGNORECASE) for p in PLACEHOLDER_PATTERNS]
    swept = []

    for slide_idx, slide in enumerate(prs.slides, 1):
        if only_slides and slide_idx not in only_slides:
            continue
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text
            if not text:
                continue

            new_text = text
            matched = False
            for pat in compiled:
                if pat.search(new_text):
                    new_text = pat.sub("", new_text)
                    matched = True

            if not matched:
                continue

            cleaned = new_text.strip()
            if not cleaned:
                # Whole shape was placeholder-only — use replacement
                final = replacement
            else:
                # Real content survived after stripping placeholders — keep it
                final = re.sub(r"\n{3,}", "\n\n", new_text).strip()

            op_set_text(prs, slide_idx, shape.name, final)
            swept.append({"slide": slide_idx, "shape": shape.name,
                          "old_text": text[:60], "new_text": final[:60]})

    if swept:
        try:
            save(prs, pptx_path)
        except Exception as e:
            return {"ok": False, "error": f"save failed: {e}", "swept": len(swept)}

    return {"ok": True, "swept": len(swept), "details": swept}


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
