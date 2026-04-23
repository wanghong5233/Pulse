from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class JobScanRunRequest(BaseModel):
    keyword: str = Field(..., min_length=1, max_length=120)
    max_items: int = Field(default=10, ge=1, le=50)
    max_pages: int = Field(default=1, ge=1, le=10)
    job_type: str = Field(default="all", max_length=20)
    fetch_detail: bool = False


class JobGreetTriggerRequest(BaseModel):
    keyword: str = Field(default="AI Agent 实习", min_length=1, max_length=120)
    batch_size: int | None = Field(default=None, ge=1, le=20)
    match_threshold: float | None = Field(default=None, ge=30, le=95)
    greeting_text: str | None = Field(default=None, max_length=300)
    job_type: str = Field(default="all", max_length=20)
    run_id: str | None = Field(default=None, max_length=120)
    confirm_execute: bool = False
    fetch_detail: bool = True


class JobChatProcessRequest(BaseModel):
    max_conversations: int = Field(default=20, ge=1, le=100)
    unread_only: bool = True
    profile_id: str = Field(default="default", min_length=1, max_length=120)
    notify_on_escalate: bool = True
    fetch_latest_hr: bool = True
    auto_execute: bool = False
    chat_tab: str = Field(default="未读", max_length=30)
    confirm_execute: bool = False


class JobChatPullRequest(BaseModel):
    max_conversations: int = Field(default=20, ge=1, le=100)
    unread_only: bool = False
    fetch_latest_hr: bool = True
    chat_tab: str = Field(default="全部", max_length=30)


class JobChatExecuteRequest(BaseModel):
    conversation_id: str = Field(..., min_length=1, max_length=64)
    action: str = Field(default="reply", max_length=40)
    reply_text: str | None = Field(default=None, max_length=2000)
    profile_id: str = Field(default="default", min_length=1, max_length=120)
    run_id: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=400)
    conversation_hint: dict[str, Any] | None = None
    confirm_execute: bool = False
    card_id: str | None = Field(default=None, max_length=128)
    card_type: str | None = Field(default=None, max_length=40)


class JobChatIngestItem(BaseModel):
    hr_name: str = Field(..., min_length=1, max_length=80)
    company: str = Field(..., min_length=1, max_length=120)
    job_title: str = Field(..., min_length=1, max_length=160)
    latest_message: str = Field(..., min_length=1, max_length=2000)
    latest_time: str | None = Field(default=None, max_length=40)
    unread_count: int = Field(default=1, ge=0, le=99)
    conversation_id: str | None = Field(default=None, max_length=64)


class JobChatIngestRequest(BaseModel):
    items: list[JobChatIngestItem] = Field(default_factory=list, max_length=200)
    source: str = Field(default="manual", max_length=40)


class JobConversationHint(BaseModel):
    conversation_id: str
    hr_name: str
    company: str
    job_title: str
    latest_message: str
    latest_time: str
    unread_count: int = 0


class JobGreetCandidate(BaseModel):
    job_id: str
    title: str
    company: str
    source_url: str
    snippet: str = ""
    match_score: float = 0.0
    source: str = ""
    collected_at: datetime | str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
