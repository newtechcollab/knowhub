import time
import threading
import requests

# ==================================================================
# ServiceNow Connector - Connects and Retrieves data from ServiceNow
# ==================================================================
class ServiceNowConnector:

    """
    ServiceNow integration connector.

    Current responsibilities:
        - OAuth 2.0 Client Credentials authentication
        - Access token caching and renewal
        - Authenticated ServiceNow REST API requests

    Future responsibilities:
        - Initial ingestion
        - Incremental polling
        - KnowledgeRecord conversion
        - Relationship extraction
        - Sync state management
    """

    # ----------------------------------------------------------
    # Initialize the ServiceNow Connector
    # ----------------------------------------------------------
    def __init__(
        self,
        instance_url: str,
        client_id: str,
        client_secret: str,
    ):
        self.instance_url = instance_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret

        self.token_url = (
            f"{self.instance_url}/oauth_token.do"
        )

        self.session = requests.Session()

        self._access_token = None
        self._token_expires_at = 0.0
        self._token_lock = threading.RLock()

    # ----------------------------------------------------------
    # Authenticate using Client Credentials
    # ----------------------------------------------------------

    def _authenticate(self) -> str:

        print("********** Authenticating with ServiceNow - Getting Access Token ************\n\n")
        print(f"******* URL -> {self.token_url} **********")
        print(f"******* Client Id -> {self.client_id} **********")
        print(f"******* Client Secret -> {self.client_secret} **********")
        response = self.session.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={
                "Accept": "application/json",
            },
            timeout=30,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                "ServiceNow OAuth authentication failed: "
                f"{response.status_code} - {response.text}"
            )

        payload = response.json()

        access_token = payload.get("access_token")
        print(f"********** Authentication with ServiceNow is successful - Access Token received is {access_token} ************\n\n")

        if not access_token:
            raise RuntimeError(
                "ServiceNow OAuth response did not "
                "contain access_token."
            )

        expires_in = int(
            payload.get("expires_in", 1800)
        )

        self._access_token = access_token

        # Refresh slightly before token expiry.
        self._token_expires_at = (
            time.monotonic()
            + max(0, expires_in - 60)
        )

        return access_token

    # ----------------------------------------------------------
    # Get cached or new access token
    # ----------------------------------------------------------

    def _get_access_token(
        self,
        force_refresh: bool = False,
    ) -> str:

        with self._token_lock:

            if (
                not force_refresh
                and self._access_token
                and time.monotonic()
                < self._token_expires_at
            ):
                return self._access_token

            return self._authenticate()

    # ----------------------------------------------------------
    # Execute authenticated GET
    # ----------------------------------------------------------

    def get(
        self,
        path: str,
        params: dict | None = None,
    ) -> dict:

        url = (
            f"{self.instance_url}/"
            f"{path.lstrip('/')}"
        )

        for attempt in range(2):

            token = self._get_access_token(
                force_refresh=(attempt == 1)
            )

            response = self.session.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=60,
            )

            # Retry once with a fresh token.
            if (
                response.status_code == 401
                and attempt == 0
            ):
                continue

            response.raise_for_status()

            return response.json()

        raise RuntimeError(
            "ServiceNow API authentication failed."
        )
