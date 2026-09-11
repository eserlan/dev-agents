"""A minimal graph proving repository context can flow through a workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from dev_agents.config import ProjectConfig, load_projects_config, select_project
from dev_agents.context.instructions import InstructionsContext, discover_instructions
from dev_agents.context.repository import RepositoryContext, load_repository_context


class InspectionState(TypedDict, total=False):
    config_path: str
    project_name: str
    commit_range: str | None
    project: ProjectConfig
    repository: RepositoryContext
    instructions: InstructionsContext
    summary: dict[str, object]


def load_configuration(state: InspectionState) -> InspectionState:
    config = load_projects_config(Path(state["config_path"]))
    return {"project": select_project(config, state["project_name"])}


def load_repository(state: InspectionState) -> InspectionState:
    return {
        "repository": load_repository_context(
            state["project"].repo, commit_range=state.get("commit_range")
        )
    }


def load_instructions(state: InspectionState) -> InspectionState:
    return {"instructions": discover_instructions(state["repository"].repository_path)}


def produce_summary(state: InspectionState) -> InspectionState:
    repository = state["repository"].model_dump(mode="json")
    repository["is_clean"] = state["repository"].is_clean
    return {
        "summary": {
            "project": state["project_name"],
            "repository": repository,
            "instructions": state["instructions"].model_dump(mode="json"),
        }
    }


def build_inspection_workflow() -> Any:
    """Build the intentionally small, explicit inspection graph."""
    graph = StateGraph(InspectionState)
    graph.add_node("load_configuration", load_configuration)
    graph.add_node("load_repository", load_repository)
    graph.add_node("load_instructions", load_instructions)
    graph.add_node("produce_summary", produce_summary)
    graph.add_edge(START, "load_configuration")
    graph.add_edge("load_configuration", "load_repository")
    graph.add_edge("load_repository", "load_instructions")
    graph.add_edge("load_instructions", "produce_summary")
    graph.add_edge("produce_summary", END)
    return graph.compile()


def inspect_project(
    config_path: Path, project_name: str, commit_range: str | None = None
) -> dict[str, object]:
    """Run the inspection graph and return its structured summary."""
    result = build_inspection_workflow().invoke(
        {
            "config_path": str(config_path),
            "project_name": project_name,
            "commit_range": commit_range,
        }
    )
    return cast(dict[str, object], result["summary"])
