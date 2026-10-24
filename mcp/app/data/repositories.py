"""PostgreSQL persistence for Luna Lexia, one repository per aggregate."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from app.core.models import DocumentRecord
from app.core.schemas import KeyFacts
from app.data.database import Database

Row = dict[str, Any]

PUBLIC_DOCUMENT_PATH = "/api/public/documents/"


def public_url_sql(base_url_parameter: str) -> str:
    """Build a stable public URL from the base and immutable document ID (null without a base)."""
    return f"NULLIF({base_url_parameter}, '') || '{PUBLIC_DOCUMENT_PATH}' || document_id::text"


class Repository:
    """Shared query helpers that serialize rows into JSON-friendly dicts."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def serialize(row: Row | None) -> Row | None:
        if row is None:
            return None
        return {
            key: (
                value.isoformat()
                if hasattr(value, "isoformat")
                else str(value) if isinstance(value, UUID) else value
            )
            for key, value in row.items()
        }

    def fetch_one(self, sql: str, params: Any = ()) -> Row | None:
        with self.database.connect() as connection:
            return self.serialize(connection.execute(sql, params).fetchone())

    def fetch_all(self, sql: str, params: Any = ()) -> list[Row]:
        with self.database.connect() as connection:
            return [self.serialize(row) for row in connection.execute(sql, params).fetchall()]


