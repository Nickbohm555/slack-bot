from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from time import perf_counter
from uuid import uuid4

from agents import invoke_agent_runtime
from sqlalchemy import Column, DateTime, MetaData, Table, Text, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

from api_service.schemas import (
    SlackConversation,
    SlackInboundMessage,
    SlackServiceResponse,
    SlackSessionRecord,
)
from database import RuntimeDependencies


SESSION_TABLE_NAME = "slack_conversation_sessions"
logger = logging.getLogger("api_service.slack_service")
metadata = MetaData()
slack_conversation_sessions = Table(
    SESSION_TABLE_NAME,
    metadata,
    Column("conversation_key", Text, primary_key=True),
    Column("slack_user_id", Text, nullable=False),
    Column("active_thread_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


def should_ignore_message(event: Mapping[str, object]) -> bool:
    return bool(event.get("subtype")) or "bot_id" in event


def build_inbound_message(event: Mapping[str, object], *, source: str) -> SlackInboundMessage:
    user_id = event.get("user")
    channel_id = event.get("channel")
    message_ts = event.get("ts")
    if not user_id or not channel_id or not message_ts:
        raise ValueError("missing_slack_message_fields")

    thread_ts = event.get("thread_ts")
    return SlackInboundMessage(
        text=str(event.get("text", "")),
        source=source,
        slack_user_id=str(user_id),
        slack_channel_id=str(channel_id),
        slack_message_ts=str(message_ts),
        slack_thread_ts=str(thread_ts) if thread_ts else None,
    )


def resolve_conversation(message: SlackInboundMessage) -> SlackConversation:
    reply_thread_ts = (
        message.slack_channel_id
        if message.source == "dm"
        else (message.slack_thread_ts or message.slack_message_ts)
    )
    conversation_key = (
        f"dm:{message.slack_channel_id}"
        if message.source == "dm"
        else f"channel:{message.slack_channel_id}:{reply_thread_ts}"
    )
    return SlackConversation(
        source=message.source,
        slack_user_id=message.slack_user_id,
        slack_channel_id=message.slack_channel_id,
        reply_thread_ts=reply_thread_ts,
        conversation_key=conversation_key,
    )


def ensure_slack_session_table(engine: Engine) -> None:
    metadata.create_all(engine, tables=[slack_conversation_sessions], checkfirst=True)


def get_or_create_session(
    engine: Engine,
    conversation: SlackConversation,
    *,
    thread_id_prefix: str,
) -> SlackSessionRecord:
    ensure_slack_session_table(engine)
    with engine.begin() as connection:
        row = connection.execute(
            select(
                slack_conversation_sessions.c.conversation_key,
                slack_conversation_sessions.c.slack_user_id,
                slack_conversation_sessions.c.active_thread_id,
            ).where(slack_conversation_sessions.c.conversation_key == conversation.conversation_key)
        ).mappings().first()
        if row is not None:
            return SlackSessionRecord(
                conversation_key=str(row["conversation_key"]),
                slack_user_id=str(row["slack_user_id"]),
                active_thread_id=str(row["active_thread_id"]),
            )

        active_thread_id = f"{thread_id_prefix}-{uuid4()}"
        connection.execute(
            slack_conversation_sessions.insert().values(
                conversation_key=conversation.conversation_key,
                slack_user_id=conversation.slack_user_id,
                active_thread_id=active_thread_id,
            )
        )
        return SlackSessionRecord(
            conversation_key=conversation.conversation_key,
            slack_user_id=conversation.slack_user_id,
            active_thread_id=active_thread_id,
        )


def rotate_session_thread(
    engine: Engine,
    conversation: SlackConversation,
    *,
    thread_id_prefix: str,
) -> SlackSessionRecord:
    ensure_slack_session_table(engine)
    active_thread_id = f"{thread_id_prefix}-{uuid4()}"
    statement = insert(slack_conversation_sessions).values(
        conversation_key=conversation.conversation_key,
        slack_user_id=conversation.slack_user_id,
        active_thread_id=active_thread_id,
    )
    with engine.begin() as connection:
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[slack_conversation_sessions.c.conversation_key],
                set_={
                    "slack_user_id": statement.excluded.slack_user_id,
                    "active_thread_id": statement.excluded.active_thread_id,
                    "updated_at": func.now(),
                },
            )
        )
    return SlackSessionRecord(
        conversation_key=conversation.conversation_key,
        slack_user_id=conversation.slack_user_id,
        active_thread_id=active_thread_id,
    )


def estimate_context_tokens(messages: Sequence[object]) -> int:
    return max(1, len(json.dumps(list(messages), default=str, ensure_ascii=True)) // 4)


def build_context_warning(estimated_tokens: int, *, warning_threshold: int) -> str | None:
    if estimated_tokens >= warning_threshold:
        return "This conversation is getting long. Consider starting a new Slack thread soon."
    return None


def handle_slack_message(
    message: SlackInboundMessage,
    *,
    settings,
    dependencies: RuntimeDependencies,
) -> SlackServiceResponse:
    conversation = resolve_conversation(message)
    normalized_text = message.text.strip()
    logger.info(
        "slack_message_received source=%s conversation_key=%s slack_user_id=%s text=%r",
        conversation.source,
        conversation.conversation_key,
        conversation.slack_user_id,
        normalized_text,
    )

    if normalized_text.lower() in {"new", "/new"}:
        rotate_session_thread(
            dependencies.postgres_engine,
            conversation,
            thread_id_prefix=settings.memory.new_session_prefix,
        )
        return SlackServiceResponse(
            text="Started a new session for this conversation.",
            reply_thread_ts=None if conversation.source == "dm" else conversation.reply_thread_ts,
        )

    if not normalized_text:
        return SlackServiceResponse(
            text="Send a message and I will pass it to the application.",
            reply_thread_ts=None if conversation.source == "dm" else conversation.reply_thread_ts,
        )

    session_record = get_or_create_session(
        dependencies.postgres_engine,
        conversation,
        thread_id_prefix=settings.memory.new_session_prefix,
    )
    started_at = perf_counter()
    answer = invoke_agent_runtime(
        text=normalized_text,
        thread_id=session_record.active_thread_id,
        slack_user_id=conversation.slack_user_id,
        conversation_key=conversation.conversation_key,
        sqlite_db_path=dependencies.sqlite_db_path,
        checkpointer=dependencies.checkpointer,
        settings=dependencies.settings,
    )
    messages = list(answer.messages or [{"role": "assistant", "content": answer.answer}])
    estimated_tokens = estimate_context_tokens(messages)
    answer = answer.model_copy(
        update={
            "messages": messages,
            "estimated_tokens": estimated_tokens,
            "warning": build_context_warning(
                estimated_tokens,
                warning_threshold=dependencies.warning_threshold_tokens,
            ),
            "latency_ms": int((perf_counter() - started_at) * 1000),
        }
    )

    response_text = answer.answer
    if not response_text and answer.error:
        response_text = "I hit a runtime error while answering that request."
    if answer.warning:
        response_text = f"{response_text}\n\n{answer.warning}" if response_text else answer.warning

    logger.info(
        "slack_message_completed source=%s conversation_key=%s slack_user_id=%s thread_id=%s latency_ms=%s error=%s",
        conversation.source,
        conversation.conversation_key,
        conversation.slack_user_id,
        session_record.active_thread_id,
        answer.latency_ms,
        answer.error,
    )
    return SlackServiceResponse(
        text=response_text,
        reply_thread_ts=None if conversation.source == "dm" else conversation.reply_thread_ts,
    )
