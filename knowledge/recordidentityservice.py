from __future__ import annotations

import json
import os
import threading
from typing import Any, Optional


class RecordIdentityService:

    """
    Maintains external enterprise identities for KnowledgeRecords.

    Single persisted mapping:

        identity_key -> metadata

    Example:

        {
            "servicenow:incident:INC0010002": {
                "record_id":
                    "servicenow:incident:62e67faf...",
                "source_type":
                    "servicenow:incident",
                "external_id":
                    "INC0010002",
                "title":
                    "Authentication failure",
                "source_name":
                    "ServiceNow Incidents"
            }
        }

    The service is intentionally independent of Chroma.
    """

    def __init__(
        self,
        storage_file: str,
    ):
        self.storage_file = storage_file

        self._lock = threading.RLock()

        os.makedirs(
            os.path.dirname(
                self.storage_file
            ) or ".",
            exist_ok=True,
        )

    # ============================================================
    # Internal storage
    # ============================================================

    def _load(self) -> dict[str, str]:
        with self._lock:
            if not os.path.exists(
                self.storage_file
            ):
                return {}

            try:
                with open(
                    self.storage_file,
                    "r",
                    encoding="utf-8",
                ) as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    return {}

                return data

            except (
                OSError,
                json.JSONDecodeError,
            ) as exc:
                print(
                    "Failed to load record identity index:",
                    type(exc).__name__,
                    str(exc),
                )

                return {}

    def _save(
        self,
        data: dict[str, str],
    ):
        with self._lock:
            temp_file = (
                f"{self.storage_file}.tmp"
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
                self.storage_file,
            )

    # ============================================================
    # Key normalization
    # ============================================================

    @staticmethod
    def _normalize_source_type(
        source_type: str,
    ) -> str:
        value = (
            source_type
            or ""
        ).strip().lower()

        # Preserve useful compound identifiers such as:
        # servicenow:incident
        # servicenow:problem
        # servicenow:change_request
        return value

    @staticmethod
    def _normalize_external_id(
        external_id: str,
    ) -> str:
        value = (
            external_id
            or ""
        ).strip()

        # Most enterprise identifiers should be
        # case-insensitive.
        return value.upper()

    @classmethod
    def _make_key(
        cls,
        source_type: str,
        external_id: str,
    ) -> str:
        source_type = (
            cls._normalize_source_type(
                source_type
            )
        )

        external_id = (
            cls._normalize_external_id(
                external_id
            )
        )

        if not source_type:
            raise ValueError(
                "source_type is required"
            )

        if not external_id:
            raise ValueError(
                "external_id is required"
            )

        return (
            f"{source_type}:"
            f"{external_id}"
        )

    # ============================================================
    # Register a KnowledgeRecord
    # ============================================================

    def register(
        self,
        record: Any,
    ) -> str:
        """
        Register the external identity represented by a
        KnowledgeRecord.

        Returns the canonical record_id.
        """

        if record is None:
            raise ValueError(
                "record is required"
            )

        record_id = (
            getattr(
                record,
                "record_id",
                "",
            )
            or ""
        ).strip()

        source_type = (
            getattr(
                record,
                "source_type",
                "",
            )
            or ""
        )

        external_id = (
            getattr(
                record,
                "external_id",
                "",
            )
            or ""
        )

        if not record_id:
            raise ValueError(
                "KnowledgeRecord.record_id is required"
            )

        if not source_type:
            raise ValueError(
                "KnowledgeRecord.source_type is required"
            )

        if not external_id:
            # Some records may not have an external ID.
            # In that case there is nothing to register.
            return record_id

        # --------------------------------------------------------
        # Determine source name/title
        # --------------------------------------------------------

        title = (
            getattr(
                record,
                "title",
                "",
            )
            or ""
        )

        source_name = (
            getattr(
                record,
                "source_name",
                "",
            )
            or ""
        )

        key = self._make_key(
            source_type,
            external_id,
        )

        data = self._load()

        existing = data.get(key)

        if existing and existing != record_id:
            print(
                "Record identity collision detected:",
                key,
                "existing=",
                existing,
                "new=",
                record_id,
            )

        data[key] = {
            "record_id": record_id,
            "source_name": source_name,
            "title": title,
        }
        self._save(data)

        # --------------------------------------------------------
        # Additional ServiceNow aliases
        #
        # Useful because ServiceNow has both:
        #   sys_id
        #   number (INC/PRB/CHG)
        # --------------------------------------------------------

        servicenow_table = (
            getattr(
                record,
                "servicenow_table",
                None,
            )
            or ""
        )

        servicenow_number = (
            getattr(
                record,
                "servicenow_number",
                None,
            )
            or ""
        )

        servicenow_sys_id = (
            getattr(
                record,
                "servicenow_sys_id",
                None,
            )
            or ""
        )

        if (
            source_type.lower()
            == "servicenow"
            and servicenow_table
        ):
            if servicenow_number:
                number_key = self._make_key(
                    (
                        f"servicenow:"
                        f"{servicenow_table}"
                    ),
                    servicenow_number,
                )

                data[number_key] = record_id

            if servicenow_sys_id:
                sys_id_key = self._make_key(
                    (
                        f"servicenow:"
                        f"{servicenow_table}"
                    ),
                    servicenow_sys_id,
                )

                data[sys_id_key] = record_id

            self._save(data)

        return record_id

    # Returns metadata for a given source type and external Id
    def get_metadata(self, source_type: str, external_id: str,) -> dict | None:

        key = self._make_key(
            source_type,
            external_id,
        )

        data = self._load()

        value = data.get(key)

        if not value:
            return None

        if isinstance(value, str):
            return {
                "record_id": value,
                "source_name": "",
                "title": "",
            }

        return value

    # ============================================================
    # Explicit registration
    # ============================================================

    def register_identity(
        self,
        source_type: str,
        external_id: str,
        record_id: str,
    ) -> str:
        """
        Register an explicit external_id -> record_id mapping.
        """

        if not record_id:
            raise ValueError(
                "record_id is required"
            )

        key = self._make_key(
            source_type,
            external_id,
        )

        data = self._load()

        data[key] = record_id

        self._save(data)

        return record_id

    # ============================================================
    # Lookup
    # ============================================================

    def lookup(
        self,
        source_type: str,
        external_id: str,
    ) -> Optional[str]:
        """
        Resolve an external identity to a canonical record_id.

        Example:

            lookup(
                "servicenow:incident",
                "INC0010002",
            )

        Returns:

            servicenow:incident:<sys_id>

        or None if not found.
        """

        key = self._make_key(
            source_type,
            external_id,
        )

        data = self._load()

        value = data.get(key)

        if not value:
            return None

        if isinstance(value, str):
            # Backward compatibility
            return value

        return value.get(
            "record_id"
        )

    # ============================================================
    # Lookup aliases
    # ============================================================

    def lookup_servicenow(
        self,
        table: str,
        number: str | None = None,
        sys_id: str | None = None,
    ) -> Optional[str]:
        """
        Convenience helper for ServiceNow.
        """

        if number:
            result = self.lookup(
                source_type=(
                    f"servicenow:{table}"
                ),
                external_id=number,
            )

            if result:
                return result

        if sys_id:
            result = self.lookup(
                source_type=(
                    f"servicenow:{table}"
                ),
                external_id=sys_id,
            )

            if result:
                return result

        return None

    # ============================================================
    # Check existence
    # ============================================================

    def exists(
        self,
        source_type: str,
        external_id: str,
    ) -> bool:
        return (
            self.lookup(
                source_type,
                external_id,
            )
            is not None
        )

    # ============================================================
    # Remove identity
    # ============================================================

    def delete(
        self,
        source_type: str,
        external_id: str,
    ) -> bool:
        """
        Remove one identity mapping.
        """

        key = self._make_key(
            source_type,
            external_id,
        )

        data = self._load()

        if key not in data:
            return False

        del data[key]

        self._save(data)

        return True

    # ============================================================
    # Remove all identities for a record
    # ============================================================

    def delete_record(
        self,
        record_id: str,
    ) -> int:
        """
        Remove every external identity pointing to record_id.
        """

        if not record_id:
            return 0

        data = self._load()

        keys_to_remove = [
            key
            for key, value in data.items()
            if value == record_id
        ]

        for key in keys_to_remove:
            del data[key]

        self._save(data)

        return len(
            keys_to_remove
        )

    # ============================================================
    # Reset
    # ============================================================

    def reset(self) -> int:
        """
        Remove the entire identity index.
        """

        data = self._load()

        count = len(data)

        self._save({})

        return count

    # ============================================================
    # Count
    # ============================================================

    def count(self) -> int:
        return len(
            self._load()
        )

    # ============================================================
    # Debug / inspection
    # ============================================================

    def all_identities(
        self,
    ) -> dict[str, str]:
        """
        Return a copy of the entire identity mapping.
        """

        return dict(
            self._load()
        )