class DocumentRepository(Repository):
    """Document metadata, extracted details, visibility, favorites, and search.

    ``page_texts`` (the parsed Markdown of every page) is large, so listings
    leave it out; ``pages`` returns it for one document.
    """

    COLUMNS = (
        "name, document_id, doc_type, title, summary, tags, financial_year, document_date, "
        "issuer, primary_amount, confidence, extracted, extraction_error, review_flags, "
        "file_format, page_count, parser, size_bytes, content_sha256, analysis_model, schema_version, "
        "created_at, updated_at, hidden, favorite, public_url"
    )

    @staticmethod
    def _filters(doc_types: list[str] | None, financial_year: str | None) -> tuple[list[str], list[Any]]:
        clauses, params = [], []
        if doc_types:
            clauses.append("doc_type = ANY(%s)")
            params.append(doc_types)
        if financial_year:
            clauses.append("financial_year = %s")
            params.append(financial_year)
        return clauses, params

    def upsert(self, record: DocumentRecord, public_base_url: str) -> Row:
        """Insert or replace metadata and assign its stable public URL."""
        values = {**asdict(record), "extracted": Jsonb(record.extracted)}
        columns = list(values)
        updates = ", ".join(f"{column} = EXCLUDED.{column}" for column in columns if column != "name")
        with self.database.connect() as connection:
            connection.execute(
                f"""INSERT INTO document_metadata ({', '.join(columns)})
                    VALUES ({', '.join(f'%({column})s' for column in columns)})
                    ON CONFLICT (name) DO UPDATE SET {updates}, updated_at = now()""",
                values,
            )
            row = connection.execute(
                f"""UPDATE document_metadata SET public_url = {public_url_sql('%s')}
                    WHERE name = %s RETURNING {self.COLUMNS}""",
                (public_base_url, record.name),
            ).fetchone()
        return self.serialize(row)

    def get(self, name: str, hidden: bool | None = False) -> Row | None:
        """Return one record, restricted to a collection unless ``hidden`` is None."""
        return self.fetch_one(
            f"""SELECT {self.COLUMNS} FROM document_metadata
                WHERE name = %s AND (%s::boolean IS NULL OR hidden = %s)""",
            (name, hidden, hidden),
        )

    def pages(self, name: str, hidden: bool = False) -> list[str] | None:
        """Return the parsed text of each page of one document in a collection."""
        row = self.fetch_one(
            "SELECT page_texts FROM document_metadata WHERE name = %s AND hidden = %s",
            (name, hidden),
        )
        return row["page_texts"] if row else None

    def list(
        self,
        hidden: bool | None = False,
        doc_types: list[str] | None = None,
        financial_year: str | None = None,
    ) -> list[Row]:
        """List visible, hidden, or all records newest first, optionally filtered."""
        clauses, params = self._filters(doc_types, financial_year)
        clauses.insert(0, "(%s::boolean IS NULL OR hidden = %s)")
        return self.fetch_all(
            f"""SELECT {self.COLUMNS} FROM document_metadata
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, name""",
            (hidden, hidden, *params),
        )

    def list_favorites(self) -> list[Row]:
        """List visible favorite documents newest first."""
        return self.fetch_all(
            f"""SELECT {self.COLUMNS} FROM document_metadata
                WHERE favorite = true AND hidden = false
                ORDER BY updated_at DESC, name"""
        )

    def is_hidden(self, name: str) -> bool:
        """Return whether the named document belongs to the Hidden Vault."""
        row = self.fetch_one("SELECT hidden FROM document_metadata WHERE name = %s", (name,))
        if row is None:
            raise FileNotFoundError(f"Document {name!r} was not found.")
        return bool(row["hidden"])

    def find_visible_by_content(self, content_sha256: str) -> Row | None:
        """Return the newest visible document with identical bytes, if any."""
        return self.fetch_one(
            f"""SELECT {self.COLUMNS} FROM document_metadata
                WHERE content_sha256 = %s AND hidden = false
                ORDER BY updated_at DESC, name LIMIT 1""",
            (content_sha256,),
        )

    def name_for_document_id(self, document_id: str) -> str | None:
        """Resolve an immutable public identifier to its current filename."""
        row = self.fetch_one(
            "SELECT name FROM document_metadata WHERE document_id::text = %s", (document_id,)
        )
        return row["name"] if row else None

    def delete(self, name: str) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM document_metadata WHERE name = %s", (name,))

    def rename(self, name: str, new_name: str) -> Row:
        """Rename one visible record while preserving all other fields."""
        try:
            row = self.fetch_one(
                f"""UPDATE document_metadata SET name = %s, updated_at = now()
                    WHERE name = %s AND hidden = false
                    RETURNING {self.COLUMNS}""",
                (new_name, name),
            )
        except psycopg.errors.UniqueViolation as error:
            raise FileExistsError(f"Document {new_name!r} already exists.") from error
        if row is None:
            raise FileNotFoundError(f"Visible document {name!r} was not found.")
        return row

    def update_metadata(
        self,
        name: str,
        title: str,
        summary: str,
        tags: list[str],
        extracted: dict[str, Any],
        extraction_error: str | None,
        review_flags: list[str],
        facts: KeyFacts,
        hidden: bool = False,
    ) -> Row:
        """Update editable metadata and the key facts derived from extracted details."""
        row = self.fetch_one(
            f"""UPDATE document_metadata
                SET title = %s, summary = %s, tags = %s, extracted = %s,
                    extraction_error = %s, review_flags = %s, financial_year = %s,
                    document_date = %s, issuer = %s, primary_amount = %s, updated_at = now()
                WHERE name = %s AND hidden = %s
                RETURNING {self.COLUMNS}""",
            (
                title,
                summary,
                tags,
                Jsonb(extracted),
                extraction_error,
                review_flags,
                facts.financial_year,
                facts.document_date,
                facts.issuer,
                facts.amount,
                name,
                hidden,
            ),
        )
        if row is None:
            raise FileNotFoundError(f"Document {name!r} was not found.")
        return row

    def set_hidden(self, names: list[str], hidden: bool) -> list[str]:
        """Persist hidden state and return the names that actually changed."""
        if not names:
            return []
        rows = self.fetch_all(
            """UPDATE document_metadata SET hidden = %s, updated_at = now()
               WHERE name = ANY(%s) AND hidden <> %s RETURNING name""",
            (hidden, names, hidden),
        )
        return [row["name"] for row in rows]

    def set_favorite(self, names: list[str], favorite: bool) -> list[str]:
        """Set favorite state for visible documents and return the changed names."""
        if not names:
            return []
        rows = self.fetch_all(
            """UPDATE document_metadata SET favorite = %s, updated_at = now()
               WHERE name = ANY(%s) AND hidden = false AND favorite <> %s
               RETURNING name""",
            (favorite, names, favorite),
        )
        return [row["name"] for row in rows]

    def search(
        self,
        normalized_query: str,
        tags: list[str],
        match_all_tags: bool,
        doc_types: list[str] | None,
        financial_year: str | None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[Row]]:
        """Query persisted full-text, tag, type, and year indexes without opening files."""
        clauses = ["hidden = false"]
        filters: list[Any] = []
        if normalized_query:
            clauses.append("search_vector @@ websearch_to_tsquery('english', %s)")
            filters.append(normalized_query)
        if tags:
            clauses.append(f"tags {'@>' if match_all_tags else '&&'} %s")
            filters.append(tags)
        type_clauses, type_params = self._filters(doc_types, financial_year)
        clauses.extend(type_clauses)
        filters.extend(type_params)
        rank_sql = (
            "ts_rank_cd(search_vector, websearch_to_tsquery('english', %s))"
            if normalized_query
            else "0"
        )
        params = ([normalized_query] if normalized_query else []) + filters + [limit, offset]
        rows = self.fetch_all(
            f"""SELECT {self.COLUMNS}, count(*) OVER() AS total_count, {rank_sql} AS rank
                FROM document_metadata WHERE {' AND '.join(clauses)}
                ORDER BY rank DESC, updated_at DESC, name LIMIT %s OFFSET %s""",
            params,
        )
        count = rows[0]["total_count"] if rows else 0
        return count, [
            {key: value for key, value in row.items() if key not in {"total_count", "rank"}}
            for row in rows
        ]


