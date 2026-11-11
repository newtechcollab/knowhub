from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Optional


# ==========================================================
# Cross-system identifier patterns
# ==========================================================

SERVICENOW_ID_PATTERN = re.compile(
    r"\b(?:INC|PRB|CHG|REQ|RITM)\d+\b",
    re.IGNORECASE,
)

JIRA_ID_PATTERN = re.compile(
    r"\b[A-Z][A-Z0-9_]+-\d+\b"
)

# ================================================================
# Identifier patterns
# ================================================================

INCIDENT_PATTERN = re.compile(
    r"\bINC\d+\b",
    re.IGNORECASE,
)

PROBLEM_PATTERN = re.compile(
    r"\bPRB\d+\b",
    re.IGNORECASE,
)

CHANGE_PATTERN = re.compile(
    r"\bCHG\d+\b",
    re.IGNORECASE,
)

# Jira-style issue keys, for example:
# ABC-123
JIRA_PATTERN = re.compile(
    r"\b[A-Z][A-Z0-9]{1,9}-\d+\b",
    re.IGNORECASE,
)

# JIRA Inverse Relationships
JIRA_INVERSE_RELATIONSHIPS = {
    "DUPLICATES": "IS_DUPLICATED_BY",
    "IS_DUPLICATED_BY": "DUPLICATES",
    "BLOCKS": "IS_BLOCKED_BY",
    "IS_BLOCKED_BY": "BLOCKS",
    "RELATES_TO": "RELATES_TO",
    "CLONES": "IS_CLONED_BY",
    "IS_CLONED_BY": "CLONES",
}

# GitHub references such as:
# #123
GITHUB_ISSUE_PATTERN = re.compile(
    r"(?<![\w-])#(\d+)\b"
)

# ==========================================================
# Semantic phrase patterns
#
# Order matters: more specific phrases should come first.
# ==========================================================

SERVICENOW_RELATION_PATTERNS = [

    (
        re.compile(
            r"\b(?:escalated|escalate|"
            r"escalation)\s+(?:to|into)\b",
            re.IGNORECASE,
        ),
        "ESCALATED_TO",
    ),

    (
        re.compile(
            r"\b(?:resolved|fixed|"
            r"addressed)\s+(?:by|through)\b",
            re.IGNORECASE,
        ),
        "RESOLVED_BY",
    ),

    (
        re.compile(
            r"\b(?:caused|caused\s+by|"
            r"root\s+cause\s+is)\b",
            re.IGNORECASE,
        ),
        "CAUSED_BY",
    ),

    (
        re.compile(
            r"\b(?:linked|link|"
            r"associated|associated\s+with)\b",
            re.IGNORECASE,
        ),
        "LINKED_TO",
    ),

    (
        re.compile(
            r"\b(?:related|"
            r"related\s+to)\b",
            re.IGNORECASE,
        ),
        "RELATED_TO",
    ),

    (
        re.compile(
            r"\b(?:tracked|"
            r"tracked\s+under)\b",
            re.IGNORECASE,
        ),
        "TRACKED_BY",
    ),

    (
        re.compile(
            r"\b(?:references|"
            r"reference|"
            r"refer\s+to)\b",
            re.IGNORECASE,
        ),
        "REFERENCES",
    ),
]

JIRA_RELATION_PATTERNS = [

    (
        re.compile(
            r"\b(?:fixed|"
            r"resolved|"
            r"addressed)\s+(?:by|in)\b",
            re.IGNORECASE,
        ),
        "RESOLVED_BY",
    ),

    (
        re.compile(
            r"\b(?:implemented|"
            r"implemented\s+in)\b",
            re.IGNORECASE,
        ),
        "IMPLEMENTED_IN",
    ),

    (
        re.compile(
            r"\b(?:tracked|"
            r"tracked\s+under)\b",
            re.IGNORECASE,
        ),
        "TRACKED_BY",
    ),

    (
        re.compile(
            r"\b(?:escalated|"
            r"escalated\s+to)\b",
            re.IGNORECASE,
        ),
        "ESCALATED_TO",
    ),

    (
        re.compile(
            r"\b(?:linked|"
            r"linked\s+to|"
            r"associated\s+with)\b",
            re.IGNORECASE,
        ),
        "LINKED_TO",
    ),

    (
        re.compile(
            r"\b(?:related|"
            r"related\s+to)\b",
            re.IGNORECASE,
        ),
        "RELATED_TO",
    ),

    (
        re.compile(
            r"\b(?:references|"
            r"reference|"
            r"refer\s+to)\b",
            re.IGNORECASE,
        ),
        "REFERENCES",
    ),
]

