-- Migration 005: Projects as first-class entities
-- Phase 1 of the 4-week Intercom 2 plan

CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    default_owner_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- project_id UUID column for future FK
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_messages_project_id
    ON messages (project_id) WHERE project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_project_status
    ON messages (project, status, created_at DESC);

-- Seed canonical projects (path of least surprise for existing operators)
INSERT INTO projects (name, description, default_owner_agent)
VALUES
    ('hmwas', 'Hit Me With A Spoon — restaurant cost engine', 'forge'),
    ('hmwas-clean-core', 'HMWAS canonical repo on GitHub', 'forge'),
    ('intercom2', 'Intercom 2.0 coordination layer', 'codex'),
    ('groove', 'Groove Burgers operations', 'forge'),
    ('groove-social', 'Groove social pipeline (FB/IG/X)', 'forge'),
    ('birddog', 'Real estate deal-hunting project', 'forge'),
    ('vitalpbx', 'VitalPBX phone migration', 'forge'),
    ('paperclip', 'Paperclip integration', 'codex'),
    ('infra', 'Cross-project infrastructure (backups, watchdog)', 'codex')
ON CONFLICT (name) DO NOTHING;

-- Backfill project_id from project text for the small set of named projects.
-- This is a one-shot operation; the rest can be left as NULL.
UPDATE messages m
SET project_id = p.id
FROM projects p
WHERE m.project = p.name AND m.project_id IS NULL;

-- updated_at trigger
CREATE OR REPLACE FUNCTION trg_projects_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
CREATE TRIGGER trg_projects_updated_at
BEFORE UPDATE ON projects
FOR EACH ROW EXECUTE FUNCTION trg_projects_updated_at();