class PublicURLRepository(Repository):
    """The mutable public origin and every document URL derived from it."""

    SELECT_BASE_URL = "SELECT public_base_url FROM vault_config WHERE singleton = true"

    def get(self) -> str:
        """Return the currently persisted public HTTPS origin."""
        row = self.fetch_one(self.SELECT_BASE_URL)
        return row["public_base_url"] if row else ""

    def seed(self, default_url: str) -> str:
        """Store ``default_url`` only when unset, then refresh stored document URLs."""
        with self.database.connect() as connection:
            if default_url:
                connection.execute(
                    """INSERT INTO vault_config (singleton, public_base_url)
                       VALUES (true, %s) ON CONFLICT (singleton) DO NOTHING""",
                    (default_url,),
                )
            row = connection.execute(self.SELECT_BASE_URL).fetchone()
            public_base_url = row["public_base_url"] if row else ""
            if public_base_url:
                self._regenerate(connection, public_base_url)
        return public_base_url

    def replace(self, public_base_url: str) -> int:
        """Atomically store the origin and regenerate every document URL."""
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO vault_config (singleton, public_base_url)
                   VALUES (true, %s)
                   ON CONFLICT (singleton) DO UPDATE SET
                       public_base_url = EXCLUDED.public_base_url,
                       updated_at = now()""",
                (public_base_url,),
            )
            return self._regenerate(connection, public_base_url)

    @staticmethod
    def _regenerate(connection: psycopg.Connection, public_base_url: str) -> int:
        cursor = connection.execute(
            f"UPDATE document_metadata SET public_url = {public_url_sql('%s')}",
            (public_base_url,),
        )
        return cursor.rowcount


class ImportRepository(Repository):
    """The Drive connection, import jobs, and the outcome of every imported file."""

    IMPORTED_OUTCOMES = ("imported", "duplicate")

    def drive_connection(self) -> Row | None:
        return self.fetch_one("SELECT email, encrypted_refresh_token, connected_at FROM drive_connection")

    def save_drive_connection(self, email: str, encrypted_refresh_token: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO drive_connection (singleton, email, encrypted_refresh_token)
                   VALUES (true, %s, %s)
                   ON CONFLICT (singleton) DO UPDATE SET email = EXCLUDED.email,
                       encrypted_refresh_token = EXCLUDED.encrypted_refresh_token, connected_at = now()""",
                (email, encrypted_refresh_token),
            )

    def delete_drive_connection(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM drive_connection")

    def create_job(self, kind: str, source: str, request: dict[str, Any]) -> Row:
        return self.fetch_one(
            "INSERT INTO import_jobs (kind, source, request) VALUES (%s, %s, %s) RETURNING job_id",
            (kind, source, Jsonb(request)),
        )

    def job_request(self, job_id: str) -> dict[str, Any] | None:
        row = self.fetch_one("SELECT request FROM import_jobs WHERE job_id::text = %s", (job_id,))
        return row["request"] if row else None

    def unfinished_jobs(self) -> list[str]:
        """Jobs a restart cut off, oldest first."""
        rows = self.fetch_all(
            """SELECT job_id FROM import_jobs WHERE status IN ('queued', 'listing', 'running')
               ORDER BY created_at""",
        )
        return [str(row["job_id"]) for row in rows]

    def item_recorded(self, job_id: str, position: int) -> bool:
        return self.fetch_one(
            "SELECT 1 AS found FROM import_items WHERE job_id::text = %s AND position = %s", (job_id, position)
        ) is not None

    def update_job(self, job_id: str, **fields: Any) -> None:
        """Set job columns; ``started`` stamps the first start and ``finished`` the end."""
        assignments, params = [], []
        for name, value in fields.items():
            if name == "started":
                assignments.append("started_at = coalesce(started_at, now())")
            elif name == "finished":
                assignments.append("finished_at = now()")
            else:
                assignments.append(f"{name} = %s")
                params.append(Jsonb(value) if isinstance(value, dict) else value)
        with self.database.connect() as connection:
            connection.execute(
                f"UPDATE import_jobs SET {', '.join(assignments)} WHERE job_id = %s", (*params, job_id)
            )

    # Each job with how many files it has processed and a count per outcome.
    _JOB_SELECT = """SELECT j.job_id, j.kind, j.source, j.status, j.total, j.error,
            j.created_at, j.started_at, j.finished_at,
            (SELECT count(*) FROM import_items i WHERE i.job_id = j.job_id) AS processed,
            coalesce((SELECT jsonb_object_agg(outcome, n) FROM (
                SELECT outcome, count(*) AS n FROM import_items i
                WHERE i.job_id = j.job_id GROUP BY outcome) counts), '{}'::jsonb) AS outcomes
        FROM import_jobs j"""

    def job(self, job_id: str) -> Row | None:
        return self.fetch_one(f"{self._JOB_SELECT} WHERE j.job_id::text = %s", (job_id,))

    def jobs(self, limit: int) -> list[Row]:
        return self.fetch_all(f"{self._JOB_SELECT} ORDER BY j.created_at DESC LIMIT %s", (limit,))

    def items(self, job_id: str) -> list[Row]:
        return self.fetch_all(
            """SELECT source, source_id, name, path, outcome, document_name, error, created_at
               FROM import_items WHERE job_id::text = %s ORDER BY item_id""",
            (job_id,),
        )

    def record_item(self, job_id: str, item: dict[str, Any]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO import_items (job_id, source, source_id, content_md5, name, path,
                       outcome, document_name, error, position)
                   VALUES (%(job_id)s, %(source)s, %(source_id)s, %(content_md5)s, %(name)s, %(path)s,
                       %(outcome)s, %(document_name)s, %(error)s, %(position)s)
                   ON CONFLICT (job_id, position) DO NOTHING""",
                {
                    "job_id": job_id,
                    **{key: item.get(key) for key in (
                        "source", "source_id", "content_md5", "name", "path", "outcome", "document_name", "error",
                        "position",
                    )},
                },
            )

    def already_imported(self, source: str, source_id: str, content_md5: str | None) -> Row | None:
        """The earlier import of this exact file version, if any."""
        return self.fetch_one(
            """SELECT name, document_name, created_at FROM import_items
               WHERE source = %s AND source_id = %s AND outcome = ANY(%s)
                 AND (%s::text IS NULL OR content_md5 IS NOT DISTINCT FROM %s)
               ORDER BY item_id DESC LIMIT 1""",
            (source, source_id, list(self.IMPORTED_OUTCOMES), content_md5, content_md5),
        )
