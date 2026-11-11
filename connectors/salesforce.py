from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
import requests

from models.knowledge import KnowledgeRecord
from knowledge.chunks import text_hash


# ==================================================================
# Salesforce Connector - Connects and Retrieves data from Salesforce
# ==================================================================

# ----------------------------------------------------------
# Salesforce entities supported for ingestion and polling
# ----------------------------------------------------------

SALESFORCE_OBJECTS = (
    "Account",
    "Contact",
    "Opportunity",
    "OpportunityLineItem",
    "Product2",
)

SALESFORCE_POLL_FIELDS = {
    "Account": [
        "Id",
        "Name",
        "Type",
        "Industry",
        "Phone",
        "Website",
        "Description",
        "LastModifiedDate",
    ],

    "Contact": [
        "Id",
        "AccountId",
        "FirstName",
        "LastName",
        "Email",
        "Phone",
        "Title",
        "LastModifiedDate",
    ],

    "Opportunity": [
        "Id",
        "AccountId",
        "Name",
        "StageName",
        "Amount",
        "CloseDate",
        "Description",
        "LastModifiedDate",
    ],

    "OpportunityLineItem": [
        "Id",
        "OpportunityId",
        "PricebookEntryId",
        "Quantity",
        "UnitPrice",
        "TotalPrice",
        "LastModifiedDate",
    ],

    "Product2": [
        "Id",
        "Name",
        "ProductCode",
        "Description",
        "Family",
        "IsActive",
        "LastModifiedDate",
    ],
}

