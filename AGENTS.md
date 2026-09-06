# PPTX Agent

Multi-agent AI pipeline that edits PPTX templates from a content document (brief/notes/transcript).

## Pipeline (LangGraph StateGraph)

1. **Extractor** (`agents/extractor.py`) — distils raw transcripts into structured JSON facts (chunked 60K chars). Filters scheduling/smalltalk; keeps decisions, timeline, budget, modules, stakeholders.
2. **Planner** (`agents/planner.py`) — reads shape inventory + source doc → JSON edit plan (`edits`, `slides_to_remove`, `slides_to_add`, `dense_slides`, `strategic_context`, `decision_inventory`). Repurposing a slide = FULL content rewrite; new concepts with no home clone a slide via `slides_to_add`. Every decision in `decision_inventory` must map to an edit.
3. **Executor** (`agents/executor.py`) — structural ops first (removals + clone via `op_apply_structure`, index remap so downstream refs land on the right slides) → preflight shape resolution → deterministic batch apply → LLM fallback only if batch fails → finalisation (relayout + placeholder sweep + verify + shrink-to-fit). Happy path: zero LLM cost.
4. **Reviewer** (`agents/reviewer.py`) — deterministic pre-check + checklist on rendered PNGs (overflow, blank, invisible text, placeholders, overlap, layout, grounding, sanitization, content wastage).
5. **Fix pass** — re-runs executor on flagged slides only if FIX NEEDED.

## Stack

LangGraph + LangChain ReAct, python-pptx, FastAPI + SSE, React/Vite/Tailwind frontend, LibreOffice optional for PNG QA.

## Commands

```bash
pip install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
python3 backend.py   # http://localhost:8000
```

CLI: `python3 run.py`, benchmarks: `python3 run_benchmark.py`

## Key paths

- `backend.py` — FastAPI entry
- `graph.py` — LangGraph wiring
- `llm_factory.py` — provider selection (Anthropic priority over OpenAI)
- `agents/` — extractor, planner, executor, reviewer
- `tools/pptx_editor.py`, `tools/wrappers.py`, `tools/font_measurer.py`, `tools/layout_solver.py`
- `frontend/` — React UI
- `eval_transcripts/` — eval inputs
- `eval_agent/` — evaluation helpers

## Env

`.env`: `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY`. Anthropic wins if both set.

## Conventions

- Prefer deterministic `pptx_editor` ops over LLM-generated code.
- Never guess coordinates — measure, spatial_map, check_overflow first.
- Keep executor happy path batch-only; LLM fallback is last resort.
- Before starting backend: clear port `kill -9 $(lsof -ti :8000) 2>/dev/null` if needed.
