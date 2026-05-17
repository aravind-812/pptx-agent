#!/usr/bin/env python3
"""CLI entrypoint for the LangGraph PPTX proposal pipeline.

Usage:
  python run.py --transcript transcripts/01.txt \
                --template pptx_templates/ABC\ Corp\ Template.pptx \
                --out runs/output_01.pptx
"""
import argparse
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from graph import graph


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a Ressl PPTX proposal from a transcript")
    ap.add_argument("--transcript", required=True, help="Path to anonymized transcript .txt")
    ap.add_argument("--template", required=True, help="Path to base PPTX template")
    ap.add_argument("--out", required=True, help="Output PPTX path")
    ap.add_argument("--work-dir", default="runs/work", help="Work directory for temp files (PNGs etc)")
    args = ap.parse_args()

    Path(args.work_dir).mkdir(parents=True, exist_ok=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    result = graph.invoke({
        "transcript_path": args.transcript,
        "template_path": args.template,
        "output_path": args.out,
        "work_dir": args.work_dir,
        "edit_plan": {},
        "review_verdict": "",
        "review_issues": [],
        "fix_attempts": 0,
    })

    verdict = result.get("review_verdict", "unknown")
    issues = result.get("review_issues", [])
    print(f"\nDone. Verdict: {verdict}")
    print(f"Output: {args.out}")
    if issues:
        print(f"Remaining issues ({len(issues)}):")
        for issue in issues:
            print(f"  - {issue}")


if __name__ == "__main__":
    main()
