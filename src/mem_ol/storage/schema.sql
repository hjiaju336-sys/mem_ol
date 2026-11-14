CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS threads (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ingestion_batches (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    batch_seq integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('dialogue', 'worldview')),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'failed')),
    turn_start integer,
    turn_end integer,
    char_count integer NOT NULL DEFAULT 0,
    chunk_count integer NOT NULL DEFAULT 0,
    doc_id text,
    char_start integer,
    char_end integer,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    UNIQUE (thread_id, batch_seq)
);

ALTER TABLE ingestion_batches
    ADD COLUMN IF NOT EXISTS doc_id text;
ALTER TABLE ingestion_batches
    ADD COLUMN IF NOT EXISTS char_start integer;
ALTER TABLE ingestion_batches
    ADD COLUMN IF NOT EXISTS char_end integer;

CREATE INDEX IF NOT EXISTS idx_batches_doc_range
    ON ingestion_batches (thread_id, doc_id, char_start);

CREATE TABLE IF NOT EXISTS messages (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content text NOT NULL,
    turn_no integer NOT NULL,
    occurred_at timestamptz NOT NULL,
    batch_id uuid REFERENCES ingestion_batches(id) ON DELETE SET NULL,
    checkpoint_id text,
    rolled_back boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS rolled_back boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_messages_thread_turn
    ON messages (thread_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_messages_thread_time
    ON messages (thread_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_messages_batch
    ON messages (batch_id);

CREATE TABLE IF NOT EXISTS thread_states (
    thread_id uuid PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
    user_id uuid,
    rollback_watermark timestamptz,
    version integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS game_states (
    thread_id uuid PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    version integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS state_events (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    turn_no integer NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state_after jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_state_events_thread_turn
    ON state_events (thread_id, turn_no);

CREATE TABLE IF NOT EXISTS chunks (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    batch_id uuid NOT NULL REFERENCES ingestion_batches(id) ON DELETE CASCADE,
    parent_id uuid REFERENCES chunks(id) ON DELETE CASCADE,
    level text NOT NULL CHECK (level IN ('parent', 'child')),
    chunk_no text NOT NULL,
    seq_no integer NOT NULL,
    source text NOT NULL CHECK (source IN ('worldview', 'dialogue')),
    turn_no integer,
    content text NOT NULL,
    char_start integer NOT NULL,
    char_end integer NOT NULL,
    turn_start integer,
    turn_end integer,
    message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    embedding vector(1024),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (batch_id, chunk_no)
);

-- 兼容已存在的 chunks 表（必须先补列，再建依赖该列的索引）
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS turn_no integer;

CREATE INDEX IF NOT EXISTS idx_chunks_thread_batch
    ON chunks (thread_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_chunks_thread_turn
    ON chunks (thread_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_chunks_batch_level_seq
    ON chunks (batch_id, level, seq_no);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS plot_nodes (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    level text NOT NULL CHECK (level IN ('turn', 'arc', 'global')),
    turn_no integer,
    turn_start integer,
    turn_end integer,
    summary text NOT NULL,
    embedding vector(1024),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    message_ids uuid[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_plot_turn_unique
    ON plot_nodes (thread_id, turn_no) WHERE level = 'turn';
CREATE UNIQUE INDEX IF NOT EXISTS idx_plot_arc_unique
    ON plot_nodes (thread_id, turn_start, turn_end) WHERE level = 'arc';
CREATE UNIQUE INDEX IF NOT EXISTS idx_plot_global_unique
    ON plot_nodes (thread_id) WHERE level = 'global';
CREATE INDEX IF NOT EXISTS idx_plot_arc_embedding
    ON plot_nodes USING hnsw (embedding vector_cosine_ops)
    WHERE level = 'arc' AND embedding IS NOT NULL;

CREATE TABLE IF NOT EXISTS retrieval_traces (
    id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    trace_id uuid,
    turn_no integer,
    query_text text NOT NULL,
    context_summary text,
    hypothetical_entities jsonb NOT NULL DEFAULT '[]'::jsonb,
    entity_keyword_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    entity_vector_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    entity_fused_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    context_candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    rerank_scores jsonb NOT NULL DEFAULT '{}'::jsonb,
    selected jsonb NOT NULL DEFAULT '{}'::jsonb,
    chunk_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- 兼容已经创建的旧 retrieval_traces 表
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS trace_id uuid;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS turn_no integer;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS context_summary text;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS hypothetical_entities jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS entity_keyword_candidates jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS entity_vector_candidates jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS entity_fused_candidates jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS context_candidates jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS rerank_scores jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE retrieval_traces ADD COLUMN IF NOT EXISTS selected jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_retrieval_traces_thread_time
    ON retrieval_traces (thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_retrieval_traces_thread_turn
    ON retrieval_traces (thread_id, turn_no);
CREATE INDEX IF NOT EXISTS idx_retrieval_traces_trace
    ON retrieval_traces (trace_id);

-- 派生实体索引：Neo4j 是权威，此表可从 Neo4j 重建
CREATE TABLE IF NOT EXISTS entity_index (
    entity_id uuid PRIMARY KEY,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    canonical_name text NOT NULL,
    normalized_name text NOT NULL,
    entity_type text NOT NULL
        CHECK (entity_type IN ('character', 'location', 'item', 'faction', 'event', 'concept', 'other')),
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    embedding_text text NOT NULL,
    embedding vector(1024) NOT NULL,
    index_status text NOT NULL DEFAULT 'ready'
        CHECK (index_status IN ('ready', 'pending', 'failed')),
    created_turn_no integer,
    created_batch_id uuid,
    last_turn_no integer,
    last_indexed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 兼容已存在的 entity_index 表（必须先补列，再建依赖该列的索引）
ALTER TABLE entity_index ADD COLUMN IF NOT EXISTS created_turn_no integer;
ALTER TABLE entity_index ADD COLUMN IF NOT EXISTS created_batch_id uuid;
ALTER TABLE entity_index ADD COLUMN IF NOT EXISTS last_turn_no integer;

CREATE INDEX IF NOT EXISTS idx_entity_index_thread
    ON entity_index (thread_id, entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_index_turn
    ON entity_index (thread_id, created_turn_no);
CREATE INDEX IF NOT EXISTS idx_entity_index_hnsw
    ON entity_index USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS entity_mentions (
    id uuid PRIMARY KEY,
    entity_id uuid NOT NULL,
    thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    batch_id uuid REFERENCES ingestion_batches(id) ON DELETE SET NULL,
    trace_id uuid,
    turn_no integer,
    surface_form text NOT NULL,
    message_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    chunk_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    chunk_nos jsonb NOT NULL DEFAULT '[]'::jsonb,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (trace_id, entity_id, surface_form, turn_no)
);

CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity
    ON entity_mentions (entity_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_entity_mentions_trace
    ON entity_mentions (trace_id);

-- 可选：Neo4j 已写、PG 索引未跟上时的重试队列
CREATE TABLE IF NOT EXISTS entity_index_jobs (
    job_id uuid PRIMARY KEY,
    entity_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    reason text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'done', 'failed')),
    retry_count integer NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_entity_index_jobs_status
    ON entity_index_jobs (status, created_at);

CREATE TABLE IF NOT EXISTS tool_call_logs (
    id uuid PRIMARY KEY,
    trace_id uuid,
    thread_id uuid,
    tool_name text NOT NULL,
    arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
    result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    success boolean NOT NULL,
    error text,
    duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tool_call_logs_trace
    ON tool_call_logs (trace_id, created_at);
