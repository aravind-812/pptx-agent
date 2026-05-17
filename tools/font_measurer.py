#!/usr/bin/env python3
"""Measure text dimensions using Pillow. Falls back to estimation if font is unavailable."""
import sys
import json
import argparse

def measure(text: str, font_size: int, font_path: str | None = None) -> dict:
    try:
        from PIL import ImageFont, ImageDraw, Image
        if font_path:
            font = ImageFont.truetype(font_path, font_size)
        else:
            font = ImageFont.load_default(size=font_size)
        img = Image.new("RGB", (4000, 2000))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), text, font=font)
        return {"width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1], "source": "pillow"}
    except Exception:
        # Simple fallback: ~0.55x width per char, 1.2x height per size unit
        return {
            "width": int(len(text) * font_size * 0.55),
            "height": int(font_size * 1.2),
            "source": "estimate",
        }

def main():
    ap = argparse.ArgumentParser(description="Measure rendered text dimensions")
    ap.add_argument("text")
    ap.add_argument("font_size", type=int)
    ap.add_argument("--font", default=None, help="Path to .ttf font file")
    args = ap.parse_args()
    print(json.dumps(measure(args.text, args.font_size, args.font)))

if __name__ == "__main__":
    main()
