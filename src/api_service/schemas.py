from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackInboundMessage:
    text: str
    source: str
    slack_user_id: str
    slack_channel_id: str
    slack_message_ts: str
    slack_thread_ts: str | None = None


@dataclass(frozen=True)
class SlackServiceResponse:
    text: str
    reply_thread_ts: str | None = None


@dataclass(frozen=True)
class SlackConversation:
    source: str
    slack_user_id: str
    slack_channel_id: str
    reply_thread_ts: str
    conversation_key: str


@dataclass(frozen=True)
class SlackSessionRecord:
    conversation_key: str
    slack_user_id: str
    active_thread_id: str
