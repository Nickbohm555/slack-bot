from __future__ import annotations

from typing import Callable

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse


def _tool_name(tool: object) -> str | None:
    if isinstance(tool, dict):
        name = tool.get("name")
        return str(name) if name else None
    name = getattr(tool, "name", None)
    return str(name) if name else None


class DeepAgentGuardrailsMiddleware(AgentMiddleware):
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        filtered_tools = [
            tool
            for tool in (request.tools or [])
            if _tool_name(tool) != "task"
        ]
        model_settings = dict(request.model_settings or {})
        model_settings["parallel_tool_calls"] = False
        return handler(request.override(tools=filtered_tools, model_settings=model_settings))
