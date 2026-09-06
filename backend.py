#!/usr/bin/env python3
"""FastAPI backend with Server-Sent Events streaming for the LangGraph pipeline."""
import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig

load_dotenv()

from graph import graph
from llm_factory import available_providers, default_provider, executor_model, planner_model

app = FastAPI(title="PPT Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# file_id → absolute path of generated PPTX
_outputs: dict[str, str] = {}
# file_id → directory of rendered PNG slides (AFTER / edited)
_slide_dirs: dict[str, str] = {}
# file_id → directory of rendered PNG slides (BEFORE / original template)
_before_slide_dirs: dict[str, str] = {}


_GRAPH_NODES = {"extractor", "planner", "executor", "reviewer"}


def _plan_points(plan: dict) -> list[dict]:
    """
    Turn the planner's edit plan into human-readable change points for the UI.
    One point per decided change: slide + target shape + the text going in.
    Capped so a huge plan doesn't flood the event stream.
    """
    points: list[dict] = []
    for e in plan.get("edits", []):
        slide = e.get("slide")
        shape = e.get("shape", "") or ""
        op = e.get("op", "set_text") or "set_text"
        text = (e.get("text") or "").strip().replace("\n", " / ")
        if op == "set_cell":
            loc = f"{shape} (r{e.get('row')}, c{e.get('col')})" if shape else "table cell"
        else:
            loc = shape or "(shape)"
        points.append({"slide": slide, "target": loc, "op": op, "text": text[:90]})
    for s in plan.get("slides_to_remove", []) or []:
        points.append({"slide": s, "target": "whole slide", "op": "remove_slide",
                       "text": "Remove slide — topic not covered in source"})
    for d in plan.get("delete_shapes", []) or []:
        shapes = ", ".join(d.get("shapes", []) or [])
        points.append({"slide": d.get("slide"), "target": shapes[:70] or "shape group",
                       "op": "delete_shapes",
                       "text": (d.get("reason") or "Delete excess template slots")[:90]})
    for a in plan.get("slides_to_add", []) or []:
        base = a.get("base_slide"); after = a.get("insert_after")
        purpose = (a.get("purpose") or "").strip()
        points.append({"slide": None, "target": "new slide", "op": "add_slide",
                       "text": f"Add after S{after} (clone of S{base}): {purpose}"[:90]})
    return points[:60]


def _plan_context(plan: dict) -> dict:
    """Strategic context headline fields for the UI points card."""
    ctx = plan.get("strategic_context", {}) or {}
    return {
        "insight": ctx.get("key_insight", ""),
        "recommendation": ctx.get("primary_recommendation", ""),
        "audience": ctx.get("audience_focus", ""),
    }


class _LogHandler(BaseCallbackHandler):
    def __init__(self, put_fn):
        self._put = put_fn
        self._seen_nodes: set = set()

    def on_chain_start(self, serialized: Any, inputs: dict, **kwargs: Any) -> None:
        name = (serialized or {}).get("name", "") if isinstance(serialized, dict) else ""
        if not name:
            name = kwargs.get("name", "") or ""
        if name in _GRAPH_NODES and name not in self._seen_nodes:
            self._seen_nodes.add(name)
            self._put({"type": "node_start", "node": name})

    def on_chat_model_start(self, serialized: Any, messages: list, **kwargs: Any) -> None:
        self._put({"type": "thinking"})

    def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> None:
        name = (serialized or {}).get("name", "tool") if isinstance(serialized, dict) else "tool"
        try:
            args = json.loads(input_str)
        except Exception:
            args = input_str
        self._put({"type": "tool_call", "tool": name, "args": args})

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        raw = str(output)
        try:
            parsed = json.loads(raw)
            preview = parsed
        except Exception:
            preview = raw[:400]
        self._put({"type": "tool_result", "output": preview})

    def on_tool_error(self, error: Any, **kwargs: Any) -> None:
        self._put({"type": "tool_error", "error": str(error)})


@app.get("/api/providers")
async def get_providers():
    return available_providers()


@app.post("/api/generate")
async def generate(
    template: UploadFile = File(...),
    transcript: str = Form(""),
    transcript_file: UploadFile = File(None),
    provider: str = Form(""),
):
    file_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    async_q: asyncio.Queue = asyncio.Queue()

    def put(item: dict) -> None:
        loop.call_soon_threadsafe(async_q.put_nowait, item)

    tmpdir = tempfile.mkdtemp(prefix="ppt_agent_")
    template_path = f"{tmpdir}/template.pptx"
    transcript_path = f"{tmpdir}/transcript.txt"
    output_path = f"{tmpdir}/output.pptx"

    template_bytes = await template.read()
    Path(template_path).write_bytes(template_bytes)

    transcript_content = transcript.strip()
    if not transcript_content and transcript_file:
        raw_bytes = await transcript_file.read()
        from tools.pdf_parser import is_pdf_bytes, extract_pdf_text
        if is_pdf_bytes(raw_bytes):
            pdf_tmp = Path(tmpdir) / "upload.pdf"
            pdf_tmp.write_bytes(raw_bytes)
            try:
                transcript_content = extract_pdf_text(pdf_tmp)
            except ValueError as exc:
                return {"error": str(exc)}
        else:
            transcript_content = raw_bytes.decode("utf-8", errors="replace")

    if not transcript_content:
        return {"error": "No content provided"}

    Path(transcript_path).write_text(transcript_content, encoding="utf-8")

    resolved_provider = provider.strip() or default_provider()

    state = {
        "transcript_path": transcript_path,
        "template_path": template_path,
        "output_path": output_path,
        "work_dir": tmpdir,
        "provider": resolved_provider,
        "planner_model": planner_model(resolved_provider),
        "executor_model": executor_model(resolved_provider),
        "edit_plan": {},
        "review_verdict": "",
        "review_issues": [],
        "fix_attempts": 0,
    }

    def run() -> None:
        try:
            put({
                "type": "model_info",
                "provider": resolved_provider,
                "planner_model": planner_model(resolved_provider),
                "executor_model": executor_model(resolved_provider),
            })
            handler = _LogHandler(put)
            config = RunnableConfig(callbacks=[handler], recursion_limit=200)

            for event in graph.stream(state, config=config, stream_mode="updates"):
                for node_name, node_state in event.items():
                    if node_name == "planner":
                        plan = node_state.get("edit_plan", {})
                        put({
                            "type": "plan_summary",
                            "edits": len(plan.get("edits", [])),
                            "removes": len(plan.get("slides_to_remove", [])),
                            "multi_phase": plan.get("multi_phase", False),
                            "summary": plan.get("document_summary", plan.get("transcript_summary", "")),
                            "points": _plan_points(plan),
                            **_plan_context(plan),
                        })
                    elif node_name == "executor":
                        put({"type": "executor_done", "attempt": node_state.get("fix_attempts", 0)})
                    elif node_name == "reviewer":
                        verdict = node_state.get("review_verdict", "")
                        put({
                            "type": "reviewer_done",
                            "verdict": verdict,
                            "issues": node_state.get("review_issues", []),
                        })

            if Path(output_path).exists():
                stable = Path(tempfile.gettempdir()) / f"{file_id}.pptx"
                shutil.copy(output_path, stable)
                _outputs[file_id] = str(stable)
                put({"type": "done", "file_id": file_id})

                # Try to render slides to PNG for in-browser preview
                try:
                    from tools.pptx_editor import op_render_png
                    slide_dir = Path(tempfile.gettempdir()) / f"{file_id}_slides"
                    result = op_render_png(str(stable), str(slide_dir))
                    if not result.startswith("ERROR"):
                        pngs = sorted(slide_dir.glob("slide*.png"))
                        if pngs:
                            _slide_dirs[file_id] = str(slide_dir)
                            put({"type": "slides_ready", "file_id": file_id, "count": len(pngs)})
                except Exception:
                    pass  # preview is optional
            else:
                put({"type": "error", "msg": "Pipeline finished but no output file was produced."})

        except Exception as exc:
            put({"type": "error", "msg": str(exc)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    loop.run_in_executor(None, run)

    async def stream():
        while True:
            try:
                item = await asyncio.wait_for(async_q.get(), timeout=180.0)
            except asyncio.TimeoutError:
                yield "data: {\"type\":\"ping\"}\n\n"
                continue
            yield f"data: {json.dumps(item)}\n\n"
            if item["type"] in ("done", "error"):
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/slides/{file_id}")
async def list_slides(file_id: str):
    slide_dir = _slide_dirs.get(file_id)
    if not slide_dir:
        return []
    pngs = sorted(Path(slide_dir).glob("slide*.png"))
    return [{"index": i, "url": f"/api/slides/{file_id}/{i}"} for i, _ in enumerate(pngs)]


@app.get("/api/slides/{file_id}/{index}")
async def get_slide(file_id: str, index: int):
    from fastapi import HTTPException
    slide_dir = _slide_dirs.get(file_id)
    if not slide_dir:
        raise HTTPException(status_code=404, detail="No slides rendered for this file")
    pngs = sorted(Path(slide_dir).glob("slide*.png"))
    if index < 0 or index >= len(pngs):
        raise HTTPException(status_code=404, detail="Slide index out of range")
    return FileResponse(str(pngs[index]), media_type="image/png")


# ── Diff Lab: before/after slide comparison ────────────────────────────────

@app.post("/api/diff/generate")
async def diff_generate(
    template: UploadFile = File(...),
    transcript: str = Form(""),
    transcript_file: UploadFile = File(None),
    provider: str = Form(""),
):
    """Run the full pipeline, then stream a before/after diff payload.

    Same flow as /api/generate, plus: renders the ORIGINAL template to PNGs
    (before), keeps the full edit_plan, and emits a `diff_ready` event grouping
    edits by slide with before_text / after_text / evidence per shape.
    """
    file_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    async_q: asyncio.Queue = asyncio.Queue()

    def put(item: dict) -> None:
        loop.call_soon_threadsafe(async_q.put_nowait, item)

    tmpdir = tempfile.mkdtemp(prefix="ppt_agent_diff_")
    template_path = f"{tmpdir}/template.pptx"
    transcript_path = f"{tmpdir}/transcript.txt"
    output_path = f"{tmpdir}/output.pptx"

    template_bytes = await template.read()
    Path(template_path).write_bytes(template_bytes)

    transcript_content = transcript.strip()
    if not transcript_content and transcript_file:
        raw_bytes = await transcript_file.read()
        from tools.pdf_parser import is_pdf_bytes, extract_pdf_text
        if is_pdf_bytes(raw_bytes):
            pdf_tmp = Path(tmpdir) / "upload.pdf"
            pdf_tmp.write_bytes(raw_bytes)
            try:
                transcript_content = extract_pdf_text(pdf_tmp)
            except ValueError as exc:
                return {"error": str(exc)}
        else:
            transcript_content = raw_bytes.decode("utf-8", errors="replace")

    if not transcript_content:
        return {"error": "No content provided"}

    Path(transcript_path).write_text(transcript_content, encoding="utf-8")

    resolved_provider = provider.strip() or default_provider()

    state = {
        "transcript_path": transcript_path,
        "template_path": template_path,
        "output_path": output_path,
        "work_dir": tmpdir,
        "provider": resolved_provider,
        "planner_model": planner_model(resolved_provider),
        "executor_model": executor_model(resolved_provider),
        "edit_plan": {},
        "review_verdict": "",
        "review_issues": [],
        "fix_attempts": 0,
    }

    def _build_diff_slides(edit_plan: dict, before_map: dict) -> list[dict]:
        """Group edit_plan.edits by slide and join before/after text + evidence."""
        by_slide: dict[int, list[dict]] = {}
        for edit in edit_plan.get("edits", []):
            slide = edit.get("slide")
            if not isinstance(slide, int):
                continue
            shape = edit.get("shape", "")
            op = edit.get("op", "")
            after_text = edit.get("text", "")
            evidence = edit.get("evidence", "")
            if op == "set_cell":
                # table cells aren't exposed by extract_pptx_text — show coords only
                r, c = edit.get("row"), edit.get("col")
                label = f"{shape} (r{r},c{c})" if r is not None else shape
                before_text = ""
            else:
                label = shape
                before_text = before_map.get(f"slide_{slide}", {}).get(shape, "")
            by_slide.setdefault(slide, []).append({
                "shape": label,
                "op": op,
                "before_text": before_text,
                "after_text": after_text,
                "evidence": evidence,
            })
        return [{"slide": s, "edits": by_slide[s]} for s in sorted(by_slide)]

    def run() -> None:
        try:
            from tools.pptx_editor import op_render_png
            from eval_agent.extract_pptx_text import extract_pptx_text

            put({
                "type": "model_info",
                "provider": resolved_provider,
                "planner_model": planner_model(resolved_provider),
                "executor_model": executor_model(resolved_provider),
            })

            # 1) render the ORIGINAL template → before PNGs
            before_dir = Path(tempfile.gettempdir()) / f"{file_id}_before"
            before_result = op_render_png(template_path, str(before_dir))
            if not before_result.startswith("ERROR"):
                before_pngs = sorted(before_dir.glob("slide*.png"))
                if before_pngs:
                    _before_slide_dirs[file_id] = str(before_dir)
                    put({"type": "before_ready", "file_id": file_id, "count": len(before_pngs)})

            # 2) before-text map for per-shape diffs
            before_map = extract_pptx_text(template_path)

            # 3) run the pipeline, capturing the full edit_plan
            captured_plan: dict = {}
            handler = _LogHandler(put)
            config = RunnableConfig(callbacks=[handler], recursion_limit=200)

            for event in graph.stream(state, config=config, stream_mode="updates"):
                for node_name, node_state in event.items():
                    if node_name == "planner":
                        captured_plan = node_state.get("edit_plan", {}) or {}
                        put({
                            "type": "plan_summary",
                            "edits": len(captured_plan.get("edits", [])),
                            "removes": len(captured_plan.get("slides_to_remove", [])),
                            "multi_phase": captured_plan.get("multi_phase", False),
                            "summary": captured_plan.get("document_summary",
                                                         captured_plan.get("transcript_summary", "")),
                            "points": _plan_points(captured_plan),
                            **_plan_context(captured_plan),
                        })
                    elif node_name == "executor":
                        put({"type": "executor_done", "attempt": node_state.get("fix_attempts", 0)})
                    elif node_name == "reviewer":
                        put({
                            "type": "reviewer_done",
                            "verdict": node_state.get("review_verdict", ""),
                            "issues": node_state.get("review_issues", []),
                        })

            if not Path(output_path).exists():
                put({"type": "error", "msg": "Pipeline finished but no output file was produced."})
                return

            stable = Path(tempfile.gettempdir()) / f"{file_id}.pptx"
            shutil.copy(output_path, stable)
            _outputs[file_id] = str(stable)

            # 4) render the EDITED deck → after PNGs
            after_dir = Path(tempfile.gettempdir()) / f"{file_id}_slides"
            after_result = op_render_png(str(stable), str(after_dir))
            if not after_result.startswith("ERROR"):
                after_pngs = sorted(after_dir.glob("slide*.png"))
                if after_pngs:
                    _slide_dirs[file_id] = str(after_dir)
                    put({"type": "slides_ready", "file_id": file_id, "count": len(after_pngs)})

            # 5) before/after diff grouped by slide
            put({
                "type": "diff_ready",
                "file_id": file_id,
                "slides": _build_diff_slides(captured_plan, before_map),
            })

            # 6) done
            put({"type": "done", "file_id": file_id})

        except Exception as exc:
            put({"type": "error", "msg": str(exc)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    loop.run_in_executor(None, run)

    async def stream():
        while True:
            try:
                item = await asyncio.wait_for(async_q.get(), timeout=180.0)
            except asyncio.TimeoutError:
                yield "data: {\"type\":\"ping\"}\n\n"
                continue
            yield f"data: {json.dumps(item)}\n\n"
            if item["type"] in ("done", "error"):
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/diff/before/{file_id}/{index}")
async def get_before_slide(file_id: str, index: int):
    from fastapi import HTTPException
    slide_dir = _before_slide_dirs.get(file_id)
    if not slide_dir:
        raise HTTPException(status_code=404, detail="No before-slides rendered for this file")
    pngs = sorted(Path(slide_dir).glob("slide*.png"))
    if index < 0 or index >= len(pngs):
        raise HTTPException(status_code=404, detail="Slide index out of range")
    return FileResponse(str(pngs[index]), media_type="image/png")


@app.get("/api/download/{file_id}")
async def download(file_id: str):
    path = _outputs.get(file_id)
    if not path or not Path(path).exists():
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="File not found or expired")
    return FileResponse(
        path,
        filename="edited.pptx",
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


# ── Eval Flywheel endpoints ────────────────────────────────────────────────

_EVAL_TRANSCRIPTS_DIR = Path(__file__).parent / "eval_transcripts"


@app.get("/api/eval/transcripts")
async def eval_transcripts():
    """List available eval transcripts."""
    if not _EVAL_TRANSCRIPTS_DIR.exists():
        return []
    paths = sorted(_EVAL_TRANSCRIPTS_DIR.glob("*.txt"))
    return [{"id": p.stem, "name": p.name, "size_kb": round(p.stat().st_size / 1024, 1)} for p in paths]


@app.post("/api/eval/run")
async def eval_run(
    template: UploadFile = File(...),
    provider: str = Form(""),
    transcript_ids: str = Form(""),  # comma-separated list; empty = all
):
    """SSE stream of batch eval progress across all (or selected) transcripts."""
    from eval_agent.benchmark import run_benchmark

    loop = asyncio.get_event_loop()
    async_q: asyncio.Queue = asyncio.Queue()

    def put(item: dict) -> None:
        loop.call_soon_threadsafe(async_q.put_nowait, item)

    tmpdir = tempfile.mkdtemp(prefix="eval_run_")
    template_path = f"{tmpdir}/template.pptx"
    Path(template_path).write_bytes(await template.read())

    resolved_provider = provider.strip() or default_provider()

    # Resolve transcript paths
    all_tx = sorted(_EVAL_TRANSCRIPTS_DIR.glob("*.txt")) if _EVAL_TRANSCRIPTS_DIR.exists() else []
    if transcript_ids.strip():
        ids_wanted = {t.strip() for t in transcript_ids.split(",") if t.strip()}
        tx_paths = [str(p) for p in all_tx if p.stem in ids_wanted]
    else:
        tx_paths = [str(p) for p in all_tx]

    if not tx_paths:
        return {"error": "No transcript files found in eval_transcripts/"}

    def run() -> None:
        try:
            run_benchmark(template_path, tx_paths, resolved_provider, on_progress=put)
        except Exception as exc:
            put({"type": "error", "msg": str(exc)})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    loop.run_in_executor(None, run)

    async def stream():
        while True:
            try:
                item = await asyncio.wait_for(async_q.get(), timeout=600.0)
            except asyncio.TimeoutError:
                yield 'data: {"type":"ping"}\n\n'
                continue
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("type") in ("eval_done", "error"):
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/eval/results")
async def eval_results():
    """Return all benchmark run results."""
    from eval_agent.benchmark import load_all_results
    results = load_all_results()
    return {"runs": results, "count": len(results)}


@app.post("/api/eval/improve")
async def eval_improve(eval_name: str = Form(...), provider: str = Form("")):
    """Cluster failures for eval_name and return Sonnet's patch suggestion."""
    from eval_agent.auto_improve import auto_improve
    resolved_provider = provider.strip() or default_provider()
    result = auto_improve(eval_name, resolved_provider)
    return result


@app.post("/api/eval/apply-patch")
async def eval_apply_patch(patch_id: str = Form(...)):
    """Apply an approved staged patch to agents/planner.py. Human-triggered only."""
    from eval_agent.auto_improve import apply_patch
    return apply_patch(patch_id)


@app.post("/api/eval/calibrate")
async def eval_calibrate(
    eval_name: str = Form(...),
    labels: str = Form(...),  # JSON string: [{"transcript_id": "...", "passed": true}]
):
    """Compute F1 score for an LLM eval judge vs human labels."""
    from eval_agent.calibrate import calibrate, save_human_labels
    try:
        human_labels = json.loads(labels)
    except Exception:
        return {"error": "Invalid JSON in labels field"}
    save_human_labels(eval_name, human_labels)
    return calibrate(eval_name, human_labels)


# Serve built React app in production
_dist = Path(__file__).parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="0.0.0.0", port=8000, reload=True)
