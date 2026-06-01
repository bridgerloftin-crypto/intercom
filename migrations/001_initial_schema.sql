-- Intercom 2.0 initial schema
-- Safe to re-run.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    display_name TEXT,
    role TEXT,
    endpoint TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id UUID REFERENCES agents(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS threads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project TEXT,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT NOT NULL DEFAULT 'normal',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY,
    thread_id UUID REFERENCES threads(id) ON DELETE SET NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    project TEXT,
    message_type TEXT NOT NULL DEFAULT 'msg',
    priority TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'unread',
    subject TEXT,
    body TEXT,
    expected_action TEXT,
    blocking_reason TEXT,
    deadline_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ref_id BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_messages_inbox
    ON messages (to_agent, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_project
    ON messages (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages (thread_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_messages_metadata
    ON messages USING GIN (metadata);

CREATE TABLE IF NOT EXISTS message_receipts (
    id BIGSERIAL PRIMARY KEY,
    message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    receipt_type TEXT NOT NULL,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (message_id, agent, receipt_type)
);

CREATE TABLE IF NOT EXISTS handoffs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id UUID REFERENCES threads(id) ON DELETE SET NULL,
    message_id BIGINT REFERENCES messages(id) ON DELETE SET NULL,
    project TEXT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    expected_output TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    priority TEXT NOT NULL DEFAULT 'normal',
    due_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accepted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_handoffs_to_status
    ON handoffs (to_agent, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_handoffs_project
    ON handoffs (project, status, created_at DESC);

CREATE TABLE IF NOT EXISTS presence_events (
    id BIGSERIAL PRIMARY KEY,
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_presence_agent_created
    ON presence_events (agent, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    actor TEXT,
    subject TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_created
    ON audit_events (created_at DESC);

CREATE OR REPLACE FUNCTION intercom2_touch_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_agents_updated_at'
    ) THEN
        CREATE TRIGGER trg_agents_updated_at
        BEFORE UPDATE ON agents
        FOR EACH ROW EXECUTE FUNCTION intercom2_touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_threads_updated_at'
    ) THEN
        CREATE TRIGGER trg_threads_updated_at
        BEFORE UPDATE ON threads
        FOR EACH ROW EXECUTE FUNCTION intercom2_touch_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_handoffs_updated_at'
    ) THEN
        CREATE TRIGGER trg_handoffs_updated_at
        BEFORE UPDATE ON handoffs
        FOR EACH ROW EXECUTE FUNCTION intercom2_touch_updated_at();
    END IF;
END $$;
