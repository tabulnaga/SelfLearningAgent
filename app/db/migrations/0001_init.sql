-- 0001: app schema. Three source-of-truth areas live in separate tables (architecture.md §1):
--   AUTHORITATIVE KNOWLEDGE  -> rag_documents, rag_chunks
--   WORKING STATE            -> conversations, sessions, messages, inbound_messages, tool_executions,
--                               memory_jobs, turn_traces
--   (LEARNED EXPERIENCE lives in Hindsight, never here)
--   Simulated environment    -> sim_* (stand-in for the real payment platform; APP_ENV=sim only)

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- RAG ----
CREATE TABLE rag_documents (
    doc_id          text PRIMARY KEY,
    tenant_id       text NOT NULL,
    title           text NOT NULL,
    source          text NOT NULL,
    classification  text NOT NULL DEFAULT 'internal',
    domain          text,
    version         text,
    content_hash    text NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingested_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rag_chunks (
    chunk_id        text PRIMARY KEY,
    doc_id          text NOT NULL REFERENCES rag_documents(doc_id) ON DELETE CASCADE,
    tenant_id       text NOT NULL,
    classification  text NOT NULL,
    domain          text,
    ord             int  NOT NULL,
    heading_path    text NOT NULL DEFAULT '',
    content         text NOT NULL,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding       vector(384) NOT NULL,
    tsv             tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('english', coalesce(heading_path, '')), 'A') ||
                        setweight(to_tsvector('english', content), 'B')) STORED
);
CREATE INDEX rag_chunks_embedding_hnsw ON rag_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX rag_chunks_tsv_gin ON rag_chunks USING gin (tsv);
CREATE INDEX rag_chunks_scope ON rag_chunks (tenant_id, classification);

-- ------------------------------------------------------ working state ----
CREATE TABLE conversations (
    conversation_id text PRIMARY KEY,
    tenant_id       text NOT NULL,
    channel         text NOT NULL,
    user_id         text,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    session_id        uuid PRIMARY KEY,
    conversation_id   text NOT NULL REFERENCES conversations(conversation_id),
    tenant_id         text NOT NULL,
    user_id           text,
    status            text NOT NULL CHECK (status IN
                        ('OPEN','ACTIVE','RESOLVED','ESCALATED','TIMED_OUT','CLOSED')),
    closure_reason    text CHECK (closure_reason IN ('RESOLVED','ESCALATED','TIMED_OUT')),
    intent            text,
    domain            text,
    workflow          text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_activity_at  timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    turn_count        int NOT NULL DEFAULT 0,
    retry_count       int NOT NULL DEFAULT 0,
    escalation        jsonb,
    outcome_note      text,
    knowledge_pages   jsonb,
    pages_fetched_at  timestamptz,
    memory_written    boolean NOT NULL DEFAULT false
);
-- one live case per conversation; a message after closure opens a new session (architecture §4)
CREATE UNIQUE INDEX sessions_one_live_per_conversation
    ON sessions (conversation_id) WHERE status IN ('OPEN','ACTIVE');
CREATE INDEX sessions_idle ON sessions (last_activity_at) WHERE status = 'ACTIVE';

CREATE TABLE messages (
    message_id   bigserial PRIMARY KEY,
    session_id   uuid NOT NULL REFERENCES sessions(session_id),
    role         text NOT NULL CHECK (role IN ('user','assistant','operator','system')),
    content      text NOT NULL,
    request_id   text,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX messages_session ON messages (session_id, message_id);

-- ingress dedupe: a webhook redelivery returns the stored reply (architecture §3 step 0)
CREATE TABLE inbound_messages (
    channel              text NOT NULL,
    external_message_id  text NOT NULL,
    request_id           text NOT NULL,
    session_id           uuid,
    response             jsonb,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, external_message_id)
);

CREATE TABLE tool_executions (
    tool_exec_id  bigserial PRIMARY KEY,
    session_id    uuid NOT NULL REFERENCES sessions(session_id),
    request_id    text NOT NULL,
    tool_name     text NOT NULL,
    side_effect   text NOT NULL CHECK (side_effect IN ('read','write')),
    input         jsonb NOT NULL,
    output        jsonb,
    status        text NOT NULL CHECK (status IN ('success','error','denied')),
    error         text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    duration_ms   int
);
CREATE INDEX tool_executions_session ON tool_executions (session_id, tool_exec_id);

-- transactional outbox for the Memory Writer (architecture §6.3)
CREATE TABLE memory_jobs (
    job_id           bigserial PRIMARY KEY,
    session_id       uuid NOT NULL UNIQUE REFERENCES sessions(session_id),
    status           text NOT NULL DEFAULT 'pending' CHECK (status IN
                        ('pending','running','submitted','done','skipped','failed')),
    attempts         int NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    locked_until     timestamptz,
    reason           text,
    document_id      text,
    operation_id     text,
    content_hash     text,
    case_record      jsonb,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_jobs_due ON memory_jobs (next_attempt_at) WHERE status IN ('pending','running','submitted');

CREATE TABLE turn_traces (
    request_id       text PRIMARY KEY,
    session_id       uuid,
    conversation_id  text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    trace            jsonb NOT NULL
);
CREATE INDEX turn_traces_session ON turn_traces (session_id, created_at);

-- ------------------------------------------- simulated payment platform ----
CREATE TABLE sim_consumers (
    name        text PRIMARY KEY,
    state       text NOT NULL CHECK (state IN ('running','stalled')),
    lag         int  NOT NULL DEFAULT 0,
    restarts    int  NOT NULL DEFAULT 0,
    restart_fixes boolean NOT NULL DEFAULT true,  -- scenario knob: false = restart does not help
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sim_payments (
    payment_id  text PRIMARY KEY,
    status      text NOT NULL CHECK (status IN ('PENDING','COMPLETED','FAILED')),
    amount      numeric(12,2) NOT NULL,
    attempts    int NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