class SalesforceConnector:
    """

    Salesforce-specific integration for the Knowledge Hub.

    Required environment variables:

        SALESFORCE_LOGIN_URL
        SALESFORCE_CLIENT_ID
        SALESFORCE_USERNAME
        SALESFORCE_PRIVATE_KEY_PATH

    This Connector uses SSO to connect to Salesforce. Here are the steps you need to follow:

    1. Create a Private Key and Public Key using command below. Set the location/name of server.key as SALESFORCE_PRIVATE_KEY_PATH in system environment variable 
    openssl req -newkey rsa:2048 -nodes -keyout server.key -x509 -days 365 -out server.crt

    2. Create an External Client App in Salesforce, using OAuth Flow Type as JWT Bearer, uploading server.crt from step 1

    3. Select OAuth Scopes - Manage user data via APIs (api) and Perform requests at any time (refresh_token, offline_access)

    4. Set OAuth Policy as Admin approved users are pre-authorized

    5. Create a new User in Salesforce - this user will be used by this Connector to connect to Salesforce. Save this Username as SALESFORCE_USERNAME in system environment variable

    6. Create a new Permission Set and assign it to above User and also to above External Client App

    7. After External Client App is created, select the Consumer Key and save it as SALESFORCE_CLIENT_ID in system environment variable
   
    """

    ACCOUNT_FIELDS = [
        "Id",
        "Name",
        "Type",
        "Industry",
        "Description",
        "Website",
        "Phone",
        "BillingStreet",
        "BillingCity",
        "BillingState",
        "BillingPostalCode",
        "BillingCountry",
        "OwnerId",
        "CreatedDate",
        "LastModifiedDate",
    ]

    OPPORTUNITY_FIELDS = [
        "Id",
        "Name",
        "AccountId",
        "Amount",
        "StageName",
        "CloseDate",
        "Probability",
        "Type",
        "LeadSource",
        "Description",
        "OwnerId",
        "CreatedDate",
        "LastModifiedDate",
    ]

    OPPORTUNITY_LINE_ITEM_FIELDS = [
        "Id",
        "OpportunityId",
        "Quantity",
        "UnitPrice",
        "TotalPrice",
        "PricebookEntryId",
        "PricebookEntry.Product2Id",
        "PricebookEntry.Product2.Name",
        "PricebookEntry.Product2.ProductCode",
        "PricebookEntry.Product2.Family",
        "CreatedDate",
        "LastModifiedDate",
    ]

    PRODUCT_FIELDS = [
        "Id",
        "Name",
        "ProductCode",
        "Description",
        "Family",
        "IsActive",
        "CreatedDate",
        "LastModifiedDate",
    ]

    def __init__(
        self,
        knowledge_service,
        login_url: str | None = None,
        client_id: str | None = None,
        username: str | None = None,
        private_key_path: str | None = None,
        state_file: str = "./data/salesforce_sync_state.json",
        api_version: str | None = None,
        timeout: int = 60,
    ):
        """
        Initialize Salesforce connector.

        knowledge_service is your existing KnowledgeService.

        It is responsible for:
            sanitizer
            chunking
            embeddings
            Chroma
            lexical repository
            identity registration
        """

        self.knowledge_service = knowledge_service

        # Salesforce Login URL - by default, it uses https://login.salesforce.com
        self.login_url = (
            login_url
            or os.getenv(
                "SALESFORCE_LOGIN_URL",
                "https://login.salesforce.com",
            )
        ).rstrip("/")

        self.client_id = (
            client_id
            or os.getenv(
                "SALESFORCE_CLIENT_ID",
                "",
            )
        )

        self.username = (
            username
            or os.getenv(
                "SALESFORCE_USERNAME",
                "",
            )
        )

        self.private_key_path = (
            private_key_path
            or os.getenv(
                "SALESFORCE_PRIVATE_KEY_PATH",
                "",
            )
        )

        self.api_version = (
            api_version
            or os.getenv(
                "SALESFORCE_API_VERSION",
                "v66.0",
            )
        )

        self.state_file = state_file
        self.timeout = timeout

        self.session = requests.Session()

        self._access_token: str | None = None
        self._instance_url: str | None = None
        self._token_expires_at = 0.0

        self._state_lock = threading.Lock()

        self._validate_configuration()
        self._ensure_state_file()

    # ==========================================================
    # Configuration
    # ==========================================================

    def _validate_configuration(
        self,
    ) -> None:

        if not self.login_url:
            raise ValueError(
                "SALESFORCE_LOGIN_URL is required."
            )

        if not self.client_id:
            raise ValueError(
                "SALESFORCE_CLIENT_ID is required."
            )

        if not self.username:
            raise ValueError(
                "SALESFORCE_USERNAME is required."
            )

        if not self.private_key_path:
            raise ValueError(
                "SALESFORCE_PRIVATE_KEY_PATH is required."
            )

    # ==========================================================
    # Source identity
    # ==========================================================

    @staticmethod
    def get_source_id(
        notebook_id: str,
    ) -> str:
        """
        Stable Salesforce source ID.

        Example:
            salesforce:default-knowledge-base
        """

        return (
            f"salesforce:"
            f"{notebook_id}"
        )


    def _set_latest_watermark(
        self,
        source_id: str,
        object_name: str,
        records: list[dict],
    ) -> None:

        latest = self._latest_timestamp(
            records
        )

        if not latest:
            return

        self._set_watermark(
            source_id=source_id,
            object_name=object_name,
            timestamp=latest,
        )

    # ==========================================================
    # State file
    # ==========================================================

    def _ensure_state_file(
        self,
    ) -> None:

        path = Path(
            self.state_file
        )

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if not path.exists():

            path.write_text(
                "{}",
                encoding="utf-8",
            )

    def _load_state(
        self,
    ) -> dict:

        with self._state_lock:

            try:

                content = Path(
                    self.state_file
                ).read_text(
                    encoding="utf-8"
                )

                data = json.loads(
                    content
                )

                if isinstance(
                    data,
                    dict,
                ):
                    return data

                return {}

            except (
                FileNotFoundError,
                json.JSONDecodeError,
            ):
                return {}

    def _save_state(
        self,
        data: dict,
    ) -> None:

        with self._state_lock:

            path = Path(
                self.state_file
            )

            temp_path = Path(
                f"{self.state_file}.tmp"
            )

            temp_path.write_text(
                json.dumps(
                    data,
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            os.replace(
                temp_path,
                path,
            )

    def _get_watermark(
        self,
        source_id: str,
        object_name: str,
    ) -> str | None:

        state = self._load_state()

        return (
            state
            .get(
                source_id,
                {},
            )
            .get(
                object_name,
                {},
            )
            .get(
                "last_modified_date"
            )
        )

    def _set_watermark(
        self,
        source_id: str,
        object_name: str,
        timestamp: str,
    ) -> None:

        state = self._load_state()

        source_state = state.setdefault(
            source_id,
            {},
        )

        object_state = (
            source_state.setdefault(
                object_name,
                {},
            )
        )

        object_state[
            "last_modified_date"
        ] = timestamp

        self._save_state(
            state
        )

    # Load Last Sync State of Salesforce records
    def _load_poll_state(self) -> dict:
        path = Path(self.state_file)

        if not path.exists():
            return {}

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)


    def _save_poll_state(self, state: dict) -> None:
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)

        temp = path.with_suffix(".json.tmp")

        with temp.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        os.replace(temp, path)


    def _ensure_salesforce_session(self) -> tuple[str, str]:
        """
        Adapter for your existing JWT OAuth implementation.

        Replace self.authenticate() with the actual method
        in your SalesforceConnector.
        """

        if not getattr(self, "access_token", None):
            self.authenticate()

        instance_url = getattr(self, "instance_url", None)
        access_token = getattr(self, "access_token", None)

        if not instance_url or not access_token:
            raise RuntimeError(
                "Salesforce authentication did not return "
                "instance_url and access_token."
            )

        return instance_url.rstrip("/"), access_token


    def _query_salesforce(self, soql: str) -> list[dict]:
        """
        Execute a paginated Salesforce SOQL query.
        """

        instance_url, token = self._ensure_salesforce_session()

        url = (
            f"{instance_url}/services/data/"
            f"{SALESFORCE_API_VERSION}/query"
        )

        params = {"q": soql}
        records = []

        while url:
            response = requests.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=60,
            )

            # Reauthenticate once if the access token expired.
            if response.status_code == 401:
                self.authenticate()
                instance_url, token = self._ensure_salesforce_session()

                response = requests.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    params=params,
                    timeout=60,
                )

            response.raise_for_status()
            payload = response.json()

            records.extend(payload.get("records", []))

            next_url = payload.get("nextRecordsUrl")

            url = (
                f"{instance_url}{next_url}"
                if next_url
                else None
            )

            params = None

        return records


    # Returns list of records that have changed since the last poll
    def _fetch_changed_records(
        self,
        object_name: str,
        last_watermark: str,
    ) -> list[dict]:
        """
        Fetch changed Salesforce records in a bounded window.

        last_watermark is exclusive.
        """

        print(f"************** Object Name -> {object_name} and Last Watermark -> {last_watermark} *********\n\n")

        fields = SALESFORCE_POLL_FIELDS[object_name]

        soql = (
            f"SELECT {', '.join(fields)} "
            f"FROM {object_name} "
            f"WHERE LastModifiedDate > {last_watermark} "
            f"ORDER BY LastModifiedDate ASC, Id ASC"
        )

        print(f"************** Salesforce polling -> Query being run {soql} *********\n\n")
        return self.query(soql)

    # Poll Salesforce to look for new/modified records
    def poll_once(self) -> dict:
        """
        Incrementally ingest changed Salesforce records.

        Pass 1:
            Fetch and index changed records.

        Pass 2:
            Refresh relationships.

        Commit each entity checkpoint only after both passes
        complete successfully.
        """

        print("Salesforce Polling -> Loading previous poll state\n\n")
        state = self._load_poll_state()
        print(f"Salesforce Polling -> Previous poll state is {state}\n\n")

        salesforce_object_states = state.get("salesforce:default-knowledge-base")

        print(f"Salesforce Polling -> Previous poll object states is {salesforce_object_states}\n\n")

        results = {}
        total_indexed = 0

        source_id = self.get_source_id("default-knowledge-base")

        for object_name in SALESFORCE_OBJECTS:

            print(f"Salesforce Polling -> Loading last watermark for {object_name}\n\n")

            watermark_element = salesforce_object_states.get(object_name)

            if not watermark_element:
                continue

            last_watermark = watermark_element.get("last_modified_date")

            print(f"Salesforce Polling -> Last watermark for {object_name} is {last_watermark}\n\n")

            if not last_watermark:
                continue

            try:
                changed_records = self._fetch_changed_records(
                    object_name=object_name,
                    last_watermark=last_watermark,
                )

                print(f"Salesforce Polling -> Changed records for {object_name} is {changed_records}\n\n")

                indexed_records = []

                # ----------------------------------------------
                # PASS 1: Index all changed records
                # ----------------------------------------------

                for sf_record in changed_records:

                    # Adapter: reuse your existing conversion
                    # method from initial ingestion.
                    source_id = self.get_source_id("default-knowledge-base")

                    knowledge_record = self._build_record(object_name=object_name, item=sf_record, source_id=source_id,)

                    result = self.knowledge_service.index_record(
                        record=knowledge_record,
                        source_name=f"Salesforce - {object_name}",
                        index_relationships=False,
                    )

                    if result.get("status") not in {
                        "indexed",
                        "unchanged",
                        "skipped_unchanged",
                    }:
                        raise RuntimeError(
                            f"Indexing failed for {object_name} "
                            f"{sf_record.get('Id')}: {result}"
                        )

                    indexed_records.append(knowledge_record)

                # ----------------------------------------------
                # PASS 2: Refresh relationships
                # ----------------------------------------------

                for record in indexed_records:
                    self.knowledge_service.index_relationships(
                        record
                    )

                # ----------------------------------------------
                # Commit checkpoint
                # ----------------------------------------------

                # Find the latest LastModifiedDate from the
                # successfully processed Salesforce records.

                latest_modified = self._latest_timestamp(changed_records)

                if latest_modified:
                    self._set_watermark(source_id=source_id, object_name=object_name, timestamp=latest_modified,)

                results[object_name] = {
                    "status": "success",
                    "changed": len(changed_records),
                    "processed": len(indexed_records),
                }

                total_indexed += len(indexed_records)

            except Exception as exc:

                results[object_name] = {
                    "status": "failed",
                    "error": str(exc),
                }

                # Do not advance this entity's checkpoint.
                # Other entities can continue.
                continue

        return {
            "status": (
                "partial_failure"
                if any(
                    item["status"] == "failed"
                    for item in results.values()
                )
                else "success"
            ),
            "records_processed": total_indexed,
            "entities": results,
        }

    # ==========================================================
    # JWT authentication
    # ==========================================================

    def _load_private_key(
        self,
    ) -> str:

        path = Path(
            self.private_key_path
        )

        if not path.exists():

            raise FileNotFoundError(
                "Salesforce private key not found: "
                f"{path}"
            )

        return path.read_text(
            encoding="utf-8"
        )

    def _authenticate(
        self,
    ) -> None:
        """
        Authenticate using Salesforce JWT Bearer OAuth.

        Salesforce expects:

            iss = External Client App consumer key
            sub = Salesforce integration username
            aud = Salesforce login URL
            exp = short-lived expiration

        The private key stays on the Knowledge Hub server.
        Salesforce has the corresponding public certificate.
        """

        private_key = (
            self._load_private_key()
        )

        now = int(
            time.time()
        )

        payload = {
            "iss": self.client_id,
            "sub": self.username,
            "aud": self.login_url,
            "exp": now + 180,
        }

        assertion = jwt.encode(
            payload,
            private_key,
            algorithm="RS256",
        )

        token_url = (
            f"{self.login_url}"
            "/services/oauth2/token"
        )

        response = self.session.post(
            token_url,
            data={
                "grant_type": (
                    "urn:ietf:params:oauth:"
                    "grant-type:jwt-bearer"
                ),
                "assertion": assertion,
            },
            headers={
                "Accept": "application/json",
            },
            timeout=self.timeout,
        )

        if not response.ok:

            raise RuntimeError(
                "Salesforce JWT authentication failed: "
                f"{response.status_code} "
                f"{response.text}"
            )

        print("Status Code:", response.status_code)
        print("OAuth Response:", response.text)

        data = response.json()

        access_token = data.get("access_token")

        instance_url = data.get("instance_url")

        if not access_token:
            raise RuntimeError(
                "Salesforce OAuth response "
                "does not contain access_token."
            )

        if not instance_url:
            raise RuntimeError(
                "Salesforce OAuth response "
                "does not contain instance_url."
            )

        self._access_token = access_token

        self._instance_url = (
            instance_url.rstrip("/")
        )

        # Refresh conservatively.
        #
        # Salesforce session/token lifetime is governed by
        # Salesforce configuration, so don't hard-code the
        # actual lifetime.
        self._token_expires_at = (
            time.time() + 300
        )

        print("Salesforce instance URL:", self._instance_url,)

    def _ensure_authenticated(
        self,
    ) -> None:

        if (
            not self._access_token
            or time.time()
            >= self._token_expires_at
        ):
            self._authenticate()

    # ==========================================================
    # HTTP
    # ==========================================================

    def _auth_headers(
        self,
    ) -> dict[str, str]:

        self._ensure_authenticated()

        return {
            "Authorization": (
                f"Bearer {self._access_token}"
            ),
            "Accept": "application/json",
        }

    def _get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> requests.Response:

        response = self.session.get(
            url,
            headers=self._auth_headers(),
            params=params,
            timeout=self.timeout,
        )

        # Token expired/revoked.
        if response.status_code == 401:

            self._access_token = None

            response = self.session.get(
                url,
                headers=self._auth_headers(),
                params=params,
                timeout=self.timeout,
            )

        return response

    # ==========================================================
    # SOQL
    # ==========================================================

    def query(
        self,
        soql: str,
    ) -> list[dict]:
        """
        Execute SOQL and follow nextRecordsUrl until all
        records are retrieved.
        """

        if not soql:
            raise ValueError(
                "SOQL query is required."
            )

        self._ensure_authenticated()

        if not self._instance_url:
            raise RuntimeError(
                "Salesforce instance URL "
                "is not available."
            )

        url = (
            f"{self._instance_url}"
            f"/services/data/"
            f"{self.api_version}"
            "/query"
        )

        response = self._get(
            url,
            params={
                "q": soql,
            },
        )

        if not response.ok:

            raise RuntimeError(
                "Salesforce SOQL query failed: "
                f"{response.status_code} "
                f"{response.text}"
            )

        payload = response.json()

        records = list(
            payload.get(
                "records",
                [],
            )
        )

        next_url = payload.get(
            "nextRecordsUrl"
        )

        while next_url:

            if not next_url.startswith(
                "http"
            ):
                next_url = (
                    f"{self._instance_url}"
                    f"{next_url}"
                )

            response = self._get(
                next_url
            )

            if not response.ok:

                raise RuntimeError(
                    "Salesforce pagination failed: "
                    f"{response.status_code} "
                    f"{response.text}"
                )

            payload = response.json()

            records.extend(
                payload.get(
                    "records",
                    [],
                )
            )

            next_url = payload.get(
                "nextRecordsUrl"
            )

        return records

    # ==========================================================
    # SOQL helpers
    # ==========================================================

    @staticmethod
    def _fields(
        fields: list[str],
    ) -> str:

        return ", ".join(
            fields
        )

    def _watermark_clause(
        self,
        source_id: str,
        object_name: str,
    ) -> str:

        watermark = (
            self._get_watermark(
                source_id=source_id,
                object_name=object_name,
            )
        )

        if not watermark:
            return ""

        try:

            dt = datetime.fromisoformat(
                watermark.replace(
                    "Z",
                    "+00:00",
                )
            )

            if dt.tzinfo is None:

                dt = dt.replace(
                    tzinfo=timezone.utc
                )

            # One-second overlap.
            #
            # We deliberately use >= so that records sharing
            # the same LastModifiedDate second are not missed.
            dt -= timedelta(
                seconds=1
            )

            timestamp = (
                dt.astimezone(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%dT%H:%M:%S.000+0000"
                )
            )

        except Exception as exc:

            raise RuntimeError(
                "Invalid Salesforce watermark for "
                f"{object_name}: {watermark}"
            ) from exc

        return (
            f"WHERE LastModifiedDate >= "
            f"{timestamp}"
        )

    # ==========================================================
    # SOQL builders
    # ==========================================================

    def _build_account_query(
        self,
        source_id: str,
        full_sync: bool,
    ) -> str:

        where = ""

        if not full_sync:

            where = self._watermark_clause(
                source_id,
                "Account",
            )

        return (
            f"SELECT {self._fields(self.ACCOUNT_FIELDS)} "
            f"FROM Account "
            f"{where} "
            f"ORDER BY LastModifiedDate ASC"
        )

    def _build_opportunity_query(
        self,
        source_id: str,
        full_sync: bool,
    ) -> str:

        where = ""

        if not full_sync:

            where = self._watermark_clause(
                source_id,
                "Opportunity",
            )

        return (
            f"SELECT {self._fields(self.OPPORTUNITY_FIELDS)} "
            f"FROM Opportunity "
            f"{where} "
            f"ORDER BY LastModifiedDate ASC"
        )

    def _build_product_query(
        self,
        source_id: str,
        full_sync: bool,
    ) -> str:

        where = ""

        if not full_sync:

            where = self._watermark_clause(
                source_id,
                "Product2",
            )

        return (
            f"SELECT {self._fields(self.PRODUCT_FIELDS)} "
            f"FROM Product2 "
            f"{where} "
            f"ORDER BY LastModifiedDate ASC"
        )

    def _build_opportunity_line_item_query(
        self,
        source_id: str,
        full_sync: bool,
    ) -> str:

        where = ""

        if not full_sync:
            where = self._watermark_clause(
                source_id,
                "OpportunityLineItem",
            )

        return (
            f"SELECT {self._fields(self.OPPORTUNITY_LINE_ITEM_FIELDS)} "
            f"FROM OpportunityLineItem "
            f"{where} "
            f"ORDER BY LastModifiedDate ASC"
        )

    # ==========================================================
    # Common helpers
    # ==========================================================

    @staticmethod
    def _safe(
        value: Any,
    ) -> str:

        if value is None:
            return ""

        return str(
            value
        )

    @staticmethod
    def _latest_timestamp(
        records: list[dict],
    ) -> str | None:

        latest = None

        for record in records:

            timestamp = (
                record.get(
                    "LastModifiedDate"
                )
            )

            if not timestamp:
                continue

            if (
                latest is None
                or timestamp > latest
            ):
                latest = timestamp

        return latest

    # ==========================================================
    # Salesforce Account -> KnowledgeRecord
    # ==========================================================

    def _build_account_record(
        self,
        item: dict,
        source_id: str,
    ) -> KnowledgeRecord:

        salesforce_id = (
            item.get("Id") or ""
        )

        name = (
            item.get("Name") or ""
        )

        content = "\n".join(
            [
                "SALESFORCE ACCOUNT",
                "",
                f"Name: {self._safe(item.get('Name'))}",
                f"Type: {self._safe(item.get('Type'))}",
                f"Industry: {self._safe(item.get('Industry'))}",
                f"Description: {self._safe(item.get('Description'))}",
                f"Website: {self._safe(item.get('Website'))}",
                f"Phone: {self._safe(item.get('Phone'))}",
                f"Billing Street: {self._safe(item.get('BillingStreet'))}",
                f"Billing City: {self._safe(item.get('BillingCity'))}",
                f"Billing State: {self._safe(item.get('BillingState'))}",
                f"Billing Postal Code: {self._safe(item.get('BillingPostalCode'))}",
                f"Billing Country: {self._safe(item.get('BillingCountry'))}",
                f"Owner ID: {self._safe(item.get('OwnerId'))}",
                f"Created: {self._safe(item.get('CreatedDate'))}",
                f"Updated: {self._safe(item.get('LastModifiedDate'))}",
            ]
        )

        return KnowledgeRecord(
            record_id=(
                f"salesforce:"
                f"account:"
                f"{salesforce_id}"
            ),
            source_id=source_id,
            source_type="salesforce",
            entity_type="account",
            external_id=salesforce_id,
            title=f"Account: {name}",
            locator=(
                f"{self._instance_url}"
                f"/lightning/r/Account/"
                f"{salesforce_id}/view"
            ),
            content=content,
            content_hash=text_hash(
                content
            ),
            metadata={
                "salesforce_object": "Account",
                "entity_type": "account",
                "salesforce_id": salesforce_id,
                "salesforce_account_id": salesforce_id,
            },
        )

    # ==========================================================
    # Salesforce Opportunity -> KnowledgeRecord
    # ==========================================================

    def _build_opportunity_record(
        self,
        item: dict,
        source_id: str,
    ) -> KnowledgeRecord:

        salesforce_id = (
            item.get("Id") or ""
        )

        name = (
            item.get("Name") or ""
        )

        account_id = (
            item.get("AccountId")
        )

        content = "\n".join(
            [
                "SALESFORCE OPPORTUNITY",
                "",
                f"Name: {self._safe(item.get('Name'))}",
                f"Stage: {self._safe(item.get('StageName'))}",
                f"Amount: {self._safe(item.get('Amount'))}",
                f"Close Date: {self._safe(item.get('CloseDate'))}",
                f"Probability: {self._safe(item.get('Probability'))}",
                f"Type: {self._safe(item.get('Type'))}",
                f"Lead Source: {self._safe(item.get('LeadSource'))}",
                f"Description: {self._safe(item.get('Description'))}",
                f"Account ID: {self._safe(account_id)}",
                f"Owner ID: {self._safe(item.get('OwnerId'))}",
                f"Created: {self._safe(item.get('CreatedDate'))}",
                f"Updated: {self._safe(item.get('LastModifiedDate'))}",
            ]
        )

        return KnowledgeRecord(
            record_id=(
                f"salesforce:"
                f"opportunity:"
                f"{salesforce_id}"
            ),
            source_id=source_id,
            source_type="salesforce",
            entity_type="opportunity",
            external_id=salesforce_id,
            title=f"Opportunity: {name}",
            locator=(
                f"{self._instance_url}"
                f"/lightning/r/Opportunity/"
                f"{salesforce_id}/view"
            ),
            content=content,
            content_hash=text_hash(
                content
            ),
            metadata={
                "salesforce_object": "Opportunity",
                "entity_type": "opportunity",
                "salesforce_id": salesforce_id,
                "salesforce_account_id": account_id,
            },
        )

    # ==========================================================
    # Salesforce Product -> KnowledgeRecord
    # ==========================================================

    def _build_product_record(
        self,
        item: dict,
        source_id: str,
    ) -> KnowledgeRecord:

        salesforce_id = (
            item.get("Id") or ""
        )

        name = (
            item.get("Name") or ""
        )

        content = "\n".join(
            [
                "SALESFORCE PRODUCT",
                "",
                f"Name: {self._safe(item.get('Name'))}",
                f"Product Code: {self._safe(item.get('ProductCode'))}",
                f"Family: {self._safe(item.get('Family'))}",
                f"Description: {self._safe(item.get('Description'))}",
                f"Active: {self._safe(item.get('IsActive'))}",
                f"Created: {self._safe(item.get('CreatedDate'))}",
                f"Updated: {self._safe(item.get('LastModifiedDate'))}",
            ]
        )

        return KnowledgeRecord(
            record_id=(
                f"salesforce:"
                f"product:"
                f"{salesforce_id}"
            ),
            source_id=source_id,
            source_type="salesforce",
            entity_type="product",
            external_id=salesforce_id,
            title=f"Product: {name}",
            locator=(
                f"{self._instance_url}"
                f"/lightning/r/Product2/"
                f"{salesforce_id}/view"
            ),
            content=content,
            content_hash=text_hash(
                content
            ),
            metadata={
                "salesforce_object": "Product2",
                "entity_type": "product",
                "salesforce_id": salesforce_id,
                "salesforce_product_id": salesforce_id,
            },
        )


    # ==========================================================
    # Salesforce OpportunityLineItem -> KnowledgeRecord
    # ==========================================================

    def _build_opportunity_line_item_record(
        self,
        item: dict,
        source_id: str,
    ) -> KnowledgeRecord:

        line_item_id = (
            item.get("Id") or ""
        )

        opportunity_id = (
            item.get("OpportunityId") or ""
        )

        quantity = item.get(
            "Quantity"
        )

        unit_price = item.get(
            "UnitPrice"
        )

        total_price = item.get(
            "TotalPrice"
        )

        pricebook_entry_id = (
            item.get(
                "PricebookEntryId"
            )
            or ""
        )

        pricebook_entry = (
            item.get(
                "PricebookEntry"
            )
            or {}
        )

        product_id = (
            pricebook_entry.get(
                "Product2Id"
            )
            or ""
        )

        product = (
            pricebook_entry.get(
                "Product2"
            )
            or {}
        )

        product_name = (
            product.get(
                "Name"
            )
            or ""
        )

        product_code = (
            product.get(
                "ProductCode"
            )
            or ""
        )

        product_family = (
            product.get(
                "Family"
            )
            or ""
        )

        content = "\n".join(
            [
                "SALESFORCE OPPORTUNITY LINE ITEM",
                "",
                f"Opportunity ID: {self._safe(opportunity_id)}",
                f"Product ID: {self._safe(product_id)}",
                f"Product: {self._safe(product_name)}",
                f"Product Code: {self._safe(product_code)}",
                f"Product Family: {self._safe(product_family)}",
                f"Quantity: {self._safe(quantity)}",
                f"Unit Price: {self._safe(unit_price)}",
                f"Total Price: {self._safe(total_price)}",
                f"Pricebook Entry ID: {self._safe(pricebook_entry_id)}",
                f"Created: {self._safe(item.get('CreatedDate'))}",
                f"Updated: {self._safe(item.get('LastModifiedDate'))}",
            ]
        )

        base_url = (
            self._instance_url
            or ""
        ).rstrip("/")

        return KnowledgeRecord(
            record_id=(
                "salesforce:"
                "opportunitylineitem:"
                f"{line_item_id}"
            ),
            source_id=source_id,
            source_type="salesforce",
            entity_type="opportunity_line_item",
            source_name="Salesforce",
            external_id=line_item_id,
            title=(
                "Opportunity Line Item: "
                f"{product_name}"
            ),
            locator=(
                f"{base_url}"
                "/lightning/r/"
                "OpportunityLineItem/"
                f"{line_item_id}/view"
            ),
            content=content,
            content_hash=text_hash(
                content
            ),
            metadata={
                "salesforce_object": "OpportunityLineItem",
                "entity_type": "opportunity_line_item",
                "salesforce_id": line_item_id,
                "salesforce_opportunity_id": opportunity_id,
                "salesforce_product_id": product_id,
                "salesforce_product_name": product_name,
                "salesforce_product_code": product_code,
                "salesforce_pricebook_entry_id": pricebook_entry_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "total_price": total_price,
            },
        )

    # ==========================================================
    # Record dispatch
    # ==========================================================

    def _build_record(
        self,
        object_name: str,
        item: dict,
        source_id: str,
    ) -> KnowledgeRecord:

        if object_name == "Account":

            return self._build_account_record(
                item,
                source_id,
            )

        if object_name == "Opportunity":

            return self._build_opportunity_record(
                item,
                source_id,
            )

        if object_name == "Product2":

            return self._build_product_record(
                item,
                source_id,
            )

        if object_name == "OpportunityLineItem":

            return self._build_opportunity_line_item_record(
                item,
                source_id,
            )

        raise ValueError(
            "Unsupported Salesforce object: "
            f"{object_name}"
        )

    # ==========================================================
    # Initial ingestion
    # ==========================================================

    def initial_sync(
        self,
        notebook_id: str = (
            "default-knowledge-base"
        ),
    ) -> dict:

        source_id = (
            self.get_source_id(
                notebook_id
            )
        )

        print(
            "=========================================="
        )

        print(
            "Starting Salesforce initial ingestion"
        )

        print(
            f"Source ID: {source_id}"
        )

        print(
            "=========================================="
        )

        # ------------------------------------------------------
        # Fetch all Salesforce data first.
        # ------------------------------------------------------

        accounts = self.query(
            self._build_account_query(
                source_id,
                full_sync=True,
            )
        )

        opportunities = self.query(
            self._build_opportunity_query(
                source_id,
                full_sync=True,
            )
        )

        products = self.query(
            self._build_product_query(
                source_id,
                full_sync=True,
            )
        )

        opportunity_line_items = self.query(
            self._build_opportunity_line_item_query(
                source_id,
                full_sync=True,
            )
        )

        all_items = [
            (
                "Account",
                accounts,
            ),
            (
                "Opportunity",
                opportunities,
            ),
            (
                "Product2",
                products,
            ),
            (
                "OpportunityLineItem",
                opportunity_line_items,
            ),
        ]

        records = []

        # ------------------------------------------------------
        # Build all records.
        # ------------------------------------------------------

        for object_name, items in all_items:

            for item in items:

                record = self._build_record(
                    object_name=object_name,
                    item=item,
                    source_id=source_id,
                )

                records.append(
                    record
                )

        # ------------------------------------------------------
        # PASS 1
        #
        # KnowledgeService performs:
        #   sanitization
        #   chunking
        #   embeddings
        #   Chroma
        #   lexical indexing
        #   identity registration
        #
        # Relationships disabled here.
        # ------------------------------------------------------

        indexed = 0

        for record in records:

            self.knowledge_service.index_record(
                record=record,
                source_name="Salesforce",
                index_relationships=False,
            )

            indexed += 1

        print(
            "Salesforce PASS 1 complete:",
            indexed,
        )

        # ------------------------------------------------------
        # PASS 2
        #
        # Let RelationshipService handle all relationships.
        # ------------------------------------------------------

        relationships_created = 0

        relationship_service = (
            self.knowledge_service
            .relationship_service
        )

        identity_service = (
            self.knowledge_service
            .record_identity_service
        )

        if relationship_service:

            for record in records:

                result = (
                    relationship_service.index_record(
                        record=record,
                        record_identity_service=(
                            identity_service
                        ),
                    )
                )

                if isinstance(
                    result,
                    dict,
                ):

                    relationships_created += (
                        result.get(
                            "relationships_created",
                            0,
                        )
                    )

        # ------------------------------------------------------
        # Watermarks.
        # Only update after the entire object has been
        # successfully indexed.
        # ------------------------------------------------------

        self._set_latest_watermark(
            source_id,
            "Account",
            accounts,
        )

        self._set_latest_watermark(
            source_id,
            "Opportunity",
            opportunities,
        )

        self._set_latest_watermark(
            source_id,
            "Product2",
            products,
        )

        result = {
            "status": "indexed",
            "notebook_id": notebook_id,
            "source_id": source_id,
            "accounts": len(accounts),
            "opportunities": len(
                opportunities
            ),
            "products": len(products),
            "records_indexed": indexed,
            "relationships_created": (
                relationships_created
            ),
        }

        print(
            "Salesforce initial ingestion result:",
            result,
        )

        return result

    # ==========================================================
    # Incremental polling
    # ==========================================================

    def poll(
        self,
        notebook_id: str = (
            "default-knowledge-base"
        ),
    ) -> dict:

        source_id = (
            self.get_source_id(
                notebook_id
            )
        )

        print(
            "=========================================="
        )

        print(
            "Polling Salesforce"
        )

        print(
            f"Source ID: {source_id}"
        )

        print(
            "=========================================="
        )

        object_definitions = [
            (
                "Account",
                self._build_account_query,
            ),
            (
                "Opportunity",
                self._build_opportunity_query,
            ),
            (
                "Product2",
                self._build_product_query,
            ),
        ]

        statistics = {}

        # Records successfully processed in Pass 1.
        successful_records = []

        # Per-object records successfully processed in
        # ascending LastModifiedDate order.
        successful_items = {}

        # ======================================================
        # PASS 1
        #
        # Fetch changed records and send them through
        # KnowledgeService.
        #
        # We DO NOT create relationships yet.
        # ======================================================

        for object_name, query_builder in (
            object_definitions
        ):

            print(
                f"Polling Salesforce object: "
                f"{object_name}"
            )

            items = self.query(
                query_builder(
                    source_id,
                    full_sync=False,
                )
            )

            statistics[
                object_name
            ] = {
                "found": len(items),
                "indexed": 0,
                "failed": 0,
            }

            successful_items[
                object_name
            ] = []

            # SOQL is ordered ascending by LastModifiedDate.
            #
            # If one record fails, stop processing this object
            # so the watermark can safely remain before the
            # failed record.
            for item in items:

                try:

                    record = self._build_record(
                        object_name=object_name,
                        item=item,
                        source_id=source_id,
                    )

                    self.knowledge_service.index_record(
                        record=record,
                        source_name="Salesforce",
                        index_relationships=False,
                    )

                    successful_items[
                        object_name
                    ].append(item)

                    successful_records.append(
                        record
                    )

                    statistics[
                        object_name
                    ]["indexed"] += 1

                except Exception as exc:

                    statistics[
                        object_name
                    ]["failed"] += 1

                    print(
                        f"Salesforce {object_name} "
                        f"{item.get('Id')} failed: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

                    # IMPORTANT:
                    # Don't advance the watermark beyond
                    # this failed record.
                    break

        print(
            "Salesforce PASS 1 complete."
        )

        # ======================================================
        # PASS 2
        #
        # All successfully indexed records now have their
        # identities registered.
        #
        # RelationshipService owns relationship extraction.
        # ======================================================

        relationships_created = 0

        relationship_service = (
            self.knowledge_service
            .relationship_service
        )

        identity_service = (
            self.knowledge_service
            .record_identity_service
        )

        if relationship_service:

            for record in successful_records:

                try:

                    result = (
                        relationship_service.index_record(
                            record=record,
                            record_identity_service=(
                                identity_service
                            ),
                        )
                    )

                    if isinstance(
                        result,
                        dict,
                    ):

                        relationships_created += (
                            result.get(
                                "relationships_created",
                                0,
                            )
                        )

                except Exception as exc:

                    print(
                        "Salesforce relationship "
                        f"processing failed for "
                        f"{record.record_id}: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

        print(
            "Salesforce PASS 2 complete."
        )

        # ======================================================
        # Watermarks
        #
        # Only advance to the last SUCCESSFULLY processed
        # record for each object.
        # ======================================================

        for object_name, items in (
            successful_items.items()
        ):

            if not items:
                continue

            latest = (
                self._latest_timestamp(
                    items
                )
            )

            if latest:

                self._set_watermark(
                    source_id=source_id,
                    object_name=object_name,
                    timestamp=latest,
                )

        result = {
            "status": "success",
            "notebook_id": notebook_id,
            "source_id": source_id,
            "statistics": statistics,
            "relationships_created": (
                relationships_created
            ),
        }

        print(
            "Salesforce poll result:",
            result,
        )

        return result

    # ==========================================================
    # Reset Salesforce sync state
    # ==========================================================

    def reset_state(
        self,
        notebook_id: str = (
            "default-knowledge-base"
        ),
    ) -> None:

        source_id = (
            self.get_source_id(
                notebook_id
            )
        )

        state = self._load_state()

        if source_id in state:

            del state[
                source_id
            ]

            self._save_state(
                state
            )

        print(
            "Salesforce sync state reset:",
            source_id,
        )
