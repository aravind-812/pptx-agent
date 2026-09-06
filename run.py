#!/usr/bin/env python3
"""CLI entrypoint for the LangGraph PPTX proposal pipeline.

Usage:
  python run.py --transcript transcripts/01.txt \
                --template "pptx_templates/ABC Corp Template.pptx" \
                --out runs/output_01.pptx
"""
import argparse
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from graph import graph
from llm_factory import default_provider, executor_model, planner_model


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a Ressl PPTX proposal from a transcript")
    ap.add_argument("--transcript", required=True, help="Path to anonymized transcript .txt")
    ap.add_argument("--template", required=True, help="Path to base PPTX template")
    ap.add_argument("--out", required=True, help="Output PPTX path")
    ap.add_argument("--work-dir", default="runs/work", help="Work directory for temp files (PNGs etc)")
    ap.add_argument("--provider", default="", help="LLM provider: anthropic or openai (default: auto-detect from env)")
    args = ap.parse_args()

    Path(args.work_dir).mkdir(parents=True, exist_ok=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    transcript_path = args.transcript
    from tools.pdf_parser import is_pdf, extract_pdf_text
    if is_pdf(transcript_path):
        txt_path = Path(args.work_dir) / "transcript.txt"
        txt_path.write_text(extract_pdf_text(transcript_path), encoding="utf-8")
        transcript_path = str(txt_path)

    provider = args.provider.strip() or default_provider()

    result = graph.invoke({
        "transcript_path": transcript_path,
        "template_path": args.template,
        "output_path": args.out,
        "work_dir": args.work_dir,
        "provider": provider,
        "planner_model": planner_model(provider),
        "executor_model": executor_model(provider),
        "extracted_facts": None,
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
