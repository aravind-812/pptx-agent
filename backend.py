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
# file_id → directory of rendered PNG slides
_slide_dirs: dict[str, str] = {}


class _LogHandler(BaseCallbackHandler):
    def __init__(self, put_fn):
        self._put = put_fn

    def on_chat_model_start(self, serialized: dict, messages: list, **kwargs: Any) -> None:
        self._put({"type": "thinking"})

    def on_tool_start(self, serialized: dict, input_str: str, **kwargs: Any) -> None:
        name = serialized.get("name", "tool")
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
        transcript_content = (await transcript_file.read()).decode("utf-8")

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
            config = RunnableConfig(callbacks=[_LogHandler(put)], recursion_limit=200)
            seen_nodes: set = set()

            for event in graph.stream(state, config=config, stream_mode="updates"):
                for node_name, node_state in event.items():
                    if node_name not in seen_nodes:
                        seen_nodes.add(node_name)
                        put({"type": "node_start", "node": node_name})

                    if node_name == "planner":
                        plan = node_state.get("edit_plan", {})
                        put({
                            "type": "plan_summary",
                            "edits": len(plan.get("edits", [])),
                            "removes": len(plan.get("slides_to_remove", [])),
                            "multi_phase": plan.get("multi_phase", False),
                            "summary": plan.get("document_summary", plan.get("transcript_summary", "")),
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


# Serve built React app in production
_dist = Path(__file__).parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend:app", host="0.0.0.0", port=8000, reload=True)
