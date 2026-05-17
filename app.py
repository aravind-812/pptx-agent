#!/usr/bin/env python3
"""Gradio frontend for the LangGraph PPTX proposal pipeline.

Run:
  python app.py

Then open http://localhost:7860
"""
import json
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import gradio as gr
from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig

load_dotenv()

from graph import graph


# ---------------------------------------------------------------------------
# Callback handler — captures tool calls + LLM activity into a queue
# ---------------------------------------------------------------------------
class _LogHandler(BaseCallbackHandler):
    def __init__(self, q: queue.Queue):
        self.q = q

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs: Any) -> None:
        self.q.put("  💭 model reasoning...\n")

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs: Any) -> None:
        name = serialized.get("name", "tool")
        try:
            args = json.loads(input_str) if input_str.strip().startswith("{") else input_str
            if isinstance(args, dict):
                preview = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items())
            else:
                preview = str(args)[:80]
        except Exception:
            preview = str(input_str)[:80]
        self.q.put(f"  → {name}({preview})\n")

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        out = str(output).strip()
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                preview = ", ".join(
                    f"{k}: {str(v)[:30]}"
                    for k, v in list(parsed.items())[:4]
                )
            else:
                preview = out[:100]
        except Exception:
            preview = out[:100]
        self.q.put(f"  ✓ {preview}\n")

    def on_tool_error(self, error: Any, **kwargs: Any) -> None:
        self.q.put(f"  ✗ ERROR: {error}\n")


# ---------------------------------------------------------------------------
# Node-level labels injected between tool calls
# ---------------------------------------------------------------------------
_NODE_LABELS = {
    "planner": "🧠 PLANNER — reading transcript and planning edits",
    "executor": "⚙  EXECUTOR — applying edits to PPTX",
    "reviewer": "🔍 REVIEWER — validating output",
}

