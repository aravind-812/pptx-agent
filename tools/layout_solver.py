#!/usr/bin/env python3
"""Constraint-based layout solver using kiwisolver (Cassowary algorithm).

Resolves relative layout constraints to exact pixel positions so the agent
never has to guess coordinates.

Usage:
  python layout_solver.py --slide-w 9144000 --slide-h 5143500 \
      --boxes '[{"name":"title","h":800000},{"name":"body","h":2000000}]' \
      --margin-top 457200 --margin-left 457200 --gap 228600
"""
import sys
import json
import argparse
import kiwisolver as kiwi


def solve(slide_w: int, slide_h: int, boxes: list[dict],
          margin_top: int = 457200, margin_left: int = 457200,
          margin_right: int = 457200, gap: int = 228600) -> list[dict]:
    """
    Solve vertical stacking layout for a list of boxes on a slide.

    boxes: list of {"name": str, "h": int, "w": int (optional)}
    All units in EMUs (English Metric Units): 1 inch = 914400 EMU.
    Returns list of {"name", "top", "left", "width", "height"}.
    """
    solver = kiwi.Solver()
    usable_w = slide_w - margin_left - margin_right

    result = []
    prev_bottom = None

    for box in boxes:
        top_var = kiwi.Variable(f"{box['name']}_top")
        bottom_var = kiwi.Variable(f"{box['name']}_bottom")
        h = int(box["h"])

        if prev_bottom is None:
            solver.addConstraint((top_var == margin_top) | "required")
        else:
            solver.addConstraint((top_var == prev_bottom + gap) | "required")

        solver.addConstraint((bottom_var == top_var + h) | "required")
        solver.updateVariables()

        result.append({
            "name": box["name"],
            "top": int(top_var.value()),
            "left": margin_left,
            "width": int(box.get("w", usable_w)),
            "height": h,
        })
        prev_bottom = bottom_var

    return result


def main():
    ap = argparse.ArgumentParser(description="Solve slide layout constraints")
    ap.add_argument("--slide-w", type=int, default=9144000, help="Slide width in EMU (default: 10in)")
    ap.add_argument("--slide-h", type=int, default=5143500, help="Slide height in EMU (default: 7.5in)")
    ap.add_argument("--boxes", required=True, help='JSON array: [{"name":"title","h":800000}, ...]')
    ap.add_argument("--margin-top", type=int, default=457200)
    ap.add_argument("--margin-left", type=int, default=457200)
    ap.add_argument("--margin-right", type=int, default=457200)
    ap.add_argument("--gap", type=int, default=228600, help="Gap between boxes in EMU")
    args = ap.parse_args()

    boxes = json.loads(args.boxes)
    layout = solve(
        args.slide_w, args.slide_h, boxes,
        args.margin_top, args.margin_left, args.margin_right, args.gap,
    )
    print(json.dumps(layout, indent=2))


if __name__ == "__main__":
    main()
