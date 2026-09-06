"""Eval runner: calls graph.invoke() directly and captures full PipelineState."""
import tempfile
import time
from pathlib import Path
from typing import Optional

from eval_agent.extract_pptx_text import extract_pptx_text
from graph import graph
from llm_factory import default_provider, executor_model, planner_model


def run_eval_case(
    transcript_path: str,
    template_path: str,
    provider: Optional[str] = None,
    run_dir: Optional[str] = None,
) -> dict:
    """
    Run the full pipeline for one (transcript, template) pair.

    Args:
        transcript_path: path to transcript .txt file
        template_path:   path to PPTX template
        provider:        'anthropic' | 'openai' | None (auto-detect)
        run_dir:         directory for intermediate files (caller manages cleanup)

    Returns:
        {
          state:          final PipelineState dict
          pptx_text:      {slide_1: {shape: text}, ..., all_text: str}
          transcript_id:  stem of transcript filename
          output_path:    path to generated PPTX (or None on failure)
          duration_sec:   float
          error:          str | None
        }
    """
    provider = provider or default_provider()
    transcript_id = Path(transcript_path).stem

    if run_dir is None:
        run_dir = tempfile.mkdtemp(prefix="eval_run_")

    output_path = str(Path(run_dir) / f"{transcript_id}_output.pptx")

    state = {
        "transcript_path": str(transcript_path),
        "template_path": str(template_path),
        "output_path": output_path,
        "work_dir": run_dir,
        "provider": provider,
        "planner_model": planner_model(provider),
        "executor_model": executor_model(provider),
        "edit_plan": {},
        "review_verdict": "",
        "review_issues": [],
        "fix_attempts": 0,
    }

    t0 = time.time()
    try:
        final_state = graph.invoke(state)
        duration = time.time() - t0

        pptx_text: dict = {}
        if Path(output_path).exists():
            pptx_text = extract_pptx_text(output_path)

        return {
            "state": dict(final_state),
            "pptx_text": pptx_text,
            "transcript_id": transcript_id,
            "output_path": output_path if Path(output_path).exists() else None,
            "duration_sec": round(duration, 2),
            "error": None,
        }

    except Exception as exc:
        return {
            "state": state,
            "pptx_text": {},
            "transcript_id": transcript_id,
            "output_path": None,
            "duration_sec": round(time.time() - t0, 2),
            "error": str(exc),
        }