_DONE = object()


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------
def run_pipeline(template_file, transcript_text: str, transcript_file):
    if template_file is None:
        yield "⚠ Upload a PPTX template first.", None
        return

    transcript_content = ""
    if transcript_text and transcript_text.strip():
        transcript_content = transcript_text.strip()
    elif transcript_file is not None:
        transcript_content = Path(transcript_file.name).read_text(encoding="utf-8")

    if not transcript_content:
        yield "⚠ Provide transcript text or upload a .txt file.", None
        return

    tmpdir = tempfile.mkdtemp(prefix="ppt_agent_")
    template_path = f"{tmpdir}/template.pptx"
    transcript_path = f"{tmpdir}/transcript.txt"
    output_path = f"{tmpdir}/output.pptx"

    shutil.copy(template_file.name, template_path)
    Path(transcript_path).write_text(transcript_content, encoding="utf-8")

    state = {
        "transcript_path": transcript_path,
        "template_path": template_path,
        "output_path": output_path,
        "work_dir": tmpdir,
        "edit_plan": {},
        "review_verdict": "",
        "review_issues": [],
        "fix_attempts": 0,
    }

    log_q: queue.Queue = queue.Queue()
    result: dict = {"output_path": None, "error": None, "final_state": None}
    _seen_nodes: set = set()

    def _run():
        try:
            config = RunnableConfig(
                callbacks=[_LogHandler(log_q)],
                recursion_limit=200,
            )

            for event in graph.stream(state, config=config, stream_mode="updates"):
                for node_name, node_state in event.items():
                    if node_name not in _seen_nodes:
                        _seen_nodes.add(node_name)
                        label = _NODE_LABELS.get(node_name, f"[{node_name.upper()}]")
                        log_q.put(f"\n{'─' * 50}\n{label}\n{'─' * 50}\n")

                    # Post-node summary
                    if node_name == "planner":
                        plan = node_state.get("edit_plan", {})
                        n_edits = len(plan.get("edits", []))
                        n_remove = len(plan.get("slides_to_remove", []))
                        summary = plan.get("transcript_summary", "")
                        multi = plan.get("multi_phase", False)
                        log_q.put(
                            f"\n  📋 Plan ready — {n_edits} edits, "
                            f"{n_remove} slides to remove, "
                            f"multi-phase={multi}\n"
                            f"  {summary}\n"
                        )

                    elif node_name == "executor":
                        attempts = node_state.get("fix_attempts", 0)
                        pass_label = "fix pass" if attempts > 1 else "initial pass"
                        log_q.put(f"\n  ✅ Executor {pass_label} complete.\n")

                    elif node_name == "reviewer":
                        verdict = node_state.get("review_verdict", "")
                        issues = node_state.get("review_issues", [])
                        icon = "✅" if verdict == "PASS" else "⚠"
                        log_q.put(f"\n  {icon} VERDICT: {verdict}\n")
                        for issue in issues:
                            log_q.put(f"    • {issue}\n")

            result["final_state"] = node_state  # last state
            if Path(output_path).exists():
                stable = Path(tempfile.gettempdir()) / f"ppt_agent_{int(time.time())}.pptx"
                shutil.copy(output_path, stable)
                result["output_path"] = str(stable)

        except Exception as exc:
            result["error"] = str(exc)
        finally:
            log_q.put(_DONE)
            shutil.rmtree(tmpdir, ignore_errors=True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    log = "Pipeline started...\n"
    yield log, None

    while True:
        try:
            msg = log_q.get(timeout=120)
        except queue.Empty:
            log += "\n(still running — waiting for model...)\n"
            yield log, None
            continue

        if msg is _DONE:
            break
        log += msg
        yield log, None

    if result["error"]:
        log += f"\n\n❌ Error: {result['error']}\n"
        yield log, None
    elif result["output_path"]:
        verdict = (result.get("final_state") or {}).get("review_verdict", "")
        log += f"\n\n{'=' * 50}\n✅ Done! Verdict: {verdict}\nDownload your file below.\n"
        yield log, result["output_path"]
    else:
        log += "\n\n❌ No output file produced.\n"
        yield log, None


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
css = """
.log-box textarea {
    font-family: 'JetBrains Mono', 'Courier New', monospace !important;
    font-size: 12px !important;
    background: #1a1a2e !important;
    color: #e0e0e0 !important;
}
"""

with gr.Blocks(title="PPTX Proposal Generator") as demo:
    gr.Markdown(
        "# 📊 PPTX Proposal Generator\n"
        "Upload a PPTX template and a customer transcript → get a tailored proposal deck.\n"
        "The pipeline runs: **Planner → Executor → Reviewer** (with auto-fix loop)."
    )

    with gr.Row(equal_height=False):
        # Left column: inputs
        with gr.Column(scale=1, min_width=320):
            gr.Markdown("### Inputs")
            template_input = gr.File(
                label="PPTX Template",
                file_types=[".pptx"],
            )
            transcript_text = gr.Textbox(
                label="Customer Transcript",
                placeholder="Paste the anonymized customer transcript here...",
                lines=18,
            )
            gr.Markdown("*— or upload a .txt file —*")
            transcript_file = gr.File(
                label="Transcript (.txt)",
                file_types=[".txt"],
            )
            run_btn = gr.Button("🚀 Generate Proposal", variant="primary", size="lg")

        # Right column: live log + output
        with gr.Column(scale=1, min_width=400):
            gr.Markdown("### Live Activity")
            log_output = gr.Textbox(
                label="",
                lines=28,
                interactive=False,
                elem_classes=["log-box"],
                placeholder="Output will appear here once you click Generate...",
            )
            gr.Markdown("### Output")
            file_output = gr.File(
                label="Download Generated PPTX",
                interactive=False,
            )

    run_btn.click(
        fn=run_pipeline,
        inputs=[template_input, transcript_text, transcript_file],
        outputs=[log_output, file_output],
    )

    gr.Markdown(
        "---\n"
        "*Powered by LangGraph + Claude Sonnet 4.6. "
        "Requires `ANTHROPIC_API_KEY` in `.env`.*"
    )


if __name__ == "__main__":
    demo.launch(
        share=False,
        server_name="0.0.0.0",
        server_port=7860,
        theme=gr.themes.Soft(),
        css=css,
    )
