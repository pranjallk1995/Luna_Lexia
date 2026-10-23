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
