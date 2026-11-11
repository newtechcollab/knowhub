from __future__ import annotations

import os
import sqlite3
from typing import Optional


class LexicalRepository:

    def __init__(
        self,
        db_path: str,
    ):
        self.db_path = db_path

        os.makedirs(
            os.path.dirname(self.db_path) or ".",
            exist_ok=True,
        )

        self._initialize()

    # ==========================================================
    # Connection
    # ==========================================================

    def _connect(self):
        connection = sqlite3.connect(
            self.db_path,
            timeout=30,
        )

        connection.row_factory = (
            sqlite3.Row
        )

        return connection

    # ==========================================================
    # Initialize FTS index
    # ==========================================================

    def _initialize(self):

        connection = self._connect()

        try:

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lexical_chunks (
                    rowid INTEGER PRIMARY KEY,
                    chunk_id TEXT UNIQUE,
                    record_id TEXT,
                    source_type TEXT,
                    source_name TEXT,
                    title TEXT,
                    text TEXT
                )
                """
            )

            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS lexical_fts
                USING fts5(
                    chunk_id UNINDEXED,
                    record_id UNINDEXED,
                    source_type UNINDEXED,
                    source_name,
                    title,
                    text,
                    tokenize = 'unicode61'
                )
                """
            )

            connection.commit()

        finally:
            connection.close()

    # ==========================================================
    # Add chunks
    # ==========================================================

    def add_chunks(self, chunks: list) -> int:
        """
        Add or update multiple KnowledgeChunk objects
        in the lexical FTS5 index.
      
        Returns the number of chunks indexed.
        """

        if not chunks:
            return 0

        connection = self._connect()

        indexed_count = 0

        try:

            for chunk in chunks:

                if chunk is None:
                    continue

                chunk_id = (
                    getattr(
                        chunk,
                        "chunk_id",
                        "",
                    )
                    or ""
                ).strip()

                record_id = (
                    getattr(
                        chunk,
                        "record_id",
                        "",
                    )
                    or ""
                ).strip()

                source_type = (
                    getattr(
                        chunk,
                        "source_type",
                        "",
                    )
                    or ""
                ).strip()

                source_name = (
                    getattr(
                        chunk,
                        "source_name",
                        "",
                    )
                    or ""
                ).strip()

                text = (
                    getattr(
                        chunk,
                        "text",
                        "",
                    )
                    or ""
                ).strip()

                if not chunk_id:
                    raise ValueError(
                        "KnowledgeChunk.chunk_id is required"
                    )

                if not record_id:
                    raise ValueError(
                        "KnowledgeChunk.record_id is required"
                    )

                if not text:
                    continue

                title = (
                    getattr(
                        chunk,
                        "title",
                        "",
                    )
                    or ""
                ).strip()

                existing = connection.execute(
                    """
                    SELECT rowid
                    FROM lexical_chunks
                    WHERE chunk_id = ?
                    """,
                    (chunk_id,),
                ).fetchone()

                if existing:

                    rowid = existing["rowid"]

                    connection.execute(
                        """
                        DELETE FROM lexical_fts
                        WHERE rowid = ?
                        """,
                        (rowid,),
                    )

                    connection.execute(
                        """
                        UPDATE lexical_chunks
                        SET
                            record_id = ?,
                            source_type = ?,
                            source_name = ?,
                            title = ?,
                            text = ?
                        WHERE chunk_id = ?
                        """,
                        (
                            record_id,
                            source_type,
                            source_name,
                            title,
                            text,
                            chunk_id,
                        ),
                    )

                else:
  
                    cursor = connection.execute(
                        """
                        INSERT INTO lexical_chunks (
                            chunk_id,
                            record_id,
                            source_type,
                            source_name,
                            title,
                            text
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk_id,
                            record_id,
                            source_type,
                            source_name,
                            title,
                            text,
                        ),
                    )

                    rowid = cursor.lastrowid

                # --------------------------------------------------
                # Add to FTS5
                # --------------------------------------------------
                connection.execute(
                    """
                    INSERT INTO lexical_fts (
                        rowid,
                        chunk_id,
                        record_id,
                        source_type,
                        source_name,
                        title,
                        text
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rowid,
                        chunk_id,
                        record_id,
                        source_type,
                        source_name,
                        title,
                        text,
                    ),

                )

                indexed_count += 1

            connection.commit()

            return indexed_count

        except Exception:
            connection.rollback()
            raise

        finally:
            connection.close()

    # ==========================================================
    # Delete record
    # ==========================================================

    def delete_record(
        self,
        record_id: str,
    ) -> bool:

        connection = self._connect()

        try:

            row = connection.execute(
                """
                SELECT rowid
                FROM lexical_records
                WHERE record_id = ?
                """,
                (record_id,),
            ).fetchone()

            if not row:
                return False

            rowid = row["rowid"]

            connection.execute(
                """
                DELETE FROM lexical_fts
                WHERE rowid = ?
                """,
                (rowid,),
            )

            connection.execute(
                """
                DELETE FROM lexical_records
                WHERE record_id = ?
                """,
                (record_id,),
            )

            connection.commit()

            return True

        finally:
            connection.close()

    # ==========================================================
    # Search
    # ==========================================================

    def search_keyword(
        self,
        query: str,
        top_k: int = 20,
        source_type: Optional[str] = None,
    ) -> list[dict]:

        query = (
            query
            or ""
        ).strip()

        if not query:
            return []

        # ------------------------------------------------------
        # Convert user query into a safe FTS query.
        #
        # We use individual quoted terms so punctuation in
        # things such as TS-123 doesn't break the FTS parser.
        # ------------------------------------------------------

        terms = []

        for token in query.split():

            token = token.strip()

            if not token:
                continue

            token = token.replace(
                '"',
                '""',
            )

            terms.append(
                f'"{token}"'
            )

        if not terms:
            return []

        fts_query = " AND ".join(
            terms
        )

        connection = self._connect()

        try:

            sql = """
                SELECT
                    lexical_fts.rowid AS rowid,
                    lexical_fts.chunk_id AS chunk_id,
                    lexical_fts.record_id AS record_id,
                    lexical_fts.source_type AS source_type,
                    lexical_fts.source_name AS source_name,
                    lexical_fts.title AS title,
                    lexical_fts.text AS text,
                    bm25(
                        lexical_fts,
                        1.0,
                        8.0,
                        1.0,
                        1.0
                    ) AS bm25_score
                FROM lexical_fts
                WHERE lexical_fts MATCH ?
            """

            params = [fts_query]

            if source_type:

                sql += """
                    AND lexical_fts.source_type = ?
                """

                params.append(
                    source_type
                )

            sql += """
                ORDER BY bm25_score ASC
                LIMIT ?
            """

            params.append(
                int(top_k)
            )

            rows = connection.execute(
                sql,
                params,
            ).fetchall()

            results = []

            for index, row in enumerate(rows):

                results.append(
                    {
                        "chunk_id": row["chunk_id"],
                        "record_id": row["record_id"],
                        "source_type": row["source_type"],
                        "source_name": row["source_name"],
                        "title": row["title"],
                        "text": row["text"],
                        "bm25_score": row["bm25_score"],
                        "keyword_rank": index + 1,
                    }
                )

            return results

        finally:
            connection.close()

    # ==========================================================
    # Count
    # ==========================================================

    def count(self) -> int:

        connection = self._connect()

        try:

            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM lexical_records
                """
            ).fetchone()

            return int(
                row["count"]
            )

        finally:
            connection.close()

    # ==========================================================
    # Reset
    # ==========================================================

    def reset(self) -> int:

        connection = self._connect()

        try:

            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM lexical_records
                """
            ).fetchone()

            count = int(
                row["count"]
            )

            connection.execute(
                "DELETE FROM lexical_fts"
            )

            connection.execute(
                "DELETE FROM lexical_records"
            )

            connection.commit()

            return count

        finally:
            connection.close()
