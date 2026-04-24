from __future__ import annotations

import logging
from pathlib import Path

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from agents.logging import log_graph_messages, normalize_messages
from agents.middleware import DeepAgentGuardrailsMiddleware
from agents.prompt import SINGLE_AGENT_SYSTEM_PROMPT
from agents.schemas import AgentWorkerContext, RuntimeAnswer, UnavailableChatModel
from config import Settings, get_settings
from tools.database import execute_sql_tool, inspect_columns_tool, list_tables_tool


logger = logging.getLogger("agents.builder")


def build_agent_model(settings: Settings | None = None) -> BaseChatModel:
    runtime_settings = settings or get_settings()
    try:
        return init_chat_model(
            runtime_settings.single_agent.model,
            model_provider=runtime_settings.single_agent.provider,
        )
    except ImportError as exc:
        return UnavailableChatModel(
            provider=runtime_settings.single_agent.provider,
            model_name=runtime_settings.single_agent.model,
            import_error=str(exc),
        )


def _build_agent(
    *,
    checkpointer: object | None = None,
    settings: Settings | None = None,
):
    model = build_agent_model(settings)
    tools = (list_tables_tool, inspect_columns_tool, execute_sql_tool)
    return create_deep_agent(
        model=model,
        tools=list(tools),
        system_prompt=SINGLE_AGENT_SYSTEM_PROMPT,
        middleware=[DeepAgentGuardrailsMiddleware()],
        skills=[],
        context_schema=AgentWorkerContext,
        checkpointer=checkpointer,
        name="database_qa_agent",
    )


def _extract_final_assistant_answer(messages: list[dict[str, object]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return str(message.get("content", ""))
    return ""


def normalize_agent_result(result: dict[str, object]) -> RuntimeAnswer:
    messages = normalize_messages(list(result.get("messages", [])))
    return RuntimeAnswer(
        answer=_extract_final_assistant_answer(messages),
        messages=messages,
    )


def invoke_agent_runtime(
    *,
    text: str,
    thread_id: str,
    slack_user_id: str,
    conversation_key: str,
    sqlite_db_path: Path,
    checkpointer: object | None = None,
    settings: Settings | None = None,
) -> RuntimeAnswer:
    runtime = _build_agent(
        checkpointer=checkpointer,
        settings=settings,
    )

    logger.info(
        "agent_runtime conversation_start thread_id=%s slack_user_id=%s conversation_key=%s input=%s",
        thread_id,
        slack_user_id,
        conversation_key,
        text,
    )
    try:
        result = runtime.invoke(
            {
                "messages": [{"role": "user", "content": text}],
                "files": {},
            },
            {"configurable": {"thread_id": thread_id}},
            context=AgentWorkerContext(db_path=sqlite_db_path),
        )
        log_graph_messages(list(result.get("messages", [])), thread_id=thread_id)
        return normalize_agent_result(result)
    except Exception:
        logger.exception(
            "agent_runtime conversation_failed thread_id=%s slack_user_id=%s conversation_key=%s",
            thread_id,
            slack_user_id,
            conversation_key,
        )
        return RuntimeAnswer(answer="", error="conversation_runtime_failed")
