# data_sanitizer.py

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from typing import Any, Optional


# ============================================================
# Sanitization Result
# ============================================================

@dataclass
class SanitizationResult:
    """
    Result of sanitizing one piece of data.
    """

    value: Any

    masked: bool = False

    critical_secret_detected: bool = False

    counts: dict[str, int] = field(
        default_factory=dict
    )

    def merge_counts(
        self,
        other: dict[str, int],
    ) -> None:
        for key, value in other.items():
            self.counts[key] = (
                self.counts.get(key, 0)
                + value
            )


# ============================================================
# Sensitive Data Sanitizer
# ============================================================

class SensitiveDataSanitizer:
    """
    Central sanitization layer for the Enterprise Knowledge Hub.

    IMPORTANT:

    This sanitizer is intended to run BEFORE:

        - chunking
        - embedding
        - Chroma
        - lexical indexing
        - LLM calls

    The raw source system remains the source of truth.
    The Knowledge Hub should persist only the sanitized form.
    """

    # ========================================================
    # Generic high-confidence patterns
    # ========================================================

    PASSWORD_PATTERN = re.compile(
        r"(?i)"
        r"\b(password|passwd|pwd)\b"
        r"(\s*[:=]\s*)"
        r"([^\s,;]+)"
    )

    API_SECRET_PATTERN = re.compile(
        r"(?i)"
        r"\b("
        r"api[_-]?key|"
        r"apikey|"
        r"access[_-]?token|"
        r"refresh[_-]?token|"
        r"client[_-]?secret|"
        r"client[_-]?id|"
        r"secret[_-]?key|"
        r"secret"
        r")\b"
        r"(\s*[:=]\s*)"
        r"([^\s,;]+)"
    )

    BEARER_PATTERN = re.compile(
        r"(?i)"
        r"\bBearer\s+"
        r"[A-Za-z0-9._~+/=-]+"
    )

    PRIVATE_KEY_PATTERN = re.compile(
        r"-----BEGIN [^-]*PRIVATE KEY-----"
        r".*?"
        r"-----END [^-]*PRIVATE KEY-----",
        re.DOTALL,
    )

    SSH_PRIVATE_KEY_PATTERN = re.compile(
        r"-----BEGIN OPENSSH PRIVATE KEY-----"
        r".*?"
        r"-----END OPENSSH PRIVATE KEY-----",
        re.DOTALL,
    )

    # ========================================================
    # JWT
    # ========================================================

    JWT_PATTERN = re.compile(
        r"\b"
        r"eyJ[A-Za-z0-9_-]+"
        r"\."
        r"[A-Za-z0-9_-]+"
        r"\."
        r"[A-Za-z0-9_-]+"
        r"\b"
    )

    # ========================================================
    # Credit / debit card candidate
    #
    # Validation is performed with Luhn before masking.
    # ========================================================

    EXPLICIT_CARD_PATTERN = re.compile(
        r"(?i)\b("
        r"cc|"
        r"dc|"
        r"credit[_\s-]?card|"
        r"debit[_\s-]?card|"
        r"card[_\s-]?number|"
        r"card[_\s-]?no"
        r")\b"
        r"(\s*[:#=\-]?\s*)"
        r"(\d(?:[\s-]?\d){7,19})\b"
    )

    CARD_PATTERN = re.compile(
        r"\b(?:\d[ -]*?){13,19}\b"
    )

    # ========================================================
    # Email
    # ========================================================

    EMAIL_PATTERN = re.compile(
        r"\b"
        r"[A-Za-z0-9._%+-]+"
        r"@"
        r"[A-Za-z0-9.-]+"
        r"\."
        r"[A-Za-z]{2,}"
        r"\b"
    )

    # ========================================================
    # Phone
    # ========================================================

    PHONE_PATTERN = re.compile(
        r"(?i)\b("
        r"phone|"
        r"mobile|"
        r"telephone|"
        r"contact[_\s-]?number|"
        r"phone[_\s-]?number"
        r")\b"
        r"(\s*[:=]\s*)"
        r"(\+?\d[\d\s().-]{7,}\d)"
    )

    # ========================================================
    # Generic customer/account labels
    #
    # These deliberately require a nearby field label to reduce
    # false positives.
    # ========================================================

    CUSTOMER_ID_PATTERN = re.compile(
        r"(?i)"
        r"\b("
        r"customer[_\s-]?id|"
        r"customer[_\s-]?number|"
        r"customer[_\s-]?no"
        r")\b"
        r"(\s*[:=]\s*)"
        r"([A-Za-z0-9_-]+)"
    )

    ACCOUNT_NUMBER_PATTERN = re.compile(
        r"(?i)"
        r"\b("
        r"account[_\s-]?number|"
        r"account[_\s-]?no|"
        r"bank[_\s-]?account"
        r")\b"
        r"(\s*[:=]\s*)"
        r"([A-Za-z0-9_-]+)"
    )

    POLICY_NUMBER_PATTERN = re.compile(
        r"(?i)"
        r"\b("
        r"policy[_\s-]?number|"
        r"policy[_\s-]?no"
        r")\b"
        r"(\s*[:=]\s*)"
        r"([A-Za-z0-9_-]+)"
    )

    EMPLOYEE_ID_PATTERN = re.compile(
        r"(?i)"
        r"\b("
        r"employee[_\s-]?id|"
        r"employee[_\s-]?number|"
        r"employee[_\s-]?no"
        r")\b"
        r"(\s*[:=]\s*)"
        r"([A-Za-z0-9_-]+)"
    )

    # ========================================================
    # ServiceNow-specific patterns
    # ========================================================

    SERVICENOW_CUSTOMER_ID_PATTERN = re.compile(
        r"(?i)"
        r"\b"
        r"(customer[_\s-]?id|"
        r"consumer[_\s-]?id)"
        r"\b"
        r"(\s*[:=]\s*)"
        r"([A-Za-z0-9_-]+)"
    )

    # ========================================================
    # Jira-specific patterns
    #
    # These are deliberately NOT treated as sensitive.
    # Jira keys must remain available for identity/relationships.
    # ========================================================

    JIRA_KEY_PATTERN = re.compile(
        r"\b[A-Z][A-Z0-9_]+-\d+\b"
    )

    # ========================================================
    # ServiceNow identifiers
    #
    # These are also operational identifiers and should NOT be
    # masked by generic numeric rules.
    # ========================================================

    SERVICENOW_NUMBER_PATTERN = re.compile(
        r"\b(?:INC|PRB|CHG|REQ|RITM)\d+\b",
        re.IGNORECASE,
    )

    # ========================================================
    # Source policies
    # ========================================================

    DEFAULT_POLICY = {
        "mask_email": True,
        "mask_phone": True,
        "mask_card": True,
        "mask_credentials": True,
        "mask_customer_id": True,
        "mask_account_number": True,
        "mask_policy_number": True,
        "mask_employee_id": True,
        "pseudonymize_customer_id": False,
        "reject_critical_secrets": False,
    }

    SOURCE_POLICIES = {
        "servicenow": {
            **DEFAULT_POLICY,
        },
        "jira": {
            **DEFAULT_POLICY,
        },
        "confluence": {
            **DEFAULT_POLICY,
        },
        "sharepoint": {
            **DEFAULT_POLICY,
        },
        "github": {
            **DEFAULT_POLICY,
        },
        "file": {
            **DEFAULT_POLICY,
        },
        "website": {
            **DEFAULT_POLICY,
        },
    }

    # ========================================================
    # Initialization
    # ========================================================

    def __init__(
        self,
        hmac_secret: bytes | str | None = None,
        source_policies: Optional[
            dict[str, dict]
        ] = None,
    ):

        if hmac_secret is None:
            self.hmac_secret = None

        elif isinstance(
            hmac_secret,
            bytes,
        ):
            self.hmac_secret = hmac_secret

        else:
            self.hmac_secret = (
                hmac_secret.encode(
                    "utf-8"
                )
            )

        self.source_policies = {
            key: dict(value)
            for key, value
            in self.SOURCE_POLICIES.items()
        }

        if source_policies:

            for source_type, policy in (
                source_policies.items()
            ):
                self.source_policies[
                    source_type
                ] = {
                    **self.DEFAULT_POLICY,
                    **policy,
                }

    # ========================================================
    # Policy
    # ========================================================

    def get_policy(
        self,
        source_type: str | None,
    ) -> dict:

        source_type = (
            source_type or ""
        ).strip().lower()

        return dict(
            self.source_policies.get(
                source_type,
                self.DEFAULT_POLICY,
            )
        )

    # ========================================================
    # Text sanitization
    # ========================================================

    def sanitize_text(
        self,
        text: str,
        source_type: str | None = None,
    ) -> tuple[str, dict]:

        if not text:
            return "", {}

        policy = self.get_policy(
            source_type
        )

        original = text

        counts: dict[str, int] = {}

        # ----------------------------------------------------
        # Critical secrets
        # ----------------------------------------------------

        if policy.get(
            "mask_credentials",
            True,
        ):

            text, count = (
                self.PRIVATE_KEY_PATTERN.subn(
                    "[REDACTED:PRIVATE_KEY]",
                    text,
                )
            )

            if count:
                counts[
                    "private_key"
                ] = count

            text, count = (
                self.SSH_PRIVATE_KEY_PATTERN.subn(
                    "[REDACTED:PRIVATE_KEY]",
                    text,
                )
            )

            if count:
                counts[
                    "ssh_private_key"
                ] = count

            text, count = (
                self.BEARER_PATTERN.subn(
                    "Bearer [REDACTED:TOKEN]",
                    text,
                )
            )

            if count:
                counts[
                    "bearer_token"
                ] = count

            text, count = (
                self.JWT_PATTERN.subn(
                    "[REDACTED:JWT]",
                    text,
                )
            )

            if count:
                counts[
                    "jwt"
                ] = count

            text, count = (
                self.PASSWORD_PATTERN.subn(
                    r"\1=[REDACTED:PASSWORD]",
                    text,
                )
            )

            if count:
                counts[
                    "password"
                ] = count

            text, count = (
                self.API_SECRET_PATTERN.subn(
                    r"\1=[REDACTED:SECRET]",
                    text,
                )
            )

            if count:
                counts[
                    "secret"
                ] = count

        # ----------------------------------------------------
        # Payment card
        # ----------------------------------------------------

        if policy.get(
            "mask_card",
            True,
        ):

            text, count = (
                self.EXPLICIT_CARD_PATTERN.subn(
                    r"\1 [REDACTED:CARD]",
                    text,
                )
            )

            if count:
                counts["payment_card"] = (
                    counts.get(
                        "payment_card",
                        0,
                    )
                    + count
                )

            text, count = (
                self.CARD_PATTERN.subn(
                    self._mask_card_match,
                    text,
                )
            )

            if count:
                counts[
                    "payment_card"
                ] = count

        # ----------------------------------------------------
        # Email
        # ----------------------------------------------------

        if policy.get(
            "mask_email",
            True,
        ):

            text, count = (
                self.EMAIL_PATTERN.subn(
                    "[REDACTED:EMAIL]",
                    text,
                )
            )

            if count:
                counts[
                    "email"
                ] = count

        # ----------------------------------------------------
        # Phone
        # ----------------------------------------------------

        if policy.get(
            "mask_phone",
            True,
        ):

            text, count = (
                self.PHONE_PATTERN.subn(
                    "[REDACTED:PHONE]",
                    text,
                )
            )

            if count:
                counts[
                    "phone"
                ] = count

        # ----------------------------------------------------
        # Customer ID
        # ----------------------------------------------------

        if policy.get(
            "mask_customer_id",
            True,
        ):

            if policy.get(
                "pseudonymize_customer_id",
                False,
            ):

                text, count = (
                    self.CUSTOMER_ID_PATTERN.subn(
                        self._pseudonymize_customer_match,
                        text,
                    )
                )

            else:

                text, count = (
                    self.CUSTOMER_ID_PATTERN.subn(
                        r"\1=[REDACTED:CUSTOMER_ID]",
                        text,
                    )
                )

            if count:
                counts[
                    "customer_id"
                ] = count

        # ----------------------------------------------------
        # Account number
        # ----------------------------------------------------

        if policy.get(
            "mask_account_number",
            True,
        ):

            text, count = (
                self.ACCOUNT_NUMBER_PATTERN.subn(
                    r"\1=[REDACTED:ACCOUNT_NUMBER]",
                    text,
                )
            )

            if count:
                counts[
                    "account_number"
                ] = count

        # ----------------------------------------------------
        # Policy number
        # ----------------------------------------------------

        if policy.get(
            "mask_policy_number",
            True,
        ):

            text, count = (
                self.POLICY_NUMBER_PATTERN.subn(
                    r"\1=[REDACTED:POLICY_NUMBER]",
                    text,
                )
            )

            if count:
                counts[
                    "policy_number"
                ] = count

        # ----------------------------------------------------
        # Employee ID
        # ----------------------------------------------------

        if policy.get(
            "mask_employee_id",
            True,
        ):

            text, count = (
                self.EMPLOYEE_ID_PATTERN.subn(
                    r"\1=[REDACTED:EMPLOYEE_ID]",
                    text,
                )
            )

            if count:
                counts[
                    "employee_id"
                ] = count

        return text, counts

    # ========================================================
    # Card helper
    # ========================================================

    def _mask_card_match(
        self,
        match: re.Match,
    ) -> str:

        value = re.sub(
            r"[\s-]",
            "",
            match.group(0),
        )

        if not self._passes_luhn(
            value
        ):
            return match.group(0)

        return (
            "[REDACTED:CARD]"
        )

    @staticmethod
    def _passes_luhn(
        number: str,
    ) -> bool:

        if not number.isdigit():
            return False

        if not (
            13 <= len(number) <= 19
        ):
            return False

        total = 0

        reversed_digits = number[::-1]

        for index, digit in enumerate(
            reversed_digits
        ):

            value = int(digit)

            if index % 2 == 1:

                value *= 2

                if value > 9:
                    value -= 9

            total += value

        return (
            total % 10 == 0
        )

    # ========================================================
    # Customer ID pseudonymization
    # ========================================================

    def _pseudonymize_customer_match(
        self,
        match: re.Match,
    ) -> str:

        return (
            f"{match.group(1)}"
            f"{match.group(2)}"
            f"{self.pseudonymize(match.group(3))}"
        )

    def pseudonymize(
        self,
        value: str,
    ) -> str:

        if not self.hmac_secret:
            raise RuntimeError(
                "HMAC secret is required "
                "for pseudonymization."
            )

        digest = hmac.new(
            self.hmac_secret,
            value.encode(
                "utf-8"
            ),
            hashlib.sha256,
        ).hexdigest()

        return (
            f"CUSTOMER_"
            f"{digest[:12].upper()}"
        )

    # ========================================================
    # Recursive value sanitization
    # ========================================================

    def sanitize_value(
        self,
        value: Any,
        source_type: str | None = None,
    ) -> tuple[Any, dict]:

        # ----------------------------------------------------
        # String
        # ----------------------------------------------------

        if isinstance(
            value,
            str,
        ):

            sanitized, counts = (
                self.sanitize_text(
                    value,
                    source_type,
                )
            )

            return sanitized, counts

        # ----------------------------------------------------
        # Dictionary
        # ----------------------------------------------------

        if isinstance(
            value,
            dict,
        ):

            output = {}
            counts: dict[str, int] = {}

            for key, item in value.items():

                # Keep technical identity fields untouched.
                normalized_key = (
                    str(key).lower()
                )

                if self._is_protected_identity_field(
                    normalized_key
                ):
                    output[key] = item
                    continue

                sanitized_item, item_counts = (
                    self.sanitize_value(
                        item,
                        source_type,
                    )
                )

                output[key] = (
                    sanitized_item
                )

                self._merge_counts(
                    counts,
                    item_counts,
                )

            return output, counts

        # ----------------------------------------------------
        # List / tuple
        # ----------------------------------------------------

        if isinstance(
            value,
            (list, tuple),
        ):

            output = []
            counts: dict[str, int] = {}

            for item in value:

                sanitized_item, item_counts = (
                    self.sanitize_value(
                        item,
                        source_type,
                    )
                )

                output.append(
                    sanitized_item
                )

                self._merge_counts(
                    counts,
                    item_counts,
                )

            if isinstance(
                value,
                tuple,
            ):
                output = tuple(
                    output
                )

            return output, counts

        # ----------------------------------------------------
        # Everything else remains unchanged
        # ----------------------------------------------------

        return value, {}

    # ========================================================
    # KnowledgeRecord sanitization
    # ========================================================

    def sanitize_record(
        self,
        record,
    ) -> tuple[Any, dict]:

        if record is None:
            raise ValueError(
                "record is required."
            )

        source_type = (
            getattr(
                record,
                "source_type",
                "",
            )
            or ""
        ).strip().lower()

        total_counts: dict[str, int] = {}

        # ----------------------------------------------------
        # Title
        # ----------------------------------------------------

        if hasattr(
            record,
            "title",
        ):

            title = getattr(
                record,
                "title",
                None,
            )

            if isinstance(
                title,
                str,
            ):

                sanitized_title, counts = (
                    self.sanitize_text(
                        title,
                        source_type,
                    )
                )

                record.title = (
                    sanitized_title
                )

                self._merge_counts(
                    total_counts,
                    counts,
                )

        # ----------------------------------------------------
        # Content
        # ----------------------------------------------------

        content = getattr(
            record,
            "content",
            "",
        )

        if isinstance(
            content,
            str,
        ):

            sanitized_content, counts = (
                self.sanitize_text(
                    content,
                    source_type,
                )
            )

            record.content = (
                sanitized_content
            )

            self._merge_counts(
                total_counts,
                counts,
            )

        # ----------------------------------------------------
        # Locator
        #
        # Don't blindly sanitize URLs because they can contain
        # useful knowledge-source identifiers.
        #
        # Only sanitize if it is actually a string.
        # ----------------------------------------------------

        locator = getattr(
            record,
            "locator",
            None,
        )

        if isinstance(
            locator,
            str,
        ):

            sanitized_locator, counts = (
                self.sanitize_text(
                    locator,
                    source_type,
                )
            )

            record.locator = (
                sanitized_locator
            )

            self._merge_counts(
                total_counts,
                counts,
            )

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        metadata = getattr(
            record,
            "metadata",
            None,
        )

        if metadata is not None:

            sanitized_metadata, counts = (
                self.sanitize_value(
                    metadata,
                    source_type,
                )
            )

            record.metadata = (
                sanitized_metadata
            )

            self._merge_counts(
                total_counts,
                counts,
            )

        # ----------------------------------------------------
        # Determine if critical secrets were detected
        # ----------------------------------------------------

        critical_types = {
            "private_key",
            "ssh_private_key",
            "password",
            "secret",
            "bearer_token",
            "jwt",
        }

        critical_detected = any(
            key in critical_types
            for key in total_counts
        )

        return record, {
            "sanitized": bool(
                total_counts
            ),
            "critical_secret_detected": (
                critical_detected
            ),
            "counts": total_counts,
        }

    # ========================================================
    # Protected identity fields
    # ========================================================

    @staticmethod
    def _is_protected_identity_field(
        field_name: str,
    ) -> bool:

        protected = {
            "record_id",
            "source_id",
            "source_type",
            "external_id",
            "jira_issue_id",
            "jira_issue_key",
            "jira_project_key",
            "servicenow_sys_id",
            "servicenow_number",
            "chunk_id",
            "chunk_index",
        }

        return field_name in protected

    # ========================================================
    # Count utilities
    # ========================================================

    @staticmethod
    def _merge_counts(
        target: dict[str, int],
        source: dict[str, int],
    ) -> None:

        for key, value in source.items():

            target[key] = (
                target.get(key, 0)
                + value
            )

    # ========================================================
    # Safety inspection helper
    # ========================================================

    def contains_critical_secret(
        self,
        text: str,
        source_type: str | None = None,
    ) -> bool:

        if not text:
            return False

        _, counts = self.sanitize_text(
            text,
            source_type,
        )

        critical_types = {
            "private_key",
            "ssh_private_key",
            "password",
            "secret",
            "bearer_token",
            "jwt",
        }

        return any(
            key in critical_types
            for key in counts
        )
