from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class AgentWorkerContext:
    db_path: Path


class RuntimeAnswer(BaseModel):
    answer: str
    error: str | None = None
    runtime_mode: Literal["single_agent"] = "single_agent"
    messages: list[dict[str, object]] = Field(default_factory=list)
    estimated_tokens: int | None = None
    warning: str | None = None
    tool_call_count: int | None = None
    latency_ms: int | None = None

class UnavailableChatModel(BaseChatModel):
    provider: str
    model_name: str
    import_error: str

    @property
    def _llm_type(self) -> str:
        return "unavailable_configured_chat_model"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        raise ImportError(self.import_error)
