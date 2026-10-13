-- One Postgres server, two databases (architecture §10, spec §24 "do not duplicate infrastructure"):
--   agent     - this application (AUTHORITATIVE KNOWLEDGE + WORKING STATE)
--   hindsight - Hindsight's own schema (LEARNED EXPERIENCE); Hindsight runs its own migrations.
-- Runs once, on first start of an empty data volume. The default database/user (agent) come from
-- POSTGRES_DB / POSTGRES_USER.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE ROLE hindsight LOGIN PASSWORD 'hindsight';
CREATE DATABASE hindsight OWNER hindsight;
\connect hindsight
-- Created here as superuser so Hindsight's migrations never need elevated rights.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
GRANT ALL ON SCHEMA public TO hindsight;
