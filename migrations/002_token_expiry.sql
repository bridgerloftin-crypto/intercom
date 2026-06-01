-- Intercom 2.0 token expiry
-- Adds expires_at to agent_tokens so per-agent tokens can rotate.
-- Tokens are valid until expires_at (when set). NULL means "no expiry"
-- (bootstrap token and any pre-existing tokens).

ALTER TABLE agent_tokens
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_agent_tokens_expires_at
    ON agent_tokens (expires_at)
    WHERE status = 'active';

-- Mark existing tokens as "no expiry" (NULL) by default.
-- Operators can rotate by running create_agent_token.py --rotate which
-- deactivates old tokens and creates a new one with expires_at = now() + 90 days.
