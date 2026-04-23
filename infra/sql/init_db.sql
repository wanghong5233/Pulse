-- Pulse database schema
-- This file is used by setup-pg.sh and by runtime _ensure_schema methods.

-- ===== Recruitment domain tables =====

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT,
    jd_raw TEXT NOT NULL,
    jd_parsed JSONB,
    match_score REAL,
    gap_analysis TEXT,
    status TEXT DEFAULT 'new',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    resume_version TEXT,
    cover_letter TEXT,
    applied_at TIMESTAMP,
    channel TEXT,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    action_type TEXT NOT NULL,
    input_summary TEXT,
    output_summary TEXT,
    screenshot_path TEXT,
    status TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS boss_chat_events (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    hr_name TEXT,
    company TEXT,
    job_title TEXT,
    latest_hr_message TEXT NOT NULL,
    latest_hr_time TEXT,
    message_signature TEXT NOT NULL UNIQUE,
    intent TEXT,
    confidence REAL,
    action TEXT,
    reason TEXT,
    reply_text TEXT,
    needs_send_resume BOOLEAN DEFAULT FALSE,
    needs_user_intervention BOOLEAN DEFAULT FALSE,
    notification_sent BOOLEAN DEFAULT FALSE,
    notification_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_events (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    email_type TEXT NOT NULL,
    company TEXT,
    interview_time TEXT,
    raw_classification JSONB,
    related_job_id TEXT,
    updated_job_status TEXT,
    received_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    signature TEXT NOT NULL UNIQUE,
    source_email_id TEXT,
    company TEXT,
    event_type TEXT NOT NULL,
    start_at TIMESTAMP NOT NULL,
    raw_time_text TEXT,
    mode TEXT,
    location TEXT,
    contact TEXT,
    confidence REAL,
    status TEXT DEFAULT 'scheduled',
    reminder_sent_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ===== Infrastructure tables =====

CREATE TABLE IF NOT EXISTS security_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    purpose TEXT,
    status TEXT NOT NULL,
    issued_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    consumed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tool_budgets (
    session_id TEXT NOT NULL,
    tool_type TEXT NOT NULL,
    used_count INTEGER NOT NULL,
    limit_count INTEGER NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    PRIMARY KEY (session_id, tool_type)
);

-- ===== Recall Memory (conversations + tool_calls) =====

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    session_id TEXT,
    task_id TEXT,
    run_id TEXT,
    workspace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_session_created_at ON conversations(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_created_at ON conversations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_task_id ON conversations(task_id);
CREATE INDEX IF NOT EXISTS idx_conversations_run_id ON conversations(run_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id),
    session_id TEXT,
    task_id TEXT,
    run_id TEXT,
    workspace_id TEXT,
    tool_name TEXT NOT NULL,
    tool_args JSONB NOT NULL DEFAULT '{}'::jsonb,
    tool_result JSONB,
    status TEXT NOT NULL,
    latency_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session_created_at ON tool_calls(session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task_id ON tool_calls(task_id);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    module_name TEXT NOT NULL,
    trigger_source TEXT,
    input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    output_json JSONB,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_session_started_at ON pipeline_runs(session_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_module_name ON pipeline_runs(module_name);

-- ===== Corrections / DPO =====

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    user_text TEXT NOT NULL,
    assistant_text TEXT,
    correction_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_corrections_session_created_at ON corrections(session_id, created_at DESC);

-- ===== Archival Memory (facts) =====

CREATE TABLE IF NOT EXISTS facts (
    id BIGSERIAL PRIMARY KEY,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    "object" TEXT NOT NULL,
    object_json JSONB,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    source TEXT,
    superseded_by BIGINT REFERENCES facts(id),
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    promoted_from TEXT,
    promotion_reason TEXT,
    task_id TEXT,
    run_id TEXT,
    workspace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_facts_subject_predicate ON facts(subject, predicate);
CREATE INDEX IF NOT EXISTS idx_facts_created_at ON facts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_facts_valid_from ON facts(valid_from DESC);
CREATE INDEX IF NOT EXISTS idx_facts_task_id ON facts(task_id);

-- ===== Workspace Memory =====

CREATE TABLE IF NOT EXISTS workspace_summaries (
    id BIGSERIAL PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    token_estimate INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_workspace_summaries_workspace_id ON workspace_summaries(workspace_id);

CREATE TABLE IF NOT EXISTS workspace_facts (
    id BIGSERIAL PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_workspace_facts_workspace_key ON workspace_facts(workspace_id, key);
CREATE UNIQUE INDEX IF NOT EXISTS ux_workspace_facts_workspace_key ON workspace_facts(workspace_id, key);

-- ===== Intel Knowledge Base =====

CREATE TABLE IF NOT EXISTS intel_documents (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'unknown',
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_intel_docs_category ON intel_documents(category);
CREATE INDEX IF NOT EXISTS idx_intel_docs_collected ON intel_documents(collected_at);
