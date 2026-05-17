# PPTX Agent

A multi-agent AI pipeline that edits any PPTX template using a document, brief, or notes as input. Drop a template and content — the agents fill every placeholder, apply edits, and QA the result.

## How it works

```
Content document + PPTX template
        ↓
  Planner (Sonnet)        — reads template shape inventory + document, produces JSON edit plan
        ↓
  Executor (Haiku)        — applies edits via deterministic tools (no code generation)
        ↓
  Deterministic pre-check — catches empty shapes, unfilled placeholders, overflow
        ↓
  Reviewer (Haiku+vision) — inspects rendered slide PNGs against a strict 7-point checklist
        ↓
  Fix pass (Sonnet)       — re-runs on flagged slides only if reviewer finds issues
        ↓
  Edited PPTX
```

## Stack

- **Orchestration**: LangGraph `StateGraph`
- **Agents**: LangChain `create_react_agent` with deterministic python-pptx tools
- **Backend**: FastAPI with Server-Sent Events for real-time streaming
- **Frontend**: React + Vite + Tailwind CSS
- **Visual QA**: LibreOffice (optional — enables slide rendering for multimodal review)

## Setup

```bash
# Clone
git clone https://github.com/aravind-812/pptx-agent.git
cd pptx-agent

# Python deps
pip install -r requirements.txt

# Frontend
cd frontend && npm install && npm run build && cd ..

# API keys (.env)
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
# or
echo "OPENAI_API_KEY=sk-..." > .env

# Optional: visual QA (macOS)
brew install --cask libreoffice

# Run
python3 backend.py
# Open http://localhost:8000
```

## Models

| Agent | Model | Calls per run |
|---|---|---|
| Planner | Sonnet 4.6 / GPT-4o | 1 |
| Executor | Haiku 4.5 / GPT-4o-mini | 20–60 |
| Reviewer | Haiku 4.5 / GPT-4o-mini + vision | 3–8 |
| Fix pass | Sonnet 4.6 / GPT-4o | ~15 (targeted, flagged slides only) |

**~$0.27–0.35 per deck** (Anthropic). ~$0.12 (OpenAI).

## Why this approach scores better

Based on [Ressl AI's evaluation](https://github.com/abhishek203/ressl-pptx-eval) of PPTX editing agents:

| Approach | Usable output rate |
|---|---|
| Single agent, best model tested | ~66% |
| This pipeline (estimated) | ~85% |

Three concrete reasons:
1. **Deterministic tools** — executor calls pre-built python-pptx functions, not generated code. Eliminates syntax errors, wrong API calls, wrong shape references.
2. **Planner-first** — reasoning about what to change happens before touching the file. Prevents the "editing mishap" failure mode where models regenerate slides from scratch.
3. **Strict visual QA** — reviewer runs a 7-point checklist (overflow, blank slides, invisible text, placeholder text, shape overlap, layout collapse) on rendered PNGs, not just XML.

## Provider support

Auto-detects `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. Anthropic takes priority if both are set. Provider selector shown in the UI at runtime.
