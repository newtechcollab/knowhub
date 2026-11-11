from __future__ import annotations
import os
import re,json
from typing import Optional
BASE_DIR = "./data"
DATA_DIR = "./data"

from typing import Callable, Optional

from models.knowledge import (
    KnowledgeRecord,
    KnowledgeChunk,
)

from knowledge.chunks import (
    split_into_chunks,
    text_hash,
)


class KnowledgeService:
    """
    Application-level knowledge service.

    Responsibilities:
      - Convert a KnowledgeRecord into chunks
      - Delete old chunks before re-indexing a record
      - Generate embeddings
      - Persist chunks through KnowledgeRepository
      - Perform semantic searches
      - Perform exact ServiceNow record searches

    KnowledgeService does NOT access Chroma directly.
    That responsibility belongs to KnowledgeRepository.
    """

    def __init__(
        self,
        repository,
        embed_fn: Callable[[list[str]], list[list[float]]],
        relationship_service=None,
        record_identity_service=None,
        lexical_repository=None,
        openai_client=None,
        model=None,
        datasanitizer=None,
    ):
        self.repository = repository
        self.embed_fn = embed_fn
        self.relationship_service = relationship_service
        self.record_identity_service = record_identity_service
        self.lexical_repository = lexical_repository
        self.openai_client = openai_client
        self.model = model
        self.datasanitizer = datasanitizer

    # ==============================================================
    # Reset Repository
    # ==============================================================
    def reset(self) -> dict:
        """
        Remove all indexed knowledge.
        """

        chunks_deleted = self.repository.reset()

        relationships_deleted = 0

        if self.relationship_service:
            try:
                relationships_deleted = (
                    self.relationship_service.reset()
                )
            except Exception as exc:
                print(
                    "Relationship reset failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        if self.record_identity_service:
            try:
                self.record_identity_service.reset()
            except Exception as exc:
                print(
                    "Record identity reset failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        # ------------------------------------------------------
        # Delete connector state files
        # ------------------------------------------------------

        state_files = [
            # ServiceNow
            os.path.join(
                BASE_DIR,
                "servicenow_source_ids.json",
            ),
            os.path.join(
                BASE_DIR,
                "servicenow_sync_state.json",
            ),

            # Salesforce
            os.path.join(
                BASE_DIR,
                "salesforce_sources.json",
            ),
            os.path.join(
                BASE_DIR,
                "salesforce_sync_state.json",
            ),


            # Confluence
            os.path.join(
                BASE_DIR,
                "confluence_sources.json",
            ),
            os.path.join(
                BASE_DIR,
                "confluence_sync_state.json",
            ),

            # JIRA
            os.path.join(
                BASE_DIR,
                "jira_source_ids.json",
            ),
            os.path.join(
                BASE_DIR,
                "jira_sync_state.json",
            ),
            os.path.join(
                BASE_DIR,
                "pending_relationships.json",
            ),
        ]

        deleted_files = []

        for file_path in state_files:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)

                    deleted_files.append(
                        os.path.basename(file_path)
                    )

                    print(
                        "Deleted knowledge state file:",
                        file_path,
                    )

            except OSError as exc:
                print(
                    f"Failed to delete state file "
                    f"{file_path}: {exc}"
                )

        return {
            "chunks_deleted": chunks_deleted,
        }

    # Enriches relationship data with proper metadata
    def enrich_relationships(self, relationships: list[dict],) -> list[dict]:
    
        enriched = []

        for relation in relationships:
            source_id = relation.get(
                "source_record_id"
            )
            target_id = relation.get(
                "target_record_id"
            )

            source = (
                self.record_identity_service
                .get_record_metadata(
                    source_id
                )
                if self.record_identity_service
                else None
            )

            target = (
                self.record_identity_service
                .get_record_metadata(
                    target_id
                )
                if self.record_identity_service
                else None
            )

            enriched.append({
                **relation,
                "source": source,
                "target": target,
            })

        return enriched

    # Get relationships for a given search result
    def get_relationships_for_results(self, results: list[dict],) -> list[dict]:
    
        if not self.relationship_service:
            return []

        relationships = []
        seen = set()

        for result in results:
            record_id = result.get(
                "record_id"
            )

            if not record_id:
                continue

            related = (
                self.relationship_service.get_related(
                    record_id
                )
            )

            for relation in related:
                key = (
                    relation.get(
                        "source_record_id"
                    ),
                    relation.get(
                        "target_record_id"
                    ),
                    relation.get(
                        "relation_type"
                    ),
                )

                if key in seen:
                    continue

                seen.add(key)
                relationships.append(
                    relation
                )

        return relationships

    # ============================================================
    # RELATED RECORDS
    # ============================================================

    def get_related_records(
        self,
        record_id: str,
    ) -> list[dict]:
        """
        Return relationships involving record_id.

        This currently returns relationship objects.
        Resolution into complete KnowledgeRecords can be added
        once the RecordIdentityService/record repository supports it.
        """

        if not self.relationship_service:
            return []

        return (
            self.relationship_service.get_related(
                record_id
            )
        )

    @staticmethod
    def _should_register_identity(
        record: KnowledgeRecord,
    ) -> bool:
        return record.source_type in {
            "servicenow",
            "jira",
            "github",
            "confluence",
            "sharepoint",
            "salesforce",
        }

    # ==============================================================
    # Indexing
    # ==============================================================

    def index_record(
        self,
        record: KnowledgeRecord,
        source_name: str,
        index_relationships: bool = True,
    ) -> dict:
        """
        Index one logical knowledge record.

        A record may produce multiple chunks.

        Re-indexing the same record is idempotent because all
        existing chunks for the record are deleted first.
        """

        content = (record.content or "").strip()

        if not content:
            return {
                "record_id": record.record_id,
                "chunks_indexed": 0,
                "status": "skipped",
                "reason": "Empty content",
            }

        # Sanitize the KnowledgeRecord, for data security purposes
        if self.datasanitizer:

            record, security_result = (self.datasanitizer.sanitize_record(record))

            print(f"************** Sanitized Record is -> {record} *************** \n\n")


        # ----------------------------------------------------------
        # Split record into chunks
        # ----------------------------------------------------------

        print(f"*************** Content being chunked ************** -> {record.content} *********** \n\n")
        chunks = split_into_chunks(record.content)

        if not chunks:
            return {
                "record_id": record.record_id,
                "chunks_indexed": 0,
                "status": "skipped",
                "reason": "No chunks generated",
            }

        # ----------------------------------------------------------
        # Remove previous version
        # ----------------------------------------------------------

        self.repository.delete_record(
            record.record_id
        )

        # --------------------------------------------------------
        # Remove old relationships for this record
        #
        # Do this before creating new relationships so updates
        # don't leave stale relationships behind.
        # --------------------------------------------------------

        if self.relationship_service:
            try:
                self.relationship_service.delete_for_record(
                    record.record_id
                )
            except Exception as exc:
                print(
                    "Relationship cleanup failed for "
                    f"{record.record_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        # ----------------------------------------------------------
        # Create KnowledgeChunk objects
        # ----------------------------------------------------------

        knowledge_chunks: list[KnowledgeChunk] = []

        for index, chunk_text in enumerate(chunks):
            knowledge_chunks.append(
                KnowledgeChunk(
                    chunk_id=self._create_chunk_id(
                        record.record_id,
                        index,
                        chunk_text,
                    ),

                    record_id=record.record_id,
                    source_id=record.source_id,
                    source_type=record.source_type,
                    entity_type=record.entity_type,
                    source_name=source_name,
                    title=record.title,
                    chunk_index=index,
                    text=chunk_text,
                    chunk_hash=text_hash(
                        chunk_text
                    ),
                    locator=record.locator,
                    page=None,
                    updated_at=record.updated_at,
                    servicenow_table=None,
                    servicenow_sys_id=None,
                    servicenow_number=None,
                )
            )

        # ----------------------------------------------------------
        # Preserve ServiceNow metadata
        #
        # These fields may already be provided on the record
        # through its locator/external_id or can be attached by
        # connector-specific code.
        # ----------------------------------------------------------

        self._populate_servicenow_metadata(
            record,
            knowledge_chunks,
        )

        # ----------------------------------------------------------
        # Generate embeddings
        # ----------------------------------------------------------

        documents = [
            chunk.text
            for chunk in knowledge_chunks
        ]

        embeddings = self.embed_fn(
            documents
        )

        if embeddings is None:
            raise RuntimeError("Embedding failure: Check Vector Embedding Server for details \n\n")
  
        if len(embeddings) != len(knowledge_chunks):
            raise RuntimeError("Embedding count does not match " "chunk count.")

        # ----------------------------------------------------------
        # Persist chunks in Vector DB
        # ----------------------------------------------------------

        indexed = self.repository.add_chunks(
            chunks=knowledge_chunks,
            embeddings=embeddings,
        )

        # ----------------------------------------------------------
        # Persist the chunks in Lexical DB also
        # ----------------------------------------------------------
        if self.lexical_repository:
            self.lexical_repository.add_chunks(
            chunks=knowledge_chunks,
        )

        # --------------------------------------------------------
        # Register external identity
        #
        # Example:
        #   servicenow:incident:INC0012345
        #       ->
        #   servicenow:incident:<sys_id>
        #
        # Or:
        #   github:repo/path/file.py
        #       ->
        #   github:<internal-record-id>
        # --------------------------------------------------------
        if (self.record_identity_service and self._should_register_identity(record)):
            try:    
                self.record_identity_service.register(record=record)
            except Exception as exc:
                print(
                    "Record identity registration failed for "
                    f"{record.record_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

        # --------------------------------------------------------
        # Extract/store relationships
        # --------------------------------------------------------

        if self.relationship_service and index_relationships:
            try:
                self._index_relationships(
                    record=record
                )
            except Exception as exc:
                print(
                    "Relationship indexing failed for "
                    f"{record.record_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                raise

        # Resolve pending relationship
        pending_relationships_resolved = 0

        if self.relationship_service:
            try:
                pending_relationships_resolved = (
                    self.relationship_service
                    .resolve_pending_for_record(
                        record
                    )
                ) or 0

                if pending_relationships_resolved:
                    print(
                        f"Resolved "
                        f"{pending_relationships_resolved} "
                        f"pending relationships for "
                        f"{record.record_id}"
                    )

            except Exception as exc:
                print(
                    f"Pending relationship resolution failed "
                    f"for {record.record_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

                # The record itself has already been indexed
                # successfully. Do not undo that work merely
                # because pending relationship resolution failed.
                print(
                    "Continuing despite pending relationship "
                    "resolution failure."
                )

        return {
            "record_id": record.record_id,
            "chunks_indexed": indexed,
            "status": "indexed",
        }

    # ============================================================
    # RELATIONSHIP INDEXING
    # ============================================================

    def _index_relationships(
        self,
        record: KnowledgeRecord,
    ):

        """
        Ask the relationship service to identify relationships.

        The RelationshipService should be responsible for
        deterministic extraction and storage.

        Example:
            ServiceNow Incident content contains CHG0012345
                ->
            relationship to the corresponding Change Request.
        """

        if not self.relationship_service:
            return

        # Preferred interface.
        #
        # RelationshipService can internally:
        #   - extract identifiers
        #   - resolve them using RecordIdentityService
        #   - create relationships
        #
        return self.relationship_service.index_record(
            record=record,
            record_identity_service=(
                self.record_identity_service
            ),
        )

    def index_relationships(
        self,
        record: KnowledgeRecord,
    ):
        if not self.relationship_service:
            return []

        return self._index_relationships(
            record=record
        )

    # Understand the intent of user query
    def _plan_query(
        self,
        query: str,
        exact_record: dict | None = None,
    ) -> dict:
        """
        Classify the user's question and produce a small retrieval plan.

        Possible question_type values:

            content
            relationship
            mixed

        The planner does NOT:
            - retrieve records
            - traverse relationships
            - decide whether a relationship actually exists
            - answer the question

        It only determines what kind of retrieval strategy should be used.

        Example:

            "What caused INC0010008?"
            =>
            {
                "question_type": "content",
                "target_entity_type": None,
                "max_depth": 0
            }

            "Which Jira issues are related to INC0010008?"
            =>
            {
                "question_type": "relationship",
                "seed_entity_type": "incident",
                "target_entity_type": "jira_issue",
                "max_depth": 2
            }

            "Which Jira issues are related to INC0010008 and
             what is their current status?"
            =>
            {
                "question_type": "mixed",
                "target_entity_type": "jira_issue",
                "max_depth": 2,
                "content_intent": "current status"
            }
        """

        if not query or not query.strip():
            return {
                "question_type": "content",
                "seed_entity_type": None,
                "target_entity_type": None,
                "max_depth": 0,
                "content_intent": None,
            }

        # ----------------------------------------------------------
        # Allowed canonical entity types
        # ----------------------------------------------------------
        #
        # Keep this controlled. Don't let the LLM invent entity types.
        #

        allowed_entity_types = [
            "account",
            "contact",
            "opportunity",
            "opportunity_line_item",
            "product",
            "case",
            "incident",
            "problem",
            "change_request",
            "jira_issue",
            "machine",
            "production_order",
            "quality_event",
            "supplier",
        ]

        allowed_entity_types_text = ", ".join(
            allowed_entity_types
        )

        # ========================================================== 
        # Exact record context 
        # 
        # IMPORTANT: 
        # If an exact record was already resolved by search(), 
        # that record is authoritative as the seed. 
        # ==========================================================

        exact_record_context = "NONE" 
        if exact_record: 
            exact_record_context = (
                f"""
EXACT RECORD FOUND 

Record ID: {exact_record.get("record_id", "")} 

Source Type: {exact_record.get("source_type", "")} 

Entity Type: {exact_record.get("entity_type", "")} 

Title: {exact_record.get("title", "")} 

External ID: {exact_record.get("external_id", "")} 

ServiceNow Table: {exact_record.get("servicenow_table", "")}

ServiceNow Number: {exact_record.get("servicenow_number", "")} 
""".strip()
            )



        # ----------------------------------------------------------
        # Prompt
        # ----------------------------------------------------------

        prompt = f"""
You are a query planner for an enterprise knowledge graph.

USER QUESTION:
{query}

{exact_record_context}

ALLOWED TARGET ENTITY TYPES:
{allowed_entity_types_text}

Classify the question into exactly one of:

1. content
   The user primarily wants information, explanation, summary,
   cause, resolution, status, description, instructions, etc.

2. relationship
   The user primarily wants to know WHICH records/entities are
   related, associated, linked, belong to, contain, use, affect,
   depend on, or connect to another entity.

3. mixed
   The user wants related entities AND information/content about
   those entities.

Examples:

CONTENT:
The user primarily wants information about an entity or topic.

"What caused INC0010008?"
"What is the resolution of this incident?"
"Explain the bearing failure."
"How do I replace the bearing?"

For content questions: 
- seed_entity_type may be populated if an exact entity is known. 
- target_entity_type should normally be null. 
- max_depth should be 0.

RELATIONSHIP:
The user primarily wants to know WHICH entities/records are connected to another entity.

"Which opportunities belong to this account?"
"Which products are associated with this opportunity?"
"Which incidents affect this account?"
"Which Jira issues are related to this incident?"

For relationship questions: 
- identify the seed_entity_type 
- identify the target_entity_type 
- choose a suitable max_depth

MIXED:
The user wants related records AND information about those records.

"Which Jira issues are related to INC0010008 and what is their
current status?"
"Which opportunities belong to this account and what are their
stages?"
"Which products are used by this customer and what quantities?"

Rules:

- If an exact record is supplied, treat it as the authoritative seed.
- Do not invent an entity type.
- seed_entity_type must be one of the allowed entity types or null.
- target_entity_type must be one of the allowed entity types or null.
- For content questions, target_entity_type should normally be null.
- For relationship and mixed questions, identify the entity type
  the user is asking to retrieve.
- max_depth should be a small integer between 1 and 4.
- Use 1 for direct relationships.
- Use 2 when one intermediate entity is likely.
- Use 3 or 4 only when the question clearly requires multiple hops.
- content_intent should describe what information is wanted about
  the target records for mixed questions.
- Do not answer the user's question.
- Return JSON only.

Return exactly:

{{
    "question_type": "content | relationship | mixed",
    "seed_entity_type": null,
    "target_entity_type": null,
    "max_depth": 0,
    "content_intent": null
}}
"""

        # ----------------------------------------------------------
        # Call LLM
        # ----------------------------------------------------------

        try:

            response = (
                self.openai_client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a precise enterprise "
                                "query planner. "
                                "Return JSON only."
                            ),
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                )
            )

        except Exception as exc:

            print(
                "Query planner failed:",
                type(exc).__name__,
                str(exc),
            )

            # Safe fallback.
            return {
                "question_type": "content",
                "target_entity_type": None,
                "max_depth": 0,
                "content_intent": None,
            }

        # ----------------------------------------------------------
        # Extract response
        # ----------------------------------------------------------

        raw_content = (
            response
            .choices[0]
            .message
            .content
            or ""
        ).strip()

        if not raw_content:

            return {
                "question_type": "content",
                "target_entity_type": None,
                "max_depth": 0,
                "content_intent": None,
            }

        # ----------------------------------------------------------
        # Parse JSON
        # ----------------------------------------------------------

        try:

            parsed = json.loads(raw_content)

        except json.JSONDecodeError as exc:

            print(
                "Unable to parse query planner response:",
                exc,
            )

            return {
                "question_type": "content",
                "target_entity_type": None,
                "max_depth": 0,
                "content_intent": None,
            }

        if not isinstance(
            parsed,
            dict,
        ):

            return {
                "question_type": "content",
                "target_entity_type": None,
                "max_depth": 0,
                "content_intent": None,
            }

        # ----------------------------------------------------------
        # Validate question type
        # ----------------------------------------------------------

        question_type = (
            parsed.get(
                "question_type"
            )
            or "content"
        ).strip().lower()

        if question_type not in {
            "content",
            "relationship",
            "mixed",
        }:

            question_type = "content"

        # ----------------------------------------------------------
        # Validate seed entity type
        # ----------------------------------------------------------

        seed_entity_type = ( 
            parsed.get( 
                "seed_entity_type" 
            ) 
        )

        if seed_entity_type: 
            seed_entity_type = ( 
                str( 
                    seed_entity_type 
                ) 
                .strip() 
                .lower() 
            ) 

            if ( 
                seed_entity_type 
                not in allowed_entity_types 
            ): 
                seed_entity_type = None


        # ----------------------------------------------------------
        # Validate target entity type
        # ----------------------------------------------------------

        target_entity_type = (
            parsed.get(
                "target_entity_type"
            )
        )

        if target_entity_type:

            target_entity_type = (
                str(
                    target_entity_type
                )
                .strip()
                .lower()
            )

            if (
                target_entity_type
                not in allowed_entity_types
            ):
                target_entity_type = None

        # Content questions generally don't need a target entity.
        if question_type == "content":

            target_entity_type = None

        # ----------------------------------------------------------
        # Validate max depth
        # ----------------------------------------------------------

        try:

            max_depth = int(
                parsed.get(
                    "max_depth",
                    0,
                )
                or 0
            )

        except (
            TypeError,
            ValueError,
        ):

            max_depth = 0

        max_depth = max(
            0,
            min(
                4,
                max_depth,
            ),
        )

        if (
            question_type
            in {
                "relationship",
                "mixed",
            }
            and max_depth == 0
        ):

            max_depth = 2

        # ----------------------------------------------------------
        # Content intent
        # ----------------------------------------------------------

        content_intent = (
            parsed.get(
                "content_intent"
            )
        )

        if content_intent is not None:

            content_intent = str(
                content_intent
            ).strip()

            if not content_intent:
                content_intent = None

        # ----------------------------------------------------------
        # Final validated plan
        # ----------------------------------------------------------

        plan = {
            "question_type": question_type,
            "seed_entity_type": seed_entity_type,
            "target_entity_type": target_entity_type,
            "max_depth": max_depth,
            "content_intent": content_intent,
        }

        # ========================================================== 
        # Exact record is authoritative 
        # 
        # The planner can help determine intent, but once search() 
        # resolves an exact record, search() should override the seed 
        # entity type using the actual persisted record. 
        # ==========================================================
        
        if exact_record: 
            exact_entity_type = ( 
                exact_record.get( 
                    "entity_type" 
                ) 
            )
            
            if exact_entity_type: 
                exact_entity_type = ( 
                    str( 
                        exact_entity_type 
                    ) 
                    .strip() 
                    .lower() 
                )

                if exact_entity_type in ( 
                    allowed_entity_types 
                ):
                    plan["seed_entity_type"] = exact_entity_type

            plan["seed_record_id"] = exact_record.get("record_id")      

        print("******** Query Plan ********")
        print(plan)
        print("*****************************")

        return plan


    # ==============================================================
    # Semantic search
    # ==============================================================

    def search(
    self,
    query: str,
    top_k: int = 8,
    source_type: Optional[str] = None,
    servicenow_table: Optional[str] = None,
    servicenow_number: Optional[str] = None,
    exact_record: bool = False,
    ) -> list[dict]:
        """
        Main knowledge search entry point.

        Search flow:

        1. Resolve exact ServiceNow record if requested.
        2. Plan the query.
        3. Dispatch:
             - content
             - relationship
             - mixed
        4. Rerank the resulting candidates.
        """

        query = (
            query or ""
        ).strip()

        if not query:
            raise ValueError(
                "Query is required."
            )

        top_k = self._normalize_top_k(
            top_k
        )

        print("======================= Start -> About to retrieve chunks that are relevant to given Query ===========================\n\n")

        print(f"******** Query -> {query} ********\n\n")

        # ==========================================================
        # STEP 1
        # Exact ServiceNow lookup
        #
        # Do this BEFORE query planning.
        # ==========================================================

        print("******** Step 1 - Let's check whether user query is related to a specific ServiceNow record ********\n\n")
        exact_results = []

        if exact_record:

            print("******** Looking for ServiceNow records ********\n\n")
            if (
                source_type != "servicenow"
                or not servicenow_table
                or not servicenow_number
            ):
                raise ValueError(
                    "Exact ServiceNow search requires "
                    "source_type='servicenow', "
                    "servicenow_table and "
                    "servicenow_number."
                )

            exact_results = (
                self.repository.search_exact_record(
                    source_type="servicenow",
                    servicenow_table=servicenow_table,
                    servicenow_number=servicenow_number,
                    top_k=top_k,
                )
            )

            print("******** Step 1 - Retrieved exact ServiceNow -> {exact_results} ********\n\n")

        # ==========================================================
        # STEP 2
        # Build planner context
        # ==========================================================

        exact_record = None

        if exact_results:

            exact_record = dict(exact_results[0])

            # The entity_type is now stored in Chroma.
            if not exact_record.get("entity_type"):

                print("*********** Step 2 - Entity Type of ServiceNow record is missing !!! ***********")

                exact_record[
                    "entity_type"
                ] = (
                    self.repository
                    .get_entity_type_for_record(
                        exact_record.get(
                            "record_id"
                        )
                    )
                )

        print(f"*********** Step 2 - Exact Record of ServiceNow is {exact_record}  ***********\n\n")

        # ==========================================================
        # STEP 3
        # Query planning
        # ==========================================================

        print("************* Step 3 -> Now, we will analyse the query to understand the nature of the query *************\n\n")

        query_plan = self._plan_query(
            query=query,
            exact_record=exact_record,
        )

        # Exact record is authoritative for seed entity type.
        if exact_record:

            print("*********** Step 3 - We are overriding the Seed Record Id and Seed Entity Type with that of ServiceNow record  ***********\n\n")
            query_plan[
                "seed_record_id"
            ] = exact_record.get(
                "record_id"
            )

            query_plan[
                "seed_entity_type"
            ] = exact_record.get(
                "entity_type"
            )

        print(f"************* Step 3 -> Final Query plan -> {query_plan} *************\n\n")

        # ==========================================================
        # STEP 4
        # Dispatch
        # ==========================================================

        question_type = (
            query_plan.get(
                "question_type",
                "content",
            )
        )

        print(f"************* Step 4 -> Query Type -> {question_type} *************\n\n")

        if question_type == "relationship":

            print("************* Step 5 -> It is Relationship-oriented Query, so, about to search for same  *************\n\n")
            return self._relationship_search(
                query=query,
                query_plan=query_plan,
                exact_record=exact_record,
                exact_results=exact_results,
                top_k=top_k,
            )

        if question_type == "mixed":

            print("************* Step 5 -> It is Mixed-type Query, so, about to search for same  *************\n\n")
            return self._mixed_search(
                query=query,
                query_plan=query_plan,
                exact_record=exact_record,
                exact_results=exact_results,
                top_k=top_k,
            )

        # Default:
        # content
        print("************* Step 5 -> It is generic content-type Query, so, about to search for same  *************\n\n")
        return self._content_search(
            query=query,
            query_plan=query_plan,
            exact_record=exact_record,
            exact_results=exact_results,
            source_type=source_type,
            servicenow_table=servicenow_table,
            servicenow_number=servicenow_number,
            top_k=top_k,
        )

    # Content search
    def _content_search(
    self,
    query: str,
    query_plan: dict,
    exact_record: dict | None,
    exact_results: list[dict],
    source_type: Optional[str],
    servicenow_table: Optional[str],
    servicenow_number: Optional[str],
    top_k: int,
    ) -> list[dict]:
        """
        Existing content/RAG search.

        Exact ServiceNow record:
            exact record
            -> related records
            -> reranker

        Normal search:
            dense + lexical
            -> RRF
            -> hybrid ranking
            -> relationship expansion
            -> reranker
        """
        print("************* Step 6 -> Performing search for generic content-type Query *************\n\n")

        # ==========================================================
        # Exact record + CONTENT question
        #
        # Preserve your existing behavior.
        # ==========================================================

        if exact_record and exact_results:
        
            print("************* Step 7 -> We already have the exact record retrieved earlier, so, will use that record to identify related records using Relatioships *************\n\n")

            expanded_results = list(
                exact_results
            )

            query_embedding = (
                self.embed_fn([query])[0]
            )

            for result in exact_results:

                record_id = result.get(
                    "record_id"
                )

                if not record_id:
                    continue

                related_by_depth = (
                    self.relationship_service
                    .traverse_related_records(
                        record_id,
                        max_depth=2,
                    )
                    if self.relationship_service
                    else {}
                )

                for (
                    depth,
                    related_items,
                ) in related_by_depth.items():

                    for related_item in (
                        related_items
                    ):

                        related_record_id = (
                            related_item.get(
                                "record_id"
                            )
                        )

                        if not related_record_id:
                            continue

                        relationship_type = (
                            related_item.get(
                                "relationship_type"
                            )
                        )

                        relationship_confidence = (
                            related_item.get(
                                "relationship_confidence",
                                1.0,
                            )
                        )

                        related_result = (
                            self.repository
                            .search_vector_for_record(
                                embedding=query_embedding,
                                record_id=related_record_id,
                                top_k=3,
                            )
                        )

                        related_chunks = (
                            self._format_vector_results(
                                related_result
                            )
                        )

                        for related_chunk in (
                            related_chunks
                        ):

                            related_chunk = dict(
                                related_chunk
                            )

                            related_chunk[
                                "retrieval_source"
                            ] = "relationship"

                            related_chunk[
                                "related_to"
                            ] = record_id

                            related_chunk[
                                "relationship_from"
                            ] = related_item.get(
                                "from_record_id"
                            )

                            related_chunk[
                                "relationship_type"
                            ] = relationship_type

                            related_chunk[
                                "relationship_confidence"
                            ] = (
                                relationship_confidence
                            )

                            related_chunk[
                                "relationship_depth"
                            ] = depth

                            if depth == 1:
                                graph_relevance = 1.0

                            elif depth == 2:
                                graph_relevance = 0.75

                            else:
                                graph_relevance = (
                                    1.0 / depth
                                )

                            graph_relevance *= (
                                relationship_confidence
                            )

                            related_chunk[
                                "graph_relevance"
                            ] = graph_relevance

                            expanded_results.append(
                                related_chunk
                            )

            print("************* Step 8 -> Identified set of candidate chunks, now, will re-rank those candidates  *************\n\n")

            return self._rerank_candidates(
                query=query,
                candidates=expanded_results,
                top_k=top_k,
            )

        # ==========================================================
        # NORMAL CONTENT SEARCH
        # ==========================================================

        print("************* Step 7 -> We did not find any exact record earlier, so, will perform generic content search in Vector database *************\n\n")

        query_embedding = (
            self.embed_fn([query])[0]
        )

        where = self._build_where_clause(
            source_type=source_type,
            servicenow_table=servicenow_table,
            servicenow_number=servicenow_number,
        )

        candidate_k = max(
            top_k * 4,
            20,
        )

        # ----------------------------------------------------------
        # Dense search
        # ----------------------------------------------------------

        print("************* Step 7a -> Performing dense search in Vector database *************\n\n")
        dense_result = (
            self.repository.search_vector(
                embedding=query_embedding,
                top_k=candidate_k,
                where=where,
            )
        )

        dense_results = (
            self._format_vector_results(
                dense_result
            )
        )

        # ----------------------------------------------------------
        # Lexical search
        # ----------------------------------------------------------

        print("************* Step 7b -> Now, performing lexical search *************\n\n")
        keyword_results = []

        if self.lexical_repository:

            keyword_results = (
                self.lexical_repository
                .search_keyword(
                    query=query,
                    top_k=candidate_k,
                    source_type=source_type,
                )
            )

        # ----------------------------------------------------------
        # RRF
        # ----------------------------------------------------------

        print("************* Step 7c -> Now, performing RRF using dense and lexical search results, this will give use hybrid results *************\n\n")
        hybrid_results = (
            self._rrf_fuse(
                dense_results=dense_results,
                keyword_results=keyword_results,
                top_k=candidate_k,
            )
        )

        # ----------------------------------------------------------
        # Hybrid ranking
        # ----------------------------------------------------------

        print("************* Step 7d -> Now, performing ranking of hybrid search results *************\n\n")
        ranked_results = (
            self._rank_hybrid_results(
                query=query,
                results=hybrid_results,
            )
        )

        # ----------------------------------------------------------
        # Existing relationship expansion
        # ----------------------------------------------------------

        print("************* Step 8 -> Now, we will pick up top 5 hybrid results and find related records for each of these *************\n\n")
        expanded_results = list(
            ranked_results
        )

        seed_results = (
            ranked_results[:5]
        )

        for seed in seed_results:

            seed_record_id = (
                seed.get(
                    "record_id"
                )
            )

            if not seed_record_id:
                continue

            print(
                "********* Calling relationship "
                f"traversal for {seed_record_id} ***********"
            )

            related_by_depth = (
                self.relationship_service
                .traverse_related_records(
                    seed_record_id,
                    max_depth=2,
                )
                if self.relationship_service
                else {}
            )

            for (
                depth,
                related_items,
            ) in related_by_depth.items():

                for related_item in (
                    related_items
                ):

                    related_record_id = (
                        related_item.get(
                            "record_id"
                        )
                    )

                    if not related_record_id:
                        continue

                    relationship_type = (
                        related_item.get(
                            "relationship_type"
                        )
                    )

                    relationship_confidence = (
                        related_item.get(
                            "relationship_confidence",
                            1.0,
                        )
                    )

                    related_result = (
                        self.repository
                        .search_vector_for_record(
                            embedding=query_embedding,
                            record_id=related_record_id,
                            top_k=3,
                        )
                    )

                    related_chunks = (
                        self._format_vector_results(
                            related_result
                        )
                    )

                    for related_chunk in (
                        related_chunks
                    ):

                        related_chunk = dict(
                            related_chunk
                        )

                        related_chunk[
                            "retrieval_source"
                        ] = "relationship"

                        related_chunk[
                            "related_to"
                        ] = seed_record_id

                        related_chunk[
                            "relationship_from"
                        ] = related_item.get(
                            "from_record_id"
                        )

                        related_chunk[
                            "relationship_type"
                        ] = relationship_type

                        related_chunk[
                            "relationship_confidence"
                        ] = (
                            relationship_confidence
                        )

                        related_chunk[
                            "relationship_depth"
                        ] = depth

                        if depth == 1:
                            graph_relevance = 1.0

                        elif depth == 2:
                            graph_relevance = 0.75

                        else:
                            graph_relevance = (
                                1.0 / depth
                            )

                        graph_relevance *= (
                            relationship_confidence
                        )

                        related_chunk[
                            "graph_relevance"
                        ] = graph_relevance

                        expanded_results.append(
                            related_chunk
                        )

        print("************* Step 9 -> We have got all the candidate results - now, we will rerank those candidates *************\n\n")
        return self._rerank_candidates(
            query=query,
            candidates=expanded_results,
            top_k=top_k,
        )

    def _rerank_candidates(
    self,
    query: str,
    candidates: list[dict],
    top_k: int,
    ) -> list[dict]:

        print("************* Starting reranking of chunk candidates *************\n\n")
        unique_results = []

        seen_chunks = set()

        for result in candidates:

            chunk_id = result.get(
                "chunk_id"
            )

            if not chunk_id:
                continue

            if chunk_id in seen_chunks:
                continue

            seen_chunks.add(
                chunk_id
            )

            unique_results.append(
                result
            )

        print("************* Sending chunk candidates to LLM for reranking *************\n\n")
        return self._rerank_with_llm(
            query=query,
            results=unique_results,
            top_k=top_k,
        )

    # User Query involves Relationship-based search
    def _relationship_search(
        self,
        query: str,
        query_plan: dict,
        exact_record: dict | None,
        exact_results: list[dict],
        top_k: int,
    ) -> list[dict]:
        """
        Graph-first retrieval for relationship questions.

        Example:

            Which opportunities belong to this account?

        Graph:

            Account
                ↓
            Opportunity
        """

        print("************* Step 6 -> Starting search for relationship-oriented query *************\n\n")
        target_entity_type = (
            query_plan.get(
                "target_entity_type"
            )
        )

        if not target_entity_type:
            print(
                "Relationship query has no target entity. "
                "Falling back to content search."
            )

            return self._content_search(
                query=query,
                query_plan=query_plan,
                exact_record=exact_record,
                exact_results=exact_results,
                source_type=None,
                servicenow_table=None,
                servicenow_number=None,
                top_k=top_k,
            )

        # ==========================================================
        # Determine seed
        # ==========================================================

        print("************* Step 7 -> Determining seed record *************\n\n")

        seed_record_id = (
            query_plan.get(
                "seed_record_id"
            )
        )

        # ----------------------------------------------------------
        # Exact record is authoritative.
        # ----------------------------------------------------------

        if exact_record:
 
            print("************* Since we have exact record, it becomes the seed record *************\n\n")
            seed_record_id = (
                exact_record.get(
                    "record_id"
                )
            )

        # ----------------------------------------------------------
        # No exact record:
        # resolve the seed using semantic search.
        # ----------------------------------------------------------

        if not seed_record_id:

            seed_record_id = (
                self._resolve_relationship_seed(
                    query=query,
                    query_plan=query_plan,
                )
            )

        if not seed_record_id:

            print(
                "Unable to resolve relationship seed."
            )

            return []

        print(f"******** Identified seed record -> {seed_record_id} ********\n\n")

        # ==========================================================
        # Graph traversal
        # ==========================================================

        print("******** Now going to find out the list of related records for the identified seed record ********\n\n")
        matches = (
            self.relationship_service
            .find_related_records(
                start_record_id=seed_record_id,
                target_entity_type=target_entity_type,
                entity_type_lookup=(
                    self.repository
                    .get_entity_type_for_record
                ),
                max_depth=int(
                    query_plan.get(
                        "max_depth",
                        2,
                    )
                    or 2
                ),
            )
            if self.relationship_service
            else []
        )

        print(f"******** The list of related records are ->  {matches} ********\n\n")

        if not matches:
            return []

        # ==========================================================
        # Retrieve content for the graph-selected records
        # ==========================================================

        query_embedding = (
            self.embed_fn([query])[0]
        )

        print("******** Now retrieving content for each of the related records ********\n\n")

        candidates = []

        for match in matches:

            print(f"******** Here is the related record -> {match} ********\n\n")

            record_id = (
                match.get(
                    "record_id"
                )
            )

            if not record_id:
                continue

            path = (
                match.get(
                    "path",
                    []
                )
                or []
            )

            # The final edge is the relationship that
            # directly reached this target record.
            last_relationship = (
                path[-1]
                if path
                else {}
            )

            relationship_type = (
                last_relationship.get(
                    "relation_type"
                )
            )

            relationship_confidence = float(
                last_relationship.get(
                    "confidence",
                    1.0,
                )
                or 1.0
            )

            related_result = (
                self.repository
                .search_vector_for_record(
                    embedding=query_embedding,
                    record_id=record_id,
                    top_k=3,
                )
            )

            related_chunks = (
                self._format_vector_results(
                    related_result
                )
            )

            for chunk in related_chunks:

                chunk = dict(
                    chunk
                )

                depth = int(
                    match.get(
                        "depth",
                        1,
                    )
                    or 1
                )

                chunk[
                    "retrieval_source"
                ] = "relationship_query"

                # Get metadata for the seed record id
                seed_metadata = self.repository.get_metadata_for_record(seed_record_id)

                chunk["relationship_seed_record_id"] = seed_record_id

                chunk["relationship_seed_entity_type"] = (
                    seed_metadata.get("entity_type", "",)
                )

                chunk["relationship_seed_source_type"] = (
                    seed_metadata.get("source_type", "",)
                )

                chunk["relationship_seed_external_id"] = (
                    seed_metadata.get("servicenow_number", "",) or seed_metadata.get("external_id", "",)
                )

                chunk[
                    "entity_type"
                ] = match.get(
                    "entity_type"
                )

                chunk[
                    "relationship_type"
                ] = relationship_type

                chunk[
                    "relationship_confidence"
                ] = relationship_confidence

                chunk[
                    "relationship_depth"
                ] = depth

                chunk[
                    "relationship_path"
                ] = match.get(
                    "path",
                    [],
                )

                chunk[
                    "graph_relevance"
                ] = (
                    1.0
                    / max(
                        1,
                        depth,
                    )
                )

                candidates.append(
                    chunk
                )

        print(f"******** Sending the following set of candidate chunks for reranking by LLM -> {candidates} ********\n\n")

        return self._rerank_candidates(
            query=query,
            candidates=candidates,
            top_k=top_k,
        )

    # Determines the Starting Seed record for Relationship-based User Query
    def _resolve_relationship_seed(
    self,
    query: str,
    query_plan: dict,
    ) -> str | None:
        """
        Resolve the starting entity for a relationship query.

        The semantic search here is NOT the final retrieval.
        It only identifies the graph seed.
        """

        query_embedding = (
            self.embed_fn([query])[0]
        )

        candidate_k = 10

        dense_result = (
            self.repository.search_vector(
                embedding=query_embedding,
                top_k=candidate_k,
                where=None,
            )
        )

        dense_results = (
            self._format_vector_results(
                dense_result
            )
        )

        desired_seed_type = (
            query_plan.get(
                "seed_entity_type"
            )
        )

        # First preference:
        # exact entity type requested by planner.
        if desired_seed_type:

            for result in dense_results:

                if (
                    result.get(
                        "entity_type"
                    )
                    == desired_seed_type
                ):

                    return result.get(
                        "record_id"
                    )

        # Fallback:
        # strongest semantic result.
        if dense_results:

            return dense_results[0].get(
                "record_id"
            )

        return None

    # Performs mixed search
    def _mixed_search(
        self,
        query: str,
        query_plan: dict,
        exact_record: dict | None,
        exact_results: list[dict],
        top_k: int,
    ) -> list[dict]:
        """
        Mixed query:

            graph determines WHICH records
            content retrieval determines WHAT information
            is relevant about those records.
        """

        print("************* Step 6 -> Starting search for mixed type of query *************\n\n")
        target_entity_type = (
            query_plan.get(
                "target_entity_type"
            )
        )

        if not target_entity_type:
            return self._content_search(
                query=query,
                query_plan=query_plan,
                exact_record=exact_record,
                exact_results=exact_results,
                source_type=None,
                servicenow_table=None,
                servicenow_number=None,
                top_k=top_k,
            )

        # ----------------------------------------------------------
        # Seed
        # ----------------------------------------------------------

        print("************* Step 7 -> We will first find out the seed record *************\n\n")
        if exact_record:

            print(f"************* We already have the exact record - that will be used as seed record -> {exact_record} *************\n\n")

            seed_record_id = (
                exact_record.get(
                    "record_id"
                )
            )

        else:

            seed_record_id = (
                self._resolve_relationship_seed(
                    query=query,
                    query_plan=query_plan,
                )
            )

            print(f"************* Here is the seed record we have identified through relationships -> {seed_record_id} *************\n\n")

        if not seed_record_id:
            return []

        # ----------------------------------------------------------
        # Graph
        # ----------------------------------------------------------

        print("************* Step 8 -> Now we will find out related records for the given seed record *************\n\n")
        matches = (
            self.relationship_service
            .find_related_records(
                start_record_id=seed_record_id,
                target_entity_type=target_entity_type,
                entity_type_lookup=(
                    self.repository
                    .get_entity_type_for_record
                ),
                max_depth=int(
                    query_plan.get(
                        "max_depth",
                        3,
                    )
                    or 3
                ),
            )
        )

        if not matches:
            return []

        print(f"************* Here are the related records for the given seed record -> {matches} *************\n\n")

        print("************* Now, we will find out the content for each of the related records *************\n\n")

        # ----------------------------------------------------------
        # Retrieve content
        # ----------------------------------------------------------

        query_embedding = (
            self.embed_fn([query])[0]
        )

        candidates = []

        for match in matches:

            record_id = (
                match.get(
                    "record_id"
                )
            )

            if not record_id:
                continue

            related_result = (
                self.repository
                .search_vector_for_record(
                    embedding=query_embedding,
                    record_id=record_id,
                    top_k=3,
                )
            )

            print(f"************* Here is the content we have found for the given related record -> {related_result} *************\n\n")

            related_chunks = (
                self._format_vector_results(
                    related_result
                )
            )

            for chunk in related_chunks:

                chunk = dict(
                    chunk
                )

                depth = int(
                    match.get(
                        "depth",
                        1,
                    )
                    or 1
                )

                chunk[
                    "retrieval_source"
                ] = "relationship_query"

                chunk[
                    "related_to"
                ] = seed_record_id

                chunk[
                    "entity_type"
                ] = match.get(
                    "entity_type"
                )

                chunk[
                    "relationship_depth"
                ] = depth

                chunk[
                    "relationship_path"
                ] = match.get(
                    "path",
                    [],
                )

                chunk[
                    "graph_relevance"
                ] = (
                    1.0
                    / max(
                        1,
                        depth,
                    )
                )

                # Useful for the reranker.
                chunk[
                    "content_intent"
                ] = query_plan.get(
                    "content_intent"
                )

                candidates.append(
                    chunk
                )

        print(f"************* We will send the following content chunks for reranking by LLM -> {candidates} *************\n\n")

        return self._rerank_candidates(
            query=query,
            candidates=candidates,
            top_k=top_k,
        )

    # Old working code for search
    def search_old(
        self,
        query: str,
        top_k: int = 8,
        source_type: Optional[str] = None,
        servicenow_table: Optional[str] = None,
        servicenow_number: Optional[str] = None,
        exact_record: bool = False,
    ) -> list[dict]:
        """
        Search the knowledge base.

        Generic search:
            search(query="how does authentication work")

        ServiceNow semantic search:
            search(
                query="authentication incidents",
                source_type="servicenow",
                servicenow_table="incident",
            )

        Exact ServiceNow record:
            search(
                query="INC0010002",
                source_type="servicenow",
                servicenow_table="incident",
                servicenow_number="INC0010002",
                exact_record=True,
            )
        """

        query = (query or "").strip()

        print(f"*********** Query passed to KnowledgeSearch -> {query} ****************")
        if not query:
            raise ValueError(
                "Query is required."
            )

        # Find the Query Intent
        query_plan = self._plan_query(query=query,)

        print(f"************* Query plan is {query_plan} ****************")

        top_k = self._normalize_top_k(
            top_k
        )

        # ----------------------------------------------------------
        # Exact ServiceNow lookup
        # ----------------------------------------------------------

        if exact_record:
            if (
                source_type != "servicenow"
                or not servicenow_table
                or not servicenow_number
            ):
                raise ValueError(
                    "Exact ServiceNow search requires "
                    "source_type='servicenow', "
                    "servicenow_table and "
                    "servicenow_number."
                )

            exact_results = (
                self.repository.search_exact_record(
                    source_type="servicenow",
                    servicenow_table=servicenow_table,
                    servicenow_number=servicenow_number,
                    top_k=top_k,
                )
            )

            # ----------------------------------------------------------
            # Expand relationships from the exact record
            # ----------------------------------------------------------

            expanded_results = list(exact_results)

            query_embedding = self.embed_fn([query])[0]

            for result in exact_results:

                record_id = result.get("record_id")

                if not record_id:
                    continue

                related_record_ids = self.relationship_service.get_related_record_ids(record_id)

                for related_record_id in related_record_ids:

                    related_result = (
                        self.repository
                        .search_vector_for_record(
                            embedding=query_embedding,
                            record_id=related_record_id,
                            top_k=3,
                        )
                    )

                    related_chunks = (self._format_vector_results(related_result))

                    for related_chunk in related_chunks:

                        related_chunk = dict(related_chunk)

                        related_chunk["retrieval_source"] = "relationship"

                        related_chunk["related_to"] = record_id

                        expanded_results.append(related_chunk)

            # ----------------------------------------------------------
            # Deduplicate
            # ----------------------------------------------------------

            unique_results = []
            seen_chunks = set()

            for result in expanded_results:

                chunk_id = result.get("chunk_id")

                if not chunk_id:
                    continue

                if chunk_id in seen_chunks:
                    continue

                seen_chunks.add(chunk_id)

                unique_results.append(result)

            # ----------------------------------------------------------
            # Final LLM relevance reranking
            # ----------------------------------------------------------

            return self._rerank_with_llm(
                query=query,
                results=unique_results,
                top_k=top_k,
            )

        # ----------------------------------------------------------
        # Semantic search
        # ----------------------------------------------------------

        query_embedding = self.embed_fn(
            [query]
        )[0]

        where = self._build_where_clause(
            source_type=source_type,
            servicenow_table=servicenow_table,
            servicenow_number=servicenow_number,
        )

        # Retrieve more candidates than we finally return.
        candidate_k = max(
            top_k * 4,
            20,
        )

        
        dense_result = self.repository.search_vector(
            embedding=query_embedding,
            top_k=candidate_k,
            where=where,
        )

        
        dense_results = (
            self._format_vector_results(
                dense_result
            )
        )


        # Now, perform lexical search
        keyword_results = []

        if self.lexical_repository:

            keyword_results = (
                self.lexical_repository.search_keyword(
                    query=query,
                    top_k=candidate_k,
                    source_type=source_type,
                )
            )

        # Hybrid search result
        hybrid_results = self._rrf_fuse(
            dense_results=dense_results,
            keyword_results=keyword_results,
            top_k=candidate_k,
        )

        # Rank hybrid search results
        ranked_results = (
            self._rank_hybrid_results(
                query=query,
                results=hybrid_results,
            )
        )

        # ----------------------------------------------------------
        # Relationship expansion
        # ----------------------------------------------------------

        expanded_results = list(
            ranked_results
        )

        seed_results = ranked_results[:5]

        for seed in seed_results:

            seed_record_id = seed.get("record_id")

            if not seed_record_id:
                continue

            # Traverse all the related records for the given record id
            print(f"********* Calling related record ids for {seed_record_id} ***********")
            related_by_depth = self.relationship_service.traverse_related_records(seed_record_id, max_depth=2,)
            print(f"********* Result -> {related_by_depth}  ***********")
        
            for depth, related_items in (related_by_depth.items()):
                for related_item in related_items:
                    related_record_id = related_item.get("record_id")
                    if not related_record_id: 
                        continue
                    relationship_type = related_item.get("relationship_type")
                    relationship_confidence = related_item.get("relationship_confidence", 1.0,)
                    from_record_id = related_item.get("from_record_id") 
                    related_result = self.repository.search_vector_for_record(embedding=query_embedding, record_id=related_record_id, top_k=3,)
                    related_results = self._format_vector_results(related_result)
                    for related_chunk in related_results:
                        related_chunk = dict(related_chunk)
                        related_chunk["retrieval_source"] = "relationship"
                        related_chunk["related_to"] = seed_record_id
                        related_chunk["relationship_from"] = related_item.get("from_record_id")
                        related_chunk["relationship_type"] = relationship_type
                        related_chunk["relationship_confidence"] = relationship_confidence
                        related_chunk["relationship_depth"] = depth
                        if depth == 1: 
                            graph_relevance = 1.0 
                        elif depth == 2: 
                            graph_relevance = 0.75 
                        else: graph_relevance = ( 1.0 / depth ) 
                        graph_relevance *= relationship_confidence
                        related_chunk["graph_relevance"] = graph_relevance
                        expanded_results.append(related_chunk)


            # The following is commented out since it picks up only next-neighbour related records
            """
            related_record_ids = (
                self.relationship_service.get_related_record_ids(seed_record_id)
            )

            for related_record_id in related_record_ids:

                related_result = (
                    self.repository.search_vector_for_record(
                        embedding=query_embedding,
                        record_id=related_record_id,
                        top_k=3,
                    )
                )

                related_results = (
                    self._format_vector_results(related_result)
                )

                for related_chunk in related_results:

                    related_chunk["retrieval_source"] = "relationship"
                    related_chunk["related_to"] = seed_record_id
                    expanded_results.append(related_chunk)
            """

        # ----------------------------------------------------------
        # Deduplicate
        # ----------------------------------------------------------

        unique_results = []

        seen_chunks = set()

        for result in expanded_results:

            chunk_id = result.get("chunk_id")

            if not chunk_id:
                continue

            if chunk_id in seen_chunks:
                continue

            seen_chunks.add(chunk_id)

            unique_results.append(result)

        # ----------------------------------------------------------
        # Re-rank the search results using LLM based on relevance 
        # ----------------------------------------------------------

        selected_results = (
            self._rerank_with_llm(
                query=query,
                results=unique_results,
                top_k=top_k,
            )
        )

        print("********* LLM reranked results -> " f"{selected_results} **************")

        return selected_results

    # Re-rank the search results using a LLM
    def _rerank_with_llm(
        self,
        query: str,
        results: list[dict],
        top_k: int,
        max_candidates: int = 10,
        min_score: float = 0.60,
    ) -> list[dict]:
        """
        Use the OpenAI-compatible client to rerank search candidates.

        The LLM only evaluates relevance. It does not answer the
        user's question.

        Expected result structure: 
            { 
                "record_id": "...", 
                "text": "...", 
                "final_score": ..., 
                "retrieval_source": "relationship", 
                "relationship_type": "BELONGS_TO_ACCOUNT", 
                "relationship_depth": 1, 
                "relationship_confidence": 1.0, 
                "graph_relevance": 1.0, 
            } 
        Returns: Top-k results sorted by final rerank score.
        """

        if not results:
            return []

        print("************* Starting reranking of chunk candidates using a LLM *************\n\n")

        # ----------------------------------------------------------
        # Limit the number of candidates sent to the LLM.
        # ----------------------------------------------------------

        candidates = results[:max_candidates]

        candidate_blocks = []

        for index, result in enumerate(candidates):

            print(f"************ Chunk Candidate -> {result} *****************\n\n")
            title = (
                result.get("title", "")
                or ""
            ).strip()

            text = (
                result.get(
                    "text",
                    result.get(
                        "content",
                        "",
                    ),
                )
                or ""
            ).strip()

            source_name = (
                result.get(
                    "source_name",
                    "",
                )
                or ""
            ).strip()

            candidate_blocks.append(
                f"""
    Candidate {index}

    Record ID:
    {result.get("record_id", "")}

    Entity type:
    {result.get("entity_type", "")}

    Content:
    {result.get("text", "")}

    Retrieval source:
    {result.get("retrieval_source", "search")}

    Seed record ID:
    {result.get("relationship_seed_record_id", "")}

    Seed entity type:
    {result.get("relationship_seed_entity_type", "")}

    Seed source type:
    {result.get("relationship_seed_source_type", "")}

    Seed external ID:
    {result.get("relationship_seed_external_id", "")}

    Relationship:
    {result.get("relationship_type", "none")}

    Relationship confidence:
    {result.get("relationship_confidence", 0.0)}

    Relationship depth:
    {result.get("relationship_depth", 0)}

    Graph relevance:
    {result.get("graph_relevance", 0.0)}
    """.strip()
            )

        candidates_text = "\n\n".join(
            candidate_blocks
        )

        # ----------------------------------------------------------
        # Relevance-ranking prompt
        # ----------------------------------------------------------

        prompt = f"""
    You are an enterprise knowledge-search reranking engine.

    Your task is to evaluate how relevant each candidate document
    is to the user's question.

    Do NOT answer the user's question.

    User question:
    {query}

    Candidate documents:

    {candidates_text}

    Scoring rules:

    1.0 = Directly answers the user's question or requested task.
    0.9 = Extremely relevant and directly useful.
    0.8 = Highly relevant.
    0.7 = Relevant and useful supporting information.
    0.6 = Somewhat relevant.
    0.5 = Related topic but may not answer the question.
    0.4 = Same general topic but different task or intent.
    0.3 = Weakly related.
    0.2 = Barely related.
    0.1 = Almost irrelevant.
    0.0 = Irrelevant.

    Important:

    - Evaluate the user's actual intent, not just topic similarity.
    - A document can mention the same technology and still be irrelevant.
    - A document about "Kubernetes cluster upgrade" should receive a low
      score for "Create Kubernetes cluster".
    - Prefer documents that directly answer the requested task.
    - Do not infer missing information.
    - Evaluate every candidate.
    - For relationship/list questions such as:
      - "which opportunities..."
      - "what incidents..."
      - "what products..."
      - "what is associated with..."
      - "what is related to..."
      treat an explicit relationship in the supplied graph metadata
      as strong evidence of relevance.
    - If a candidate is connected by an explicit relationship that
      satisfies the user's requested relationship, consider it
      relevant even when textual overlap is low.

    For relationship and mixed questions:

    - The existence of a graph relationship is strong evidence that
      the candidate is connected to the requested seed entity.
    - The relationship_type describes HOW the records are connected.
    - Do not require the relationship_type to exactly match the user's
      wording.
    - Interpret natural-language wording using the relationship type,
      relationship path, candidate content, and the user's overall intent.
    - Do not invent a relationship that is not present in the supplied
      graph metadata.

    For relationship_query candidates:

    1. Assume the graph relationship itself is valid.
    2. Do not question whether the seed and candidate are actually connected.
    3. Evaluate whether the supplied relationship semantics are consistent
       with the user's wording.
    4. A relationship such as REFERENCES is weaker evidence for a question
       asking "caused by" than a relationship such as CAUSED_BY.
    5. Do not invent a stronger relationship than the graph provides.

    GRAPH RETRIEVAL RULES:

    - If retrieval_source = relationship_query, the graph has already
      established that the candidate is connected to the seed record.
    - Do NOT attempt to independently verify or reconstruct the seed
      record identity from opaque internal record IDs.
    - Treat relationship_path and relationship_confidence as supplied
      graph evidence.
    - The human-readable seed identifier may be supplied separately as
      relationship_seed_external_id.
    - Do not reject a graph candidate simply because the candidate text
      does not contain the seed's human-readable identifier.
    - Use the graph relationship and the user's wording to assess
      relevance.

    Return ONLY valid JSON in this exact format:

    {{
      "results": [
        {{
          "index": "...",
          "relevance_score": 0.0,
          "reason": "..."
        }}
      ]
    }}

    Where: 
    relevance_score: 
        0.0 = completely irrelevant 
        1.0 = extremely relevant 
    Return one result entry for every supplied candidate.
    """

        try:

            response = self.openai_client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a precise enterprise "
                            "search relevance evaluator."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
            )

            content = (
                response.choices[0]
                .message
                .content
                or ""
            ).strip()

            print(f"********* Response from LLM Reranker -> {content} *********\n\n")

            # ------------------------------------------------------
            # Parse JSON
            # ------------------------------------------------------

            parsed = None

            try:
                parsed = json.loads(
                    content
                )

            except json.JSONDecodeError:

                # Try extracting JSON from surrounding text.
                match = re.search(
                    r"\{.*\}",
                    content,
                    re.DOTALL,
                )

                if match:
                    try:
                        parsed = json.loads(
                            match.group(0)
                        )
                    except json.JSONDecodeError:
                        parsed = None

            if not isinstance(
                parsed,
                dict,
            ):
                print("LLM reranker returned invalid JSON.")
                return []

            scores = parsed.get(
                "results",
                [],
            )

            if not isinstance(
                scores,
                list,
            ):
                return []

            # ------------------------------------------------------
            # Attach scores to original candidates
            # ------------------------------------------------------

            reranked = []

            for item in scores:

                if not isinstance(
                    item,
                    dict,
                ):
                    continue

                try:
                    index = int(
                        item.get(
                            "index"
                        )
                    )

                    score = float(
                        item.get(
                            "relevance_score",
                            0.0,
                        )
                    )

                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                if (
                    index < 0
                    or index >= len(candidates)
                ):
                    continue

                # Let's calculate a Hybrid score here

                # --------------------------------------------------
                # Get original candidate
                # --------------------------------------------------
                candidate = dict(candidates[index])

                # --------------------------------------------------
                # Existing hybrid / retrieval score
                # --------------------------------------------------

                hybrid_score = float(
                    candidate.get(
                        "final_score",
                        candidate.get(
                            "rrf_score",
                            candidate.get(
                                "score",
                                0.0,
                            ),
                        ),
                    )
                    or 0.0
                )
          
                print(f"********* Hybrid score is {hybrid_score} *************\n\n")
 
                # --------------------------------------------------
                # Relationship metadata
                # --------------------------------------------------

                graph_relevance = float(candidate.get("graph_relevance", 0.0,) or 0.0)

                relationship_confidence = float(candidate.get("relationship_confidence", 0.0,) or 0.0)

                relationship_depth = int(candidate.get("relationship_depth", 0,) or 0)

                retrieval_source = (candidate.get("retrieval_source", "search",) or "search")

                # --------------------------------------------------
                # Calculate relationship score
                # --------------------------------------------------

                if relationship_depth == 1:

                    relationship_score = graph_relevance

                elif relationship_depth == 2:

                    relationship_score = 0.75 * graph_relevance

                elif relationship_depth > 2:

                    relationship_score = ((1.0 / relationship_depth) * graph_relevance)
 
                else:

                    relationship_score = 0.0

                # Apply relationship confidence
                relationship_score *= (
                    relationship_confidence
                    if relationship_depth > 0
                    else 0.0
                )

                print(f"************ The relationship score is {relationship_score} ************\n\n")
                # --------------------------------------------------
                # Determine final score
                # --------------------------------------------------

                #if (retrieval_source == "relationship"):
                if retrieval_source in ("relationship", "relationship_query"):

                    final_score = 0.80 * score + 0.05 * hybrid_score + 0.15 * relationship_score

                else:

                    final_score = 0.75 * score + 0.25 * hybrid_score

                score = final_score
                print(f"************ The FINAL SCORE is {score} ************\n\n")


                score = max(
                    0.0,
                    min(
                        1.0,
                        score,
                    ),
                )

                result = dict(
                    candidates[index]
                )

                result[
                    "rerank_score"
                ] = score

                reranked.append(
                    result
                )

            # ------------------------------------------------------
            # Sort by actual LLM relevance score
            # ------------------------------------------------------

            reranked.sort(
                key=lambda item: item.get(
                    "rerank_score",
                    0.0,
                ),
                reverse=True,
            )

            # ------------------------------------------------------
            # Relevance threshold
            #
            # top_k is a maximum, not a requirement.
            # ------------------------------------------------------

            selected = [
                result
                for result in reranked
                if result.get(
                    "rerank_score",
                    0.0,
                ) >= min_score
            ]

            selected = selected[
                :top_k
            ]

            print(f"********* LLM Reranker has selected -> {selected} *********\n\n")

            return selected

        except Exception as exc:

            print(
                "LLM reranking failed:",
                type(exc).__name__,
                str(exc),
            )

            return []

    # Ranking of dense and keyword search results
    def _rrf_fuse(
        self,
        dense_results: list[dict],
        keyword_results: list[dict],
        top_k: int,
        rrf_k: int = 60,
    ) -> list[dict]:

        scores = {}
        documents = {}

        # ----------------------------------------------------------
        # Dense ranking
        # ----------------------------------------------------------

        for rank, result in enumerate(
            dense_results,
            start=1,
        ):

            chunk_id = (
                result.get(
                    "chunk_id"
                )
                or result.get(
                    "id"
                )
                or ""
            )

            if not chunk_id:
                continue

            scores.setdefault(
                chunk_id,
                0.0,
            )

            scores[chunk_id] += (
                1.0
                / (
                    rrf_k + rank
                )
            )

            if chunk_id not in documents:
                documents[chunk_id] = dict(
                    result
                )

        # ----------------------------------------------------------
        # Keyword ranking
        # ----------------------------------------------------------

        for rank, result in enumerate(
            keyword_results,
            start=1,
        ):

            chunk_id = (
                result.get(
                    "chunk_id"
                )
                or result.get(
                    "id"
                )
                or ""
            )

            if not chunk_id:
                continue

            scores.setdefault(
                chunk_id,
                0.0,
            )

            scores[chunk_id] += (
                1.0
                / (
                    rrf_k + rank
                )
            )

            # If this chunk was only found by lexical search,
            # retain its complete lexical result.
            if chunk_id not in documents:
                documents[chunk_id] = dict(
                    result
                )


        # ----------------------------------------------------------
        # Sort
        # ----------------------------------------------------------

        ranked = sorted(
            documents.values(),
            key=lambda result: scores.get(
                result.get(
                    "chunk_id"
                ),
                0.0,
            ),
            reverse=True,
        )

        # ==========================================================
        # Build final RRF results
        # ==========================================================

        fused_results = []

        for rank, result in enumerate(
            ranked[:top_k],
            start=1,
        ):

            result = dict(
                result
            )

            chunk_id = result.get(
                "chunk_id"
            )

            result["rrf_score"] = scores.get(
                chunk_id,
                0.0,
            )

            result["hybrid_rank"] = rank

            fused_results.append(
                result
            )

        return fused_results


    # ==============================================================
    # Helpers
    # ==============================================================

    def _tokenize_query(
        self,
        query: str,
    ) -> list[str]:
        return [
            token.lower()
            for token in re.findall(
                r"\b[a-zA-Z0-9_-]+\b",
                query,
            )
            if token
        ]


    def _keyword_score(
        self,
        query: str,
        result: dict,
    ) -> float:
        """
        Score lexical relevance.

        Title/source metadata gets more weight than the full text.
        """

        query_terms = set(
            self._tokenize_query(query)
        )

        if not query_terms:
            return 0.0

        title = str(
            result.get(
                "title",
                "",
            )
            or ""
        ).lower()

        text = str(
            result.get(
                "text",
                "",
            )
            or ""
        ).lower()

        # ----------------------------------------------------------
        # Title scoring
        # ----------------------------------------------------------

        title_terms = set(
            self._tokenize_query(title)
        )

        title_matches = len(
            query_terms.intersection(
                title_terms
            )
        )

        title_score = (
            title_matches / len(query_terms)
        )

        # ----------------------------------------------------------
        # Full-text scoring
        # ----------------------------------------------------------

        text_terms = set(
            self._tokenize_query(text)
        )

        text_matches = len(
            query_terms.intersection(
                text_terms
            )
        )

        text_score = (
            text_matches / len(query_terms)
        )

        # ----------------------------------------------------------
        # Exact phrase bonus
        # ----------------------------------------------------------

        normalized_query = (
            query.strip().lower()
        )

        phrase_score = 0.0

        if (
            normalized_query
            and normalized_query in title
        ):
            phrase_score = 1.0

        elif (
            normalized_query
            and normalized_query in text
        ):
            phrase_score = 0.5

        # ----------------------------------------------------------
        # Weighted score
        # ----------------------------------------------------------

        return (
            0.60 * title_score
            + 0.30 * text_score
            + 0.10 * phrase_score
        )


    # Rank Hybrid results
    def _rank_hybrid_results(
        self,
        query: str,
        results: list[dict],
    ) -> list[dict]:
        """
        Rank Hybrid results
        """

        if not results:
            return []

        ranked = []

        for result in results:

            result = dict(result)

            rrf_score = result.get(
                "rrf_score",
                0.0,
            )

            keyword_score = self._keyword_score(
                query,
                result,
            )

            # RRF is the primary ranking signal.
            final_score = (
                0.80 * rrf_score
                + 0.20 * keyword_score
            )

            result["keyword_score"] = keyword_score
            result["final_score"] = final_score

            ranked.append(result)

        ranked.sort(
            key=lambda item: item.get(
                "final_score",
                0.0,
            ),
            reverse=True,
        )

        return ranked

    @staticmethod
    def _normalize_top_k(
        top_k: int,
    ) -> int:
        try:
            top_k = int(top_k)
        except (TypeError, ValueError):
            top_k = 8

        return min(
            max(top_k, 1),
            20,
        )

    @staticmethod
    def _build_where_clause(
        source_type: Optional[str] = None,
        servicenow_table: Optional[str] = None,
        servicenow_number: Optional[str] = None,
    ) -> Optional[dict]:
        conditions = []

        if source_type:
            conditions.append(
                {
                    "source_type": source_type
                }
            )

        if servicenow_table:
            conditions.append(
                {
                    "servicenow_table":
                        servicenow_table
                }
            )

        if servicenow_number:
            conditions.append(
                {
                    "servicenow_number":
                        servicenow_number
                }
            )

        if not conditions:
            return None

        if len(conditions) == 1:
            return conditions[0]

        return {
            "$and": conditions
        }

    @staticmethod
    def _format_vector_results(
        result: dict,
    ) -> list[dict]:
        """
        Convert Chroma's response format into the
        normalized search result format returned by
        the API layer.
        """

        documents = result.get(
            "documents",
            [[]],
        )

        metadatas = result.get(
            "metadatas",
            [[]],
        )

        distances = result.get(
            "distances",
            [[]],
        )

        docs = (
            documents[0]
            if documents
            else []
        )

        metas = (
            metadatas[0]
            if metadatas
            else []
        )

        dists = (
            distances[0]
            if distances
            else []
        )

        matches = []

        for index, doc in enumerate(
            docs
        ):
            meta = (
                metas[index]
                if index < len(metas)
                else {}
            )

            distance = (
                dists[index]
                if index < len(dists)
                else None
            )

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

                    "file_name": meta.get(
                        "file_name",
                        "",
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

                    "updated_at": meta.get(
                        "updated_at",
                        "",
                    ),

                    "distance": distance,

                    # ServiceNow metadata
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

                    "servicenow_updated_on": meta.get(
                        "servicenow_updated_on",
                        "",
                    ),
                }
            )

        return matches

    @staticmethod
    def _create_chunk_id(
        record_id: str,
        index: int,
        chunk_text: str,
    ) -> str:
        """
        Create a deterministic chunk ID.

        This is preferable to uuid4 because re-indexing
        the same unchanged record produces the same IDs.
        """

        import hashlib

        value = (
            f"{record_id}|"
            f"{index}|"
            f"{text_hash(chunk_text)}"
        )

        return hashlib.sha256(
            value.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _populate_servicenow_metadata(
        record: KnowledgeRecord,
        chunks: list[KnowledgeChunk],
    ):
        """
        Populate ServiceNow metadata when the record belongs
        to ServiceNow.

        Expected:
            record.external_id = sys_id
            record.title       = INC/PRB/CHG number
            record.locator     = servicenow/<table>/<sys_id>
        """

        if record.source_type != "servicenow":
            return

        table = ""

        if record.locator:
            parts = record.locator.strip(
                "/"
            ).split("/")

            if len(parts) >= 2:
                # Example:
                # servicenow/incident/sysid
                table = parts[1]

        for chunk in chunks:
            chunk.servicenow_table = table

            chunk.servicenow_sys_id = (
                record.external_id
                or ""
            )

            chunk.servicenow_number = (
                record.title
                or ""
            )
