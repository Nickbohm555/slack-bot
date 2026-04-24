from __future__ import annotations

import json
import sys

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _message_value(message: object, key: str, default: object = None) -> object:
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def _normalize_role(role: object) -> str:
    normalized_role = str(role or "").strip().lower()
    if normalized_role == "ai":
        return "assistant"
    if normalized_role == "human":
        return "user"
    return normalized_role or "assistant"


def _normalize_message_role(message: object) -> str:
    return _normalize_role(_message_value(message, "role") or _message_value(message, "type"))


def _normalize_message_content(message: object) -> str:
    content = _message_value(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return str(content)


def _extract_message_tool_calls(message: object) -> list[dict[str, object]]:
    raw_tool_calls = _message_value(message, "tool_calls", [])
    tool_calls: list[dict[str, object]] = []
    for tool_call in raw_tool_calls or []:
        if not isinstance(tool_call, dict):
            continue
        tool_calls.append(
            {
                "name": tool_call.get("name"),
                "args": tool_call.get("args", {}),
                "id": tool_call.get("id"),
            }
        )
    return tool_calls


def _summarize_message_for_log(message: object) -> dict[str, object]:
    summary: dict[str, object] = {
        "role": _normalize_message_role(message),
        "content": _normalize_message_content(message),
    }
    tool_calls = _extract_message_tool_calls(message)
    if tool_calls:
        summary["tool_calls"] = tool_calls
    name = _message_value(message, "name")
    if name:
        summary["name"] = name
    tool_call_id = _message_value(message, "tool_call_id")
    if tool_call_id:
        summary["tool_call_id"] = tool_call_id
    return summary


def normalize_messages(messages: list[object]) -> list[dict[str, object]]:
    return [_summarize_message_for_log(message) for message in messages]


def _pretty_log_message(message: object) -> str:
    summary = _summarize_message_for_log(message)
    role = str(summary.get("role", "assistant"))
    content = str(summary.get("content", ""))
    common_kwargs = {
        "content": content,
        "name": str(summary["name"]) if summary.get("name") else None,
    }
    if role == "user":
        return HumanMessage(**common_kwargs).pretty_repr()
    if role == "tool":
        return ToolMessage(
            **common_kwargs,
            tool_call_id=str(summary.get("tool_call_id") or ""),
        ).pretty_repr()
    return AIMessage(
        **common_kwargs,
        tool_calls=_extract_message_tool_calls(message),
    ).pretty_repr()


def log_graph_messages(messages: list[object], *, thread_id: str) -> None:
    print(
        f"========================= Conversation {thread_id} =========================",
        file=sys.stdout,
        flush=True,
    )
    for message in messages:
        print(_pretty_log_message(message), file=sys.stdout, flush=True)


def dump_messages(messages: list[dict[str, object]]) -> str:
    return json.dumps(messages, ensure_ascii=True, default=str)
