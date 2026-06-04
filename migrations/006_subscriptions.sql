-- Migration 006: Thread subscriptions (Phase 4 polish)
CREATE TABLE IF NOT EXISTS thread_subscriptions (
    thread_id UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    agent TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (thread_id, agent)
);

CREATE INDEX IF NOT EXISTS idx_thread_subscriptions_agent
    ON thread_subscriptions(agent);