class RelationshipService:
    """
    Stores and manages relationships between KnowledgeRecords.

    Current storage:
        JSON file

    Later:
        Can be replaced with Neo4j or another graph database
        without changing the rest of the application significantly.
    """

    def __init__(
        self,
        storage_file: str,
        record_identity_service,
        pending_storage_file="data/pending_relationships.json",
    ):
        self.storage_file = storage_file
        self.record_identity_service = (
            record_identity_service
        )
        self.pending_storage_file = (
            pending_storage_file
        )

        os.makedirs(
            os.path.dirname(
                self.storage_file
            ) or ".",
            exist_ok=True,
        )

    # ============================================================
    # Storage
    # ============================================================

    def _load(self) -> list[dict]:
        if not os.path.exists(
            self.storage_file
        ):
            return []

        try:
            with open(
                self.storage_file,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            if isinstance(data, list):
                return data

            return []

        except (
            OSError,
            json.JSONDecodeError,
        ) as exc:
            print(
                "Failed to load knowledge relationships:",
                type(exc).__name__,
                str(exc),
            )

            return []

    def _save(
        self,
        relationships: list[dict],
    ):
        temp_file = (
            f"{self.storage_file}.tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                relationships,
                f,
                indent=2,
                ensure_ascii=False,
            )

        os.replace(
            temp_file,
            self.storage_file,
        )

    # Load Pending Relationships
    def _load_pending(self) -> list[dict]:

        if not os.path.exists(
            self.pending_storage_file
        ):
            return []

        with open(
            self.pending_storage_file,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        return (
            data
            if isinstance(data, list)
            else []
        )


    # Save Pending Relationships
    def _save_pending(
        self,
        data: list[dict],
    ):
        temp_file = (
            f"{self.pending_storage_file}.tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                indent=2,
                ensure_ascii=False,
            )

        os.replace(
            temp_file,
            self.pending_storage_file,
        )

    # Remove all relationships for a given record
    def remove_relationships_for_record(
        self,
        record_id: str,
        source: str | None = None,
    ) -> dict:
        """
        Remove all relationships associated with a record.

        If source is supplied, only relationships created from that
        source are removed.

        Example:
            source="jira-issue-link"

        Returns:
            {
                "relationships_removed": 2,
                "pending_removed": 1,
            }
        """

        if not record_id:
            raise ValueError(
                "record_id is required."
            )

        # ==========================================================
        # Remove resolved relationships
        # ==========================================================

        relationships = self._load()

        remaining_relationships = []

        removed_relationships = 0

        for relationship in relationships:

            matches_record = (
                relationship.get(
                    "source_record_id"
                )
                == record_id
                or relationship.get(
                    "target_record_id"
                )
                == record_id
            )

            if not matches_record:

                remaining_relationships.append(
                    relationship
                )

                continue

            if source:

                if (
                    relationship.get(
                        "source"
                    )
                    != source
                ):

                    remaining_relationships.append(
                        relationship
                    )

                    continue

            removed_relationships += 1

            print(
                "Removing relationship:",
                relationship,
            )

        self._save(
            remaining_relationships
        )

        # ==========================================================
        # Remove pending relationships
        # ==========================================================

        pending_removed = 0

        try:

            pending = self._load_pending()

            remaining_pending = []

            for relationship in pending:

                matches_source_record = (
                    relationship.get(
                        "source_record_id"
                    )
                    == record_id
                )

                if not matches_source_record:

                    remaining_pending.append(
                        relationship
                    )

                    continue

                if source:

                    if (
                        relationship.get(
                            "source"
                        )
                        != source
                    ):

                        remaining_pending.append(
                            relationship
                        )

                        continue

                pending_removed += 1

                print(
                    "Removing pending relationship:",
                    relationship,
                )

            self._save_pending(
                remaining_pending
            )

        except Exception as exc:

            # Pending relationship storage is optional.
            print(
                "Failed removing pending relationships:",
                type(exc).__name__,
                str(exc),
            )

        print(
            f"Removed {removed_relationships} "
            f"relationships and "
            f"{pending_removed} pending "
            f"relationships for {record_id}"
        )

        return {
            "relationships_removed": (
                removed_relationships
            ),
            "pending_removed": (
                pending_removed
            ),
        }

    # ============================================================
    # Add relationship
    # ============================================================

    def add(
        self,
        source_record_id: str,
        target_record_id: str,
        relation_type: str,
        confidence: float = 1.0,
        source: str = "deterministic",
    ) -> dict:
        """
        Add one relationship.

        Duplicate relationships are ignored.
        """

        if not source_record_id:
            raise ValueError(
                "source_record_id is required"
            )

        if not target_record_id:
            raise ValueError(
                "target_record_id is required"
            )

        if not relation_type:
            raise ValueError(
                "relation_type is required"
            )

        source_record_id = (
            source_record_id.strip()
        )

        target_record_id = (
            target_record_id.strip()
        )

        relation_type = (
            relation_type.strip().upper()
        )

        confidence = min(
            max(float(confidence), 0.0),
            1.0,
        )

        relationships = self._load()

        # --------------------------------------------------------
        # Prevent duplicates
        # --------------------------------------------------------

        for relationship in relationships:
            if (
                relationship.get(
                    "source_record_id"
                )
                == source_record_id
                and relationship.get(
                    "target_record_id"
                )
                == target_record_id
                and relationship.get(
                    "relation_type"
                )
                == relation_type
            ):
                return relationship

        # ----------------------------------------------------------
        # Jira inverse relationship
        # ----------------------------------------------------------

        inverse_type = (
            JIRA_INVERSE_RELATIONSHIPS.get(
                relation_type
            )
        )

        if inverse_type:

            for relationship in relationships:

                if (
                    relationship.get(
                        "source_record_id"
                    )
                    == target_record_id
                    and relationship.get(
                        "target_record_id"
                    )
                    == source_record_id
                    and relationship.get(
                        "relation_type"
                    )
                    == inverse_type
                ):
                    # Same logical Jira relationship already exists.
                    return relationship

        relationship = {
            "relation_id": str(
                uuid.uuid4()
            ),
            "source_record_id": source_record_id,
            "target_record_id": target_record_id,
            "relation_type": relation_type,
            "confidence": confidence,
            "source": source,
            "created_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        relationships.append(
            relationship
        )

        self._save(
            relationships
        )

        return relationship


    # Create Pending Relationship
    def add_pending_relationship(
        self,
        source_record_id: str,
        target_source_type: str,
        target_external_id: str,
        relation_type: str,
        confidence: float = 1.0,
        source: str = "",
    ) -> dict:
        """
        Store a relationship whose target record has not been
        registered in RecordIdentityService yet.

        Returns the pending relationship entry.
        """

        if not source_record_id:
            raise ValueError(
                "source_record_id is required"
            )

        if not target_source_type:
            raise ValueError(
                "target_source_type is required"
            )

        if not target_external_id:
            raise ValueError(
                "target_external_id is required"
            )

        if not relation_type:
            raise ValueError(
                "relation_type is required"
            )

        target_source_type = (
            target_source_type
            or ""
        ).strip().lower()

        target_external_id = (
            target_external_id
            or ""
        ).strip().upper()

        relation_type = (
            relation_type
            or "RELATED_TO"
        ).strip().upper()

        pending = self._load_pending()

        # ----------------------------------------------------------
        # Check whether the same pending relationship already exists
        # ----------------------------------------------------------

        for item in pending:

            if (
                item.get(
                    "source_record_id"
                )
                == source_record_id
                and item.get(
                    "target_source_type"
                )
                == target_source_type
                and (
                    item.get(
                        "target_external_id"
                    )
                    or ""
                ).strip().upper()
                == target_external_id
                and (
                    item.get(
                        "relation_type"
                    )
                    or ""
                ).strip().upper()
                == relation_type
            ):
                print(
                    "Pending relationship already exists:",
                    item,
                )

                return item

        # ----------------------------------------------------------
        # Create pending relationship
        # ----------------------------------------------------------

        pending_relationship = {
            "pending_id": str(
                uuid.uuid4()
            ),
            "source_record_id": (
                source_record_id
            ),
            "target_source_type": (
                target_source_type
            ),
            "target_external_id": (
                target_external_id
            ),
            "relation_type": (
                relation_type
            ),
            "confidence": confidence,
            "source": source,
            "created_at": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
        }

        pending.append(
            pending_relationship
        )

        self._save_pending(
            pending
        )

        print(
            "********* Pending relationship saved *********"
        )

        print(
            pending_relationship
        )

        return pending_relationship

    # Resolve Pending Relationship
    def _add_resolved(
        self,
        source_record_id: str,
        target_record_id: str,
        relation_type: str,
        confidence: float,
        source: str,
    ):
        """
        Create a resolved relationship between two canonical record IDs.
        """

        relationships = self._load()

        # Don't create duplicates
        for relationship in relationships:

            if (
                relationship["source_record_id"] == source_record_id
                and relationship["target_record_id"] == target_record_id
                and relationship["relation_type"] == relation_type
            ):
                return relationship

        relationship = {
            "relation_id": str(uuid.uuid4()),
            "source_record_id": source_record_id,
            "target_record_id": target_record_id,
            "relation_type": relation_type,
            "confidence": confidence,
            "source": source,
            "created_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        relationships.append(relationship)

        self._save(relationships)

        return relationship

    # Resolve a pending relationship record
    def resolve_pending_for_record(
        self,
        record,
    ):

        print(f"Record received under Pending Relationship -> {record}")
        source_type = (
            getattr(
                record,
                "source_type",
                "",
            )
            or ""
        ).strip()

        external_id = (
            getattr(
                record,
                "external_id",
                "",
            )
            or ""
        ).strip()

        record_id = (
            getattr(
                record,
                "record_id",
                "",
            )
            or ""
        ).strip()

        if not source_type or not external_id:
            return 0

        pending = self._load_pending()

        if not pending:
            return 0

        remaining = []
        resolved_count = 0

        for item in pending:

            target_source_type = (
                item.get(
                    "target_source_type",
                    "",
                )
                or ""
            ).strip()

            target_external_id = (
                item.get(
                    "target_external_id",
                    "",
                )
                or ""
            ).strip()

            if (
                target_source_type.lower()
                != source_type.lower()
                or target_external_id.upper()
                != external_id.upper()
            ):
                remaining.append(
                    item
                )
                continue

            relationship = (
                self._add_resolved(
                    source_record_id=(
                        item[
                            "source_record_id"
                        ]
                    ),
                    target_record_id=record_id,
                    relation_type=(
                        item[
                            "relation_type"
                        ]
                    ),
                    confidence=item.get(
                        "confidence",
                        1.0,
                    ),
                    source=item.get(
                        "source",
                        "",
                    ),
                )
            )

            if relationship:
                resolved_count += 1

        self._save_pending(
            remaining
        )

        if resolved_count:
            print(
                f"Resolved {resolved_count} "
                f"pending relationships for "
                f"{source_type}:{external_id}"
            )

        print(f"*********** Returning Resolved count -> {resolved_count}")
        return resolved_count

    # Get related records for a given source record id and target record id
    def find_related_records(
        self,
        start_record_id: str,
        target_entity_type: str,
        entity_type_lookup,
        max_depth: int = 4,
        allowed_relationship_types: list[str] | None = None,
    ) -> list[dict]:
        """
        Starting from start_record_id, traverse the relationship graph
        and return records whose entity_type matches target_entity_type.

        The graph is traversed in both directions.

        Returns:

            [
                {
                    "record_id": "...",
                    "entity_type": "opportunity",
                    "depth": 1,
                    "path": [
                        {
                            "from_record_id": "...",
                            "to_record_id": "...",
                            "relation_type": "...",
                            "confidence": 1.0,
                        }
                    ],
                }
            ]
        """

        if not start_record_id:
            return []

        if not target_entity_type:
            return []

        relationships = self._load()

        visited = {
            start_record_id
        }

        # Queue entries:
        #
        # (
        #     current_record_id,
        #     path
        # )
        #
        queue = [
            (
                start_record_id,
                [],
            )
        ]

        matches = []

        while queue:

            current_record_id, path = (
                queue.pop(0)
            )

            current_depth = len(path)

            # Do not go beyond max_depth.
            if current_depth >= max_depth:
                continue

            for relationship in relationships:

                source_record_id = (
                    relationship.get(
                        "source_record_id"
                    )
                )

                target_record_id = (
                    relationship.get(
                        "target_record_id"
                    )
                )

                relation_type = (
                    relationship.get(
                        "relation_type"
                    )
                )

                confidence = (
                    relationship.get(
                        "confidence",
                        1.0,
                    )
                    or 1.0
                )

                # --------------------------------------------------
                # Determine neighboring record.
                #
                # Treat relationship graph as bidirectional for
                # traversal.
                # --------------------------------------------------

                if (
                    source_record_id
                    == current_record_id
                ):

                    next_record_id = (
                        target_record_id
                )

                elif (
                    target_record_id
                    == current_record_id
                ):

                    next_record_id = (
                        source_record_id
                    )

                else:
                    continue

                if not next_record_id:
                    continue

                # --------------------------------------------------
                # Prevent loops.
                # --------------------------------------------------

                if next_record_id in visited:
                    continue

                visited.add(
                    next_record_id
                )

                # --------------------------------------------------
                # Build graph path.
                # --------------------------------------------------

                next_path = (
                    path
                    + [
                        {
                            "from_record_id": (
                                current_record_id
                            ),
                            "to_record_id": (
                                next_record_id
                            ),
                            "relation_type": (
                                relation_type
                            ),
                            "confidence": (
                                confidence
                            ),
                        }
                    ]
                )

                # --------------------------------------------------
                # Look up canonical entity type from Chroma.
                # --------------------------------------------------

                try:

                    entity_type = (
                        entity_type_lookup(
                            next_record_id
                        )
                    )

                except Exception as exc:

                    print(
                        "Entity type lookup failed for "
                        f"{next_record_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )

                    entity_type = None

                # --------------------------------------------------
                # Target found.
                # --------------------------------------------------

                if (
                    entity_type
                    == target_entity_type
                ):

                    matches.append(
                        {
                            "record_id": (
                                next_record_id
                            ),
                            "entity_type": (
                                entity_type
                            ),
                            "depth": len(
                                next_path
                            ),
                            "path": (
                                next_path
                            ),
                        }
                    )

                # --------------------------------------------------
                # Continue traversal.
                # --------------------------------------------------

                queue.append(
                    (
                        next_record_id,
                        next_path,
                    )
                )

        return matches

    # Get related record ids
    def get_related_record_ids(
        self,
        record_id: str,
    ) -> list[str]:
        """
        Return record_ids directly related to the supplied record.
        """

        relationships = self._load()

        related = set()

        for relationship in relationships:

            source = relationship.get("source_record_id")

            target = relationship.get("target_record_id")

            if source == record_id:

                if target:
                    related.add(target)

            elif target == record_id:

                if source:
                    related.add(source)

        return list(related)

    # Get the relationship details between source and target records
    def get_relationship_details(self, source_record_id: str, target_record_id: str,) -> dict | None:

        relationships = self._load()

        for relationship in relationships:

            source = relationship.get(
                "source_record_id"
            )

            target = relationship.get(
                "target_record_id"
            )

            if (
                source == source_record_id
                and target == target_record_id
            ):
                return {
                    "relation_type": relationship.get(
                        "relation_type"
                    ),
                    "confidence": relationship.get(
                        "confidence",
                        1.0,
                    ),
                    "direction": "outgoing",
                }

            if (
                source == target_record_id
                and target == source_record_id
            ):
                return {
                    "relation_type": relationship.get(
                        "relation_type"
                    ),
                    "confidence": relationship.get(
                        "confidence",
                        1.0,
                    ),
                    "direction": "incoming",
                }

        return None

    # Traverse all the related records
    def traverse_related_records(self, start_record_id: str, max_depth: int = 2,) -> dict[int, list[dict]]:
        """
        Traverse the relationship graph up to max_depth.

        Returns:

        {
            1: [...],
            2: [...],
        }
        """

        results = {}

        visited = {
            start_record_id
        }

        frontier = [
            start_record_id
        ]

        for depth in range(
            1,
            max_depth + 1,
        ):

            next_frontier = []

            for current_record_id in frontier:

                related_ids = (
                    self.get_related_record_ids(
                        current_record_id
                    )
                )

                for related_id in related_ids:

                    if related_id in visited:
                        continue

                    relationship = (
                        self.get_relationship_details(
                            current_record_id,
                            related_id,
                        )
                    )

                    visited.add(
                        related_id
                    )

                    next_frontier.append(
                    {
                        "record_id": related_id,
                        "from_record_id": (
                            current_record_id
                        ),
                        "relationship_type": (
                            relationship.get(
                                "relation_type"
                            )
                            if relationship
                            else None
                        ),
                        "relationship_confidence": (
                            relationship.get(
                                "confidence",
                                1.0,
                            )
                            if relationship
                            else 1.0
                        ),
                    }
                )

            results[depth] = (
                next_frontier
            )

            frontier = [
                item["record_id"]
                for item in next_frontier
            ]

            if not frontier:
                break

        return results

    # ============================================================
    # Index relationships for a record
    # ============================================================

    def index_record(
        self,
        record,
        record_identity_service=None,
    ) -> list[dict]:
        """
        Extract deterministic relationships from a KnowledgeRecord.

        The current implementation focuses on explicit identifiers
        in the record content.

        ServiceNow:
            INC -> PRB / CHG
            PRB -> INC / CHG
            CHG -> INC / PRB

        Jira:
            Jira issue keys

        GitHub:
            #123 style references

        Relationships are only created when the referenced external
        identifier can be resolved through RecordIdentityService.
        """

        print("************** Creating Relationships *************")
        if record is None:
            return []

        content = (
            getattr(
                record,
                "content",
                "",
            )
            or ""
        )

        source_type = (
            getattr(
                record,
                "source_type",
                "",
            )
            or ""
        ).lower()

        print(f"********** SourceType is {source_type} **********")
        source_record_id = (
            getattr(
                record,
                "record_id",
                "",
            )
            or ""
        )

        if not content or not source_record_id:
            return []

        extracted = []

        # ========================================================
        # ServiceNow
        # ========================================================

        if source_type == "servicenow":
            extracted.extend(
                self._extract_servicenow_relationships(
                    record=record,
                    content=content,
                )
            )

        # ========================================================
        # GitHub
        # ========================================================

        elif source_type == "github":
            extracted.extend(
                self._extract_github_relationships(
                    record=record,
                    content=content,
                )
            )

        # ========================================================
        # Jira
        # ========================================================

        elif source_type == "jira":
            extracted.extend(
                self._extract_jira_relationships(
                    record=record,
                    content=content,
                )
            )

        # ========================================================
        # Confluence
        # ========================================================

        elif source_type in (
            "confluence",
            "confluence-page",
            "confluence-space",
        ):
            extracted.extend(
                self._extract_confluence_relationships(
                    record=record,
                    content=content,
                )
            )

        # ========================================================
        # Salesforce
        # ========================================================

        elif source_type == "salesforce":
            extracted.extend(
                self._extract_salesforce_relationships(
                    record=record,
                    content=content,
                )
            )

        created = []

        for item in extracted:
            external_type = item[
                "external_type"
            ]

            external_id = item[
                "external_id"
            ]

            relation_type = item[
                "relation_type"
            ]

            # ----------------------------------------------------
            # Resolve external ID to canonical record ID
            # ----------------------------------------------------

            target_record_id = None

            if record_identity_service:
                try:
                    target_record_id = (
                        record_identity_service.lookup(
                            source_type=external_type,
                            external_id=external_id,
                        )
                    )
                except Exception as exc:
                    print(
                        "Record identity lookup failed:",
                        external_type,
                        external_id,
                        type(exc).__name__,
                        str(exc),
                    )

            if not target_record_id:
                print(f"Saving Target record {external_id} in Pending Relationship store")
                self.add_pending_relationship(
                    source_record_id=record.record_id,
                    target_source_type=external_type,
                    target_external_id=external_id,
                    relation_type=relation_type,
                    confidence=item.get("confidence",1.0,),
                    source=item.get("source","deterministic",),
                )
                continue

            # Don't create self-references.
            if (
                target_record_id
                == source_record_id
            ):
                continue

            relationship = self.add(
                source_record_id=source_record_id,
                target_record_id=target_record_id,
                relation_type=relation_type,
                confidence=item.get(
                    "confidence",
                    1.0,
                ),
                source=item.get(
                    "source",
                    "deterministic",
                ),
            )

            created.append(
                relationship
            )

        return created

    # ==========================================================
    # Main Salesforce extractor
    # ==========================================================

    def _extract_salesforce_relationships(
        self,
        record,
        content: str,
    ) -> list[dict]:

        relationships = []

        content = (
            content
            or ""
        )

        # ------------------------------------------------------
        # Salesforce-native relationships
        # ------------------------------------------------------

        relationships.extend(
            self._extract_salesforce_native_relationships(
                record,
                content
            )
        )

        # ------------------------------------------------------
        # Cross-system ServiceNow relationships
        # ------------------------------------------------------

        relationships.extend(
            self._extract_cross_system_relationships(
                content=content,
                source_type="servicenow",
            )
        )

        # ------------------------------------------------------
        # Cross-system Jira relationships
        # ------------------------------------------------------

        relationships.extend(
            self._extract_cross_system_relationships(
                content=content,
                source_type="jira",
            )
        )

        return self._deduplicate_relationships(
            relationships
        )

    # ==========================================================
    # Salesforce-native relationships
    # ==========================================================
    def _extract_salesforce_native_relationships(self, record, content: str,) -> list[dict]:
        
        relationships = []

        metadata = (
            getattr(
                record,
                "metadata",
                {},
            )
            or {}
        )

        salesforce_object = (
            metadata.get(
                "salesforce_object",
                "",
            )
            or ""
        )

        # ----------------------------------------------------------
        # Salesforce Opportunity -> Account
        # ----------------------------------------------------------

        if salesforce_object == "Opportunity":

            account_id = (
                metadata.get(
                    "salesforce_account_id",
                    "",
                )
                or ""
            ).strip()

            if account_id:

                relationships.append(
                    {
                        "external_type": "salesforce",
                        "external_id": account_id,
                        "relation_type": (
                            "BELONGS_TO_ACCOUNT"
                        ),
                        "confidence": 1.0,
                        "source": (
                            "salesforce-native"
                        ),
                    }
                )

        elif salesforce_object == "OpportunityLineItem":

            # ------------------------------------------------------
            # OpportunityLineItem -> Opportunity
            # ------------------------------------------------------

            opportunity_id = (
                metadata.get(
                    "salesforce_opportunity_id",
                    "",
                )
                or ""
            ).strip()

            if opportunity_id:

                relationships.append(
                    {
                        "external_type": "salesforce",

                        "external_id": opportunity_id,

                        "relation_type": (
                            "BELONGS_TO_OPPORTUNITY"
                        ),

                        "confidence": 1.0,

                        "source": (
                            "salesforce-native"
                        ),
                    }
                )

            # ------------------------------------------------------
            # OpportunityLineItem -> Product
            # ------------------------------------------------------

            product_id = (
                metadata.get(
                    "salesforce_product_id",
                    "",
                )
                or ""
            ).strip()

            if product_id:

                relationships.append(
                    {
                        "external_type": "salesforce",

                        "external_id": product_id,

                        "relation_type": (
                            "FOR_PRODUCT"
                        ),

                        "confidence": 1.0,

                        "source": (
                            "salesforce-native"
                        ),
                    }
                )

        # ----------------------------------------------------------
        # Future Salesforce relationships
        # ----------------------------------------------------------

        elif salesforce_object == "Contact":

            account_id = (
                metadata.get(
                    "salesforce_account_id",
                    "",
                )
                or ""
            ).strip()

            if account_id:

                relationships.append(
                    {
                        "external_type": "salesforce",
                        "external_id": account_id,
                        "relation_type": (
                            "BELONGS_TO_ACCOUNT"
                        ),
                        "confidence": 1.0,
                        "source": (
                            "salesforce-native"
                        ),
                    }
                )

        return relationships

    # ==========================================================
    # Cross-system relationship extraction
    # ==========================================================

    def _extract_cross_system_relationships(
        self,
        content: str,
        source_type: str,
    ) -> list[dict]:

        relationships = []

        if source_type == "servicenow":

            matches = list(
                SERVICENOW_ID_PATTERN.finditer(
                    content
                )
            )

            relation_patterns = (
                SERVICENOW_RELATION_PATTERNS
            )

        elif source_type == "jira":

            matches = list(
                JIRA_ID_PATTERN.finditer(
                    content
                )
            )

            relation_patterns = (
                JIRA_RELATION_PATTERNS
            )

        else:

            return []

        for match in matches:

            external_id = (
                match.group(0)
                .strip()
                .upper()
            )

            context = (
                self._get_relationship_context(
                    content=content,
                    match=match,
                )
            )

            relation_type, confidence = (
                self._determine_relation_type(
                    context=context,
                    patterns=relation_patterns,
                )
            )

            relationships.append(
                {
                    "external_type": source_type,
                    "external_id": external_id,
                    "relation_type": relation_type,
                    "confidence": confidence,
                    "source": (
                        "salesforce-text"
                    ),
                    "context": context,
                }
            )

        return relationships

    # ==========================================================
    # Extract surrounding context
    # ==========================================================

    @staticmethod
    def _get_relationship_context(
        content: str,
        match,
        window: int = 100,
    ) -> str:

        start = max(
            0,
            match.start() - window,
        )

        end = min(
            len(content),
            match.end() + window,
        )

        context = content[
            start:end
        ]

        # Normalize whitespace.
        context = re.sub(
            r"\s+",
            " ",
            context,
        ).strip()

        return context

    # ==========================================================
    # Determine semantic relationship
    # ==========================================================

    @staticmethod
    def _determine_relation_type(
        context: str,
        patterns,
    ) -> tuple[str, float]:

        # ------------------------------------------------------
        # Strong semantic evidence
        # ------------------------------------------------------

        for pattern, relation_type in patterns:

            if pattern.search(
                context
            ):

                return (
                    relation_type,
                    1.0,
                )

        # ------------------------------------------------------
        # Identifier found but no semantic phrase.
        #
        # Don't pretend we know the business relationship.
        # ------------------------------------------------------

        return (
            "REFERENCES",
            0.85,
        )

    # ==========================================================
    # Remove duplicate relationships
    # ==========================================================

    @staticmethod
    def _deduplicate_relationships(
        relationships: list[dict],
    ) -> list[dict]:

        result = []

        seen = set()

        for relationship in relationships:

            key = (
                relationship.get(
                    "external_type"
                ),
                relationship.get(
                    "external_id"
                ),
                relationship.get(
                    "relation_type"
                ),
            )

            if key in seen:
                continue

            seen.add(
                key
            )

            result.append(
                relationship
            )

        return result

    # ============================================================
    # ServiceNow relationships
    # ============================================================

    def _extract_servicenow_relationships(
        self,
        record,
        content: str,
    ) -> list[dict]:
        """
        Extract relationships from a ServiceNow record.

        Detects:
        - ServiceNow Incident / Problem / Change references
        - Jira issue references such as TS-123
        """

        table = (
            getattr(
                record,
                "servicenow_table",
                None,
            )
            or ""
        ).lower()

        results = []

        incidents = {
            value.upper()
            for value in INCIDENT_PATTERN.findall(
                content
            )
        }

        problems = {
            value.upper()
            for value in PROBLEM_PATTERN.findall(
                content
            )
        }

        changes = {
            value.upper()
            for value in CHANGE_PATTERN.findall(
                content
            )
        }

        if table == "incident":

            for number in problems:
                results.append(
                    {
                        "external_type": "servicenow:problem",
                        "external_id": number,
                        "relation_type": "RELATED_TO",
                        "confidence": 1.0,
                        "source": "servicenow",
                    }
                )

            for number in changes:
                results.append(
                    {
                        "external_type": (
                            "servicenow:change_request"
                        ),
                        "external_id": number,
                        "relation_type": "RELATED_TO",
                        "confidence": 1.0,
                        "source": "servicenow",
                    }
                )

        elif table == "problem":

            for number in incidents:
                results.append(
                    {
                        "external_type": "servicenow:incident",
                        "external_id": number,
                        "relation_type": "RELATED_TO",
                        "confidence": 1.0,
                        "source": "servicenow",
                    }
                )

            for number in changes:
                results.append(
                    {
                        "external_type": (
                            "servicenow:change_request"
                        ),
                        "external_id": number,
                        "relation_type": "RELATED_TO",
                        "confidence": 1.0,
                        "source": "servicenow",
                    }
                )

        elif table == "change_request":

            for number in incidents:
                results.append(
                    {
                        "external_type": "servicenow:incident",
                        "external_id": number,
                        "relation_type": "RELATED_TO",
                        "confidence": 1.0,
                        "source": "servicenow",
                    }
                )

            for number in problems:
                results.append(
                    {
                        "external_type": "servicenow:problem",
                        "external_id": number,
                        "relation_type": "RELATED_TO",
                        "confidence": 1.0,
                        "source": "servicenow",
                    }
                )

        # ==========================================================
        # Jira references
        # ==========================================================

        for jira_key in set(JIRA_PATTERN.findall(content or "")):
            target_external_id = (jira_key or "").strip().upper()

            if not target_external_id:
                continue

            results.append(
                {
                    "external_type": "jira",
                    "external_id": target_external_id,
                    "relation_type": "REFERENCES",
                    "confidence": 1.0,
                    "source": "servicenow-text",
                }
            )

        return results

    # ============================================================
    # GitHub relationships
    # ============================================================

    def _extract_github_relationships(
        self,
        record,
        content: str,
    ) -> list[dict]:
        """
        Extract basic GitHub relationships.

        Currently detects #123 references.

        Example:

            Fixes #481

        becomes:

            GitHub record -> github:issue:#481
        """

        results = []

        for issue_number in set(
            GITHUB_ISSUE_PATTERN.findall(
                content
            )
        ):
            results.append(
                {
                    "external_type": "github:issue",
                    "external_id": (
                        f"#{issue_number}"
                    ),
                    "relation_type": "REFERENCES",
                    "confidence": 1.0,
                    "source": "github",
                }
            )

        return results

    # ============================================================
    # Jira relationships
    # ============================================================


    def _extract_jira_relationships(
    self,
    record,
    content: str,
    ) -> list[dict]:
        """
        Extract Jira relationships from:

        1. Native Jira issue links stored in
           record.metadata["jira_issue_links"]

        2. Jira issue references found in content.

        Native Jira issue links are authoritative.
        Text references are treated as REFERENCES.
        """

        results = []
        seen = set()

        current_external_id = (
            getattr(
                record,
                "external_id",
                "",
            )
            or ""
        ).strip().upper()

        metadata = (
            getattr(
                record,
                "metadata",
                {}
            )
            or {}
        )

        # ==========================================================
        # 1. Native Jira issue links
        # ==========================================================

        jira_issue_links = (
            metadata.get(
                "jira_issue_links",
                [],
            )
            or []
        )

        for link in jira_issue_links:

            link_type = (
                link.get(
                    "type",
                    {}
                )
                or {}
            )

            outward_relation = (
                link_type.get(
                    "outward",
                    "",
                )
                or ""
            ).strip()

            inward_relation = (
                link_type.get(
                    "inward",
                    "",
                )
                or ""
            ).strip()

            # ------------------------------------------------------
            # Outward issue
            # ------------------------------------------------------

            outward_issue = link.get(
                "outwardIssue"
            )

            if outward_issue:

                target_external_id = (
                    outward_issue.get(
                        "key",
                        "",
                    )
                    or ""
                ).strip().upper()

                if (
                    target_external_id
                    and target_external_id
                    != current_external_id
                ):

                    relation_type = (
                        outward_relation
                        or "RELATED TO"
                    ).strip().upper().replace(
                        " ",
                        "_",
                    )

                    relationship_key = (
                        target_external_id,
                        relation_type,
                    )

                    if relationship_key not in seen:

                        seen.add(
                            relationship_key
                        )

                        results.append(
                            {
                                "external_type": "jira",
                                "external_id": (
                                    target_external_id
                                ),
                                "relation_type": (
                                    relation_type
                                ),
                                "confidence": 1.0,
                                "source": (
                                    "jira-issue-link"
                                ),
                            }
                        )

            # ------------------------------------------------------
            # Inward issue
            # ------------------------------------------------------

            inward_issue = link.get(
                "inwardIssue"
            )

            if inward_issue:

                target_external_id = (
                    inward_issue.get(
                        "key",
                        "",
                    )
                    or ""
                ).strip().upper()

                if (
                    target_external_id
                    and target_external_id
                    != current_external_id
                ):

                    relation_type = (
                        inward_relation
                        or "RELATED TO"
                    ).strip().upper().replace(
                        " ",
                        "_",
                    )

                    relationship_key = (
                        target_external_id,
                        relation_type,
                    )

                    if relationship_key not in seen:

                        seen.add(
                            relationship_key
                        )

                        results.append(
                            {
                                "external_type": "jira",
                                "external_id": (
                                    target_external_id
                                ),
                                "relation_type": (
                                    relation_type
                                ),
                                "confidence": 1.0,
                                "source": (
                                    "jira-issue-link"
                                ),
                            }
                        )

        # ==========================================================
        # 2. Jira issue references in text
        # ==========================================================

        jira_keys = set(
            JIRA_PATTERN.findall(
                content or ""
            )
        )

        for jira_key in jira_keys:

            target_external_id = (
                jira_key
                or ""
            ).strip().upper()

            if not target_external_id:
                continue

            if (
                target_external_id
                == current_external_id
            ):
                continue

            relationship_key = (
                target_external_id,
                "REFERENCES",
            )

            if relationship_key in seen:
                continue

            seen.add(
                relationship_key
            )

            results.append(
                {
                    "external_type": "jira",
                    "external_id": (
                        target_external_id
                    ),
                    "relation_type": "REFERENCES",
                    "confidence": 0.8,
                    "source": "jira-text",
                }
            )

        return results

    # ============================================================
    # Confluence relationships
    # ============================================================

    def _extract_confluence_relationships(
        self,
        record,
        content: str,
    ) -> list[dict]:
        """
        Initial Confluence relationship extraction.

        At this stage, we don't infer semantic relationships from
        free text. The main deterministic relationship will normally
        be created from source metadata, such as page -> parent page
        or page -> space.

        This method is intentionally conservative.
        """

        return []

    # ============================================================
    # Get relationships
    # ============================================================

    def get_related(
        self,
        record_id: str,
    ) -> list[dict]:
        relationships = self._load()

        return [
            relationship
            for relationship in relationships
            if (
                relationship.get(
                    "source_record_id"
                )
                == record_id
                or relationship.get(
                    "target_record_id"
                )
                == record_id
            )
        ]

    # ============================================================
    # Get outgoing
    # ============================================================

    def get_outgoing(
        self,
        record_id: str,
    ) -> list[dict]:
        relationships = self._load()

        return [
            relationship
            for relationship in relationships
            if relationship.get(
                "source_record_id"
            ) == record_id
        ]

    # ============================================================
    # Get incoming
    # ============================================================

    def get_incoming(
        self,
        record_id: str,
    ) -> list[dict]:
        relationships = self._load()

        return [
            relationship
            for relationship in relationships
            if relationship.get(
                "target_record_id"
            ) == record_id
        ]

    # ============================================================
    # Delete relationships for record
    # ============================================================

    def delete_for_record(
        self,
        record_id: str,
    ) -> int:
        """
        Remove all relationships where record_id appears on
        either side.
        """

        relationships = self._load()

        original_count = len(
            relationships
        )

        relationships = [
            relationship
            for relationship in relationships
            if (
                relationship.get(
                    "source_record_id"
                )
                != record_id
                and relationship.get(
                    "target_record_id"
                )
                != record_id
            )
        ]

        self._save(
            relationships
        )

        return (
            original_count
            - len(relationships)
        )

    # ============================================================
    # Reset
    # ============================================================

    def reset(self) -> int:
        """
        Remove all relationships.
        """

        relationships = self._load()

        count = len(
            relationships
        )

        self._save([])

        return count

    # ============================================================
    # Count
    # ============================================================

    def count(self) -> int:
        return len(
            self._load()
        )

