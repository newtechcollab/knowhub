# knowledge/repository.py

from models.knowledge import KnowledgeChunk


class KnowledgeRepository:

    def __init__(self, collection, chroma_client,):
        self.collection = collection
        self.chroma_client = chroma_client


    # ============================================================
    # RESET Repository
    # ============================================================
    def reset(self) -> int:
        """
        Delete every chunk from the knowledge collection.
        Then, delete the collection itself and create a new collection
        Returns the number of chunks that existed before reset.
        """

        collection_name = self.collection.name

        existing = self.collection.get(
            include=[]
        )

        ids = existing.get("ids", []) or []

        count = len(ids)

        if ids:
            self.collection.delete(
                ids=ids
            )

        print(f"Deleting Chroma collection: {collection_name}")

        self.chroma_client.delete_collection(name=collection_name)

        print(f"Recreating Chroma collection: {collection_name}")

        self.collection = (
            self.chroma_client.get_or_create_collection(
                name=collection_name
            )
        )

        return count

    # ============================================================
    # INDEXING
    # ============================================================

    def add_chunks(
        self,
        chunks: list[KnowledgeChunk],
        embeddings: list[list[float]],
    ) -> int:
        if not chunks:
            return 0

        if len(chunks) != len(embeddings):
            raise ValueError(
                "Number of chunks does not match number of embeddings."
            )

        self.collection.add(
            ids=[
                chunk.chunk_id
                for chunk in chunks
            ],
            documents=[
                chunk.text
                for chunk in chunks
            ],
            embeddings=embeddings,
            metadatas=[
                self._to_metadata(chunk)
                for chunk in chunks
            ],
        )

        return len(chunks)

    # ============================================================
    # DELETE RECORD
    # ============================================================

    def delete_record(
        self,
        record_id: str,
    ):
        if not record_id:
            return

        self.collection.delete(
            where={
                "record_id": record_id
            }
        )

    # ============================================================
    # DELETE SOURCE
    # ============================================================

    def delete_source(
        self,
        source_id: str,
    ):
        if not source_id:
            return

        self.collection.delete(
            where={
                "source_id": source_id
            }
        )

    # ============================================================
    # Complete Metadata for the given Record Id
    # ============================================================

    def get_metadata_for_record(
        self,
        record_id: str,
    ) -> dict:
        """
        Return persisted Chroma metadata for a record.

        A record can have multiple chunks, but all chunks for the
        same record carry the same record-level metadata.
        """

        if not record_id:
            return {}

        result = self.collection.get(
            where={
                "record_id": record_id
            },
            include=[
                "metadatas",
            ],
        )

        metadatas = (
            result.get(
                "metadatas",
                [],
            )
            or []
        )

        if not metadatas:
            return {}

        metadata = (
            metadatas[0]
            or {}
        )

        return dict(
            metadata
        )


    # ============================================================
    # Entity Type for the given Record Id
    # ============================================================

    def get_entity_type_for_record(
        self,
        record_id: str,
    ) -> str | None:
        """
        Return the canonical entity_type stored in Chroma
        for the supplied record_id.
        """

        metadata = (
            self.get_metadata_for_record(
                record_id
            )
        )

        return metadata.get(
            "entity_type"
        )

    # ============================================================
    # VECTOR SEARCH FOR EXACT RECORD ID
    # ============================================================

    def search_vector_for_record(
        self,
        embedding: list[float],
        record_id: str,
        top_k: int = 3,
    ) -> dict:
        """
        Search only the chunks belonging to a specific record.
        """

        if not record_id:
            raise ValueError(
                "record_id is required."
            )

        where = {
            "record_id": record_id,
        }

        return self.search_vector(
            embedding=embedding,
            top_k=top_k,
            where=where,
        )

    # ============================================================
    # VECTOR SEARCH
    # ============================================================

    def search_vector(
        self,
        embedding: list[float],
        top_k: int = 8,
        where: dict | None = None,
    ) -> dict:
        """
        Perform semantic/vector search against Chroma.
        """

        query_args = {
            "query_embeddings": [
                embedding
            ],
            "n_results": top_k,
            "include": [
                "documents",
                "metadatas",
                "distances",
            ],
        }

        if where:
            query_args["where"] = where

        return self.collection.query(
            **query_args
        )

    # ============================================================
    # EXACT SERVICENOW RECORD SEARCH
    # ============================================================

    def search_exact_record(
        self,
        source_type: str,
        servicenow_table: str,
        servicenow_number: str,
        top_k: int = 20,
    ) -> list[dict]:
        """
        Perform an exact metadata lookup.

        This does NOT use embeddings.

        Example:
            INC0010002
            PRB0000050
            CHG0001234
        """

        result = self.collection.get(
            where={
                "$and": [
                    {
                        "source_type":
                            source_type
                    },
                    {
                        "servicenow_table":
                            servicenow_table
                    },
                    {
                        "servicenow_number":
                            servicenow_number
                    },
                ]
            },
            include=[
                "documents",
                "metadatas",
            ],
        )

        documents = (
            result.get(
                "documents",
                [],
            )
            or []
        )

        metadatas = (
            result.get(
                "metadatas",
                [],
            )
            or []
        )

        matches = []

        for doc, meta in zip(
            documents,
            metadatas,
        ):
            meta = meta or {}

            matches.append(
                {
                    "text": doc,

                    "chunk_id": meta.get(
                        "chunk_id",
                        "",
                    ),

                    "record_id": meta.get(
                        "record_id",
                        "",
                    ),

                    "source_id": meta.get(
                        "source_id",
                        "",
                    ),

                    "source_name": meta.get(
                        "source_name",
                        "",
                    ),

                    "source_type": meta.get(
                        "source_type",
                        "",
                    ),

                    "entity_type": meta.get(
                        "entity_type",
                        "",
                    ),

                    "file_path": meta.get(
                        "file_path",
                        meta.get(
                            "locator",
                            "",
                        ),
                    ),

                    "page": meta.get(
                        "page",
                        1,
                    ),

                    "chunk_index": meta.get(
                        "chunk_index",
                        0,
                    ),

                    "chunk_hash": meta.get(
                        "chunk_hash",
                        "",
                    ),

                    "servicenow_table": meta.get(
                        "servicenow_table",
                        "",
                    ),

                    "servicenow_sys_id": meta.get(
                        "servicenow_sys_id",
                        "",
                    ),

                    "servicenow_number": meta.get(
                        "servicenow_number",
                        "",
                    ),

                    "updated_at": meta.get(
                        "updated_at",
                        "",
                    ),

                    # Exact searches don't have vector distance.
                    "distance": None,
                }
            )

            if len(matches) >= top_k:
                break

        return matches

    # ============================================================
    # GET RECORD CHUNKS
    # ============================================================

    def get_record_chunks(
        self,
        record_id: str,
    ) -> dict:
        return self.collection.get(
            where={
                "record_id": record_id
            },
            include=[
                "documents",
                "metadatas",
            ],
        )

    # ============================================================
    # METADATA
    # ============================================================

    @staticmethod
    def _to_metadata(
        chunk: KnowledgeChunk,
    ) -> dict:
        """
        Convert KnowledgeChunk into Chroma-compatible metadata.

        Chroma metadata values should be scalar values rather
        than None.
        """

        return {
            "chunk_id":
                chunk.chunk_id,

            "record_id":
                chunk.record_id,

            "source_id":
                chunk.source_id,

            "source_type":
                chunk.source_type,

            "entity_type":
                chunk.entity_type,

            "source_name":
                chunk.source_name or "",

            "chunk_index":
                chunk.chunk_index,

            "chunk_hash":
                chunk.chunk_hash,

            "locator":
                chunk.locator or "",

            "page":
                chunk.page or 0,

            "updated_at": (
                chunk.updated_at.isoformat()
                if chunk.updated_at
                else ""
            ),

            "servicenow_table":
                chunk.servicenow_table or "",

            "servicenow_sys_id":
                chunk.servicenow_sys_id or "",

            "servicenow_number":
                chunk.servicenow_number or "",
        }
