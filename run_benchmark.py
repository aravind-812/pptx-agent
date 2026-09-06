#!/usr/bin/env python3
"""Benchmark runner for the LangGraph PPTX pipeline.

Runs anonymized transcripts through run.py sequentially, capturing logs,
timing, and output quality metrics. Compatible with ressl-pptx-eval scoring.

Usage:
  python run_benchmark.py                          # all transcripts
  python run_benchmark.py --transcripts 01,02,03   # specific IDs
  python run_benchmark.py --timeout-sec 1200        # longer timeout
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESSL_EVAL = ROOT.parent / "ressl-pptx-eval"
TRANSCRIPT_DIR = RESSL_EVAL / "implementation_proposal_curated_35_anonymized"
TEMPLATE = RESSL_EVAL / "pptx_templates" / "ABC Corp Template.pptx"
RUNS_DIR = ROOT / "runs"
RUN_PY = ROOT / "run.py"

FORBIDDEN = [
    "ComplianceQuest", "Compliance Quest", "compliancequest",
    "Insert Customer Logo", "Enter Current State", "Process..", "Process…",
    "TODO", "Only Applicable", "Speaker:", "TIMESTAMP",
]


def now_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def transcript_id(path: Path) -> str:
    m = re.match(r"(\d+)_", path.name)
    return m.group(1) if m else path.stem


def inspect_pptx(path: Path) -> tuple[int | None, list[str]]:
    if not path.exists():
        return None, []
    hits: list[str] = []
    slide_count: int | None = None
    try:
        with zipfile.ZipFile(path) as z:
            slide_names = sorted(
                [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                key=lambda n: int(re.search(r"slide(\d+)\.xml", n).group(1)),
            )
            slide_count = len(slide_names)
            for name in slide_names:
                data = z.read(name).decode("utf-8", errors="ignore")
                slide_no = re.search(r"slide(\d+)\.xml", name).group(1)
                for bad in FORBIDDEN:
                    if bad in data:
                        hits.append(f"slide {slide_no}: {bad}")
                if re.search(r"(?<![A-Za-z])CQ(?![A-Za-z])", data):
                    hits.append(f"slide {slide_no}: standalone CQ")
    except Exception as e:
        hits.append(f"pptx_inspect_error: {e}")
    return slide_count, hits


def run_one(
    transcript: Path,
    template: Path,
    bench_dir: Path,
    timeout_sec: int,
    provider: str = "",
) -> dict:
    tid = transcript_id(transcript)
    run_id = f"{now_id()}_{tid}_langgraph"
    run_dir = RUNS_DIR / run_id
    for subdir in ("output", "logs", "work"):
        (run_dir / subdir).mkdir(parents=True, exist_ok=True)

    out_path = run_dir / "output" / f"{tid}_langgraph.pptx"
    log_path = run_dir / "logs" / "agent.log"
    attempt_log = bench_dir / "attempt_logs" / f"{tid}_langgraph.log"
    attempt_log.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(RUN_PY),
        "--transcript", str(transcript),
        "--template", str(template),
        "--out", str(out_path),
        "--work-dir", str(run_dir / "work"),
    ]
    if provider:
        cmd += ["--provider", provider]

    start = time.time()
    timed_out = False
    rc: int | None = None
    stdout_text = ""

    with log_path.open("w", buffering=1) as log:
        log.write("CMD: " + " ".join(cmd) + "\n\n")
        proc = subprocess.Popen(
            cmd, cwd=ROOT, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
        )
        try:
            stdout_text, _ = proc.communicate(timeout=timeout_sec)
            rc = proc.returncode
            log.write(stdout_text)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(3)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            rc = 124
            log.write(f"\nTIMEOUT after {timeout_sec}s\n")

    attempt_log.write_text(log_path.read_text(errors="ignore"))

    elapsed = round(time.time() - start, 1)
    pptx_exists = out_path.exists() and out_path.stat().st_size > 1000
    slide_count, forbidden_hits = inspect_pptx(out_path) if pptx_exists else (None, [])

    if timed_out:
        status = "timeout"
    elif rc == 0 and pptx_exists:
        status = "success"
    else:
        status = "failed"

    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "transcript_id": tid,
        "transcript": str(transcript),
        "model_key": "langgraph_sonnet46",
        "model_route": "langgraph",
        "status": status,
        "exit_code": rc,
        "timed_out": timed_out,
        "duration_sec": elapsed,
        "run_id": run_id,
        "output": str(out_path) if pptx_exists else "",
        "pptx_exists": pptx_exists,
        "slide_count": slide_count,
        "forbidden_hits": forbidden_hits,
        "attempt_log": str(attempt_log),
        "log": str(log_path),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "transcript_id", "model_key", "status", "duration_sec", "run_id",
        "output", "pptx_exists", "slide_count", "forbidden_hits",
        "exit_code", "timed_out", "ts",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = dict(r)
            row["forbidden_hits"] = "; ".join(r.get("forbidden_hits") or [])
            w.writerow({k: row.get(k, "") for k in fields})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcripts", help="Comma-separated IDs, e.g. 01,02. Default: all.")
    ap.add_argument("--timeout-sec", type=int, default=900)
    ap.add_argument("--benchmark-id", default=f"benchmark_{now_id()}_langgraph")
    ap.add_argument("--transcript-dir", default=str(TRANSCRIPT_DIR),
                    help="Directory containing *_transcript.txt or *_anonymized_transcript.txt files")
    ap.add_argument("--template", default=str(TEMPLATE), help="Base PPTX template path")
    ap.add_argument("--provider", default="", help="LLM provider: anthropic or openai (default: auto-detect)")
    args = ap.parse_args()

    transcript_dir = Path(args.transcript_dir)
    template = Path(args.template)

    if not transcript_dir.exists():
        raise SystemExit(f"Transcript dir not found: {transcript_dir}")
    if not template.exists():
        raise SystemExit(f"Template not found: {template}")

    all_transcripts = sorted(transcript_dir.glob("*_anonymized_transcript.txt"))
    if not all_transcripts:
        all_transcripts = sorted(transcript_dir.glob("*_transcript.txt"))
    if args.transcripts:
        ids = {v.strip().zfill(2) for v in args.transcripts.split(",")}
        all_transcripts = [t for t in all_transcripts if transcript_id(t) in ids]
    if not all_transcripts:
        raise SystemExit("No transcripts found")

    bench_dir = RUNS_DIR / args.benchmark_id
    bench_dir.mkdir(parents=True, exist_ok=True)
    csv_path = bench_dir / "summary.csv"
    jsonl_path = bench_dir / "benchmark.jsonl"

    print(f"Benchmark: {bench_dir}")
    print(f"Transcripts: {len(all_transcripts)}")
    print(f"Template: {template}")
    print(f"Timeout: {args.timeout_sec}s per transcript\n")

    rows: list[dict] = []
    for i, t in enumerate(all_transcripts, 1):
        tid = transcript_id(t)
        print(f"=== Job {i}/{len(all_transcripts)}: transcript {tid} ===")
        result = run_one(t, template, bench_dir, args.timeout_sec, provider=args.provider)
        rows.append(result)
        with jsonl_path.open("a") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
        write_csv(csv_path, rows)
        print(
            f"  {result['status'].upper()} | "
            f"{result['duration_sec']}s | "
            f"slides={result['slide_count']} | "
            f"forbidden={len(result['forbidden_hits'])}\n"
        )

    successes = sum(1 for r in rows if r["status"] == "success")
    print(f"Done. {successes}/{len(rows)} succeeded.")
    print(f"Summary: {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
