"""LangGraph pipeline: extractor → planner → executor → reviewer (with fix loop)."""
from typing import List, Optional, TypedDict

from langgraph.graph import END, StateGraph

from agents.executor import executor_node
from agents.extractor import extractor_node
from agents.planner import planner_node
from agents.reviewer import reviewer_node

MAX_FIX_ATTEMPTS = 2


class PipelineState(TypedDict):
    transcript_path: str
    template_path: str
    output_path: str
    work_dir: str
    provider: str
    planner_model: str
    executor_model: str
    extracted_facts: Optional[dict]
    edit_plan: dict
    review_verdict: str
    review_issues: List[str]
    fix_attempts: int


def _route(state: PipelineState) -> str:
    if state.get("review_verdict") == "PASS":
        return "end"
    if state.get("fix_attempts", 0) >= MAX_FIX_ATTEMPTS:
        return "end"
    return "fix"


builder = StateGraph(PipelineState)
builder.add_node("extractor", extractor_node)
builder.add_node("planner", planner_node)
builder.add_node("executor", executor_node)
builder.add_node("reviewer", reviewer_node)

builder.set_entry_point("extractor")
builder.add_edge("extractor", "planner")
builder.add_edge("planner", "executor")
builder.add_edge("executor", "reviewer")
builder.add_conditional_edges("reviewer", _route, {"fix": "executor", "end": END})

graph = builder.compile()
