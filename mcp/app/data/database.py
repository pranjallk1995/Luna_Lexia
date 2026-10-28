"""PostgreSQL connection handling and idempotent schema management."""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS document_metadata (
    name text PRIMARY KEY,
    document_id uuid NOT NULL DEFAULT gen_random_uuid(),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    content_sha256 char(64) NOT NULL,
    file_format text NOT NULL,
    page_count integer NOT NULL CHECK (page_count >= 1),
    doc_type text NOT NULL,
    title text NOT NULL,
    summary text NOT NULL,
    tags text[] NOT NULL DEFAULT '{}',
    confidence real NOT NULL DEFAULT 0,
    extracted jsonb NOT NULL DEFAULT '{}'::jsonb,
    extraction_error text,
    review_flags text[] NOT NULL DEFAULT '{}',
    financial_year text,
    document_date date,
    issuer text,
    primary_amount double precision,
    page_texts text[] NOT NULL DEFAULT '{}',
    parser text NOT NULL DEFAULT 'docling',
    analysis_model text NOT NULL,
    schema_version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    search_vector tsvector NOT NULL DEFAULT ''::tsvector,
    hidden boolean NOT NULL DEFAULT false,
    favorite boolean NOT NULL DEFAULT false,
    public_url text
);
CREATE UNIQUE INDEX IF NOT EXISTS document_metadata_document_id_idx
    ON document_metadata (document_id);
CREATE INDEX IF NOT EXISTS document_metadata_search_idx
    ON document_metadata USING gin (search_vector);
CREATE INDEX IF NOT EXISTS document_metadata_tags_idx
    ON document_metadata USING gin (tags);
CREATE INDEX IF NOT EXISTS document_metadata_sha_idx
    ON document_metadata (content_sha256);
CREATE INDEX IF NOT EXISTS document_metadata_hidden_idx
    ON document_metadata (hidden, updated_at DESC);
CREATE INDEX IF NOT EXISTS document_metadata_favorite_idx
    ON document_metadata (favorite, hidden, updated_at DESC);
CREATE INDEX IF NOT EXISTS document_metadata_type_year_idx
    ON document_metadata (hidden, doc_type, financial_year);

-- The weighted full-text vector over the filename, analysis, tags, and parsed page text.
CREATE OR REPLACE FUNCTION document_search_vector() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', regexp_replace(NEW.name, '[._-]+', ' ', 'g')), 'A') ||
        setweight(to_tsvector('english', NEW.title), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.issuer, '')), 'A') ||
        setweight(to_tsvector('english', replace(NEW.doc_type, '_', ' ')), 'B') ||
        setweight(to_tsvector('english', NEW.summary), 'B') ||
        setweight(to_tsvector('english', array_to_string(NEW.tags, ' ')), 'B') ||
        setweight(to_tsvector('english', left(array_to_string(NEW.page_texts, ' '), 100000)), 'D');
    RETURN NEW;
END
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER document_metadata_search_vector
    BEFORE INSERT OR UPDATE OF name, title, issuer, doc_type, summary, tags, page_texts
    ON document_metadata
    FOR EACH ROW EXECUTE FUNCTION document_search_vector();

-- Google Drive import: the connected account, background jobs, and each file's outcome.
CREATE TABLE IF NOT EXISTS drive_connection (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    email text NOT NULL,
    encrypted_refresh_token text NOT NULL,
    connected_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS import_jobs (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL,
    source text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    total integer NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz
);
CREATE TABLE IF NOT EXISTS import_items (
    item_id bigserial PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES import_jobs (job_id) ON DELETE CASCADE,
    source text NOT NULL,
    source_id text,
    content_md5 text,
    name text NOT NULL,
    path text,
    outcome text NOT NULL,
    document_name text,
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS import_items_job_idx ON import_items (job_id, item_id);
CREATE INDEX IF NOT EXISTS import_items_source_idx ON import_items (source, source_id, outcome);
-- What a job was asked to import, kept until it finishes so a restart can resume it;
-- and each file's place in its job, so a resumed job never records a file twice.
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS request jsonb;
ALTER TABLE import_items ADD COLUMN IF NOT EXISTS position integer;
CREATE UNIQUE INDEX IF NOT EXISTS import_items_position_idx ON import_items (job_id, position);
-- The file a running job is on, and since when, to show its progress.
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS current_item text;
ALTER TABLE import_jobs ADD COLUMN IF NOT EXISTS current_started_at timestamptz;
-- LangGraph's checkpoint tables, from when imports ran as a checkpointed graph.
DROP TABLE IF EXISTS checkpoint_writes, checkpoint_blobs, checkpoints, checkpoint_migrations;

CREATE TABLE IF NOT EXISTS vault_config (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    public_base_url text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


class Database:
    """Open short-lived transactional connections that return dict rows."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        """Yield one connection; its transaction commits when the block succeeds."""
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            yield connection

    def initialize_schema(self) -> None:
        """Create or upgrade the idempotent Luna Lexia database schema."""
        with psycopg.connect(self.database_url, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)

    def initialize_semantic_schema(self, dimensions: int) -> None:
        """Create the chunk table for semantic search, sized for the embedding model.

        Embeddings are derived data: when the model's dimensions change, the table is
        rebuilt empty and the background indexer embeds every document again.
        """
        with psycopg.connect(self.database_url, autocommit=True) as connection:
            installed = connection.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
            if installed is None:
                raise RuntimeError(
                    "The pgvector extension is missing; run Luna Sentia's postgres-init to install it."
                )
            current = connection.execute(
                """SELECT atttypmod FROM pg_attribute
                   WHERE attrelid = to_regclass('document_chunks') AND attname = 'embedding'"""
            ).fetchone()
            if current is not None and current[0] != dimensions:
                connection.execute("DROP TABLE document_chunks")
            connection.execute(
                f"""
CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id bigserial PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES document_metadata (document_id) ON DELETE CASCADE,
    chunk_index integer NOT NULL,
    page integer NOT NULL,
    content text NOT NULL,
    source_md5 char(32) NOT NULL,
    embedding_model text NOT NULL,
    embedding vector({int(dimensions)}) NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    UNIQUE (document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS document_chunks_embedding_idx
    ON document_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS document_chunks_search_idx ON document_chunks USING gin (search_vector);
"""
            )
