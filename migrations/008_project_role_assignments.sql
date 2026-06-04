-- Migration 008: Project role assignments (coding, reviewing)
--
-- The /projects/new form captures coding and reviewing agents but the
-- values were persisted to state/project_assignments.json rather than
-- Postgres. That split-brain (DB + sidecar JSON) was a maintenance
-- trap. Bring it home.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS coding_agent TEXT,
    ADD COLUMN IF NOT EXISTS reviewing_agent TEXT;
