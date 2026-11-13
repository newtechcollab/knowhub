import os
import re
import uuid
import certifi
import hashlib
import traceback
import json
import openpyxl
import threading
import chromadb
import fitz
import requests
import base64
import asyncio

from urllib.parse import quote
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from github import Github
from urllib.parse import urlparse
from requests.auth import HTTPBasicAuth
from docx import Document
from pptx import Presentation
from fastapi import BackgroundTasks
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel
from fastapi import HTTPException
from github.GithubException import RateLimitExceededException, GithubException
from typing import Optional, Literal
from pydantic import BaseModel
from dotenv import load_dotenv

from models.knowledge import KnowledgeRecord
from knowledge.chunks import text_hash
from knowledge.knowledgerepository import KnowledgeRepository
from knowledge.lexicalrepository import LexicalRepository
from knowledge.lexicalrepository import LexicalRepository
from knowledge.knowledgeservice import KnowledgeService
from knowledge.relationshipservice import RelationshipService
from knowledge.recordidentityservice import RecordIdentityService
from security.data_sanitizer import SensitiveDataSanitizer

from connectors.salesforce import SalesforceConnector
from connectors.servicenow import ServiceNowConnector

load_dotenv()

# -------------------------------------------------------------------
# All general configuration is given below, such as Embedding Model 
# Name, localtion of local folders etc.
# There is no need to change these, unless you really want to
# -------------------------------------------------------------------

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text",)
STATIC_NOTEBOOK_ID = "default-knowledge-base"
BASE_DIR = "./data"
DATA_DIR = "./data"
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
STATIC_DIR = "./static"
RELATIONSHIP_FILE = os.path.join(BASE_DIR, "knowledge_relationships.json",)
HMAC_SECRET = os.environ.get("KNOWLEDGE_HUB_HMAC_SECRET", "1234")


MAX_EMBED_CHUNK_CHARS = 12_000
MAX_EMBED_BATCH_CHARS = 60_000
MAX_EMBED_BATCH_DOCS = 24
MAX_EMBED_SUBCHUNK_OVERLAP = 200
EMBED_BATCH_SIZE = 8

# -------------------------------------------------------------------
# Confluence-related configuration is given below, such as Base URL, 
# Polling Interval etc.
# Set the Confluence Base URL in system environment for this to work
# -------------------------------------------------------------------

CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL")
CONFLUENCE_POLL_INTERVAL_SECONDS = 3600
CONFLUENCE_SYNC_FILE = os.path.join(BASE_DIR, "confluence_sync_state.json",)
CONFLUENCE_SOURCES_FILE = os.path.join(BASE_DIR, "confluence_sources.json",)
confluence_poll_stop_event = asyncio.Event()

# -------------------------------------------------------------------
# ServiceNow-related configuration is given below, such as Instance Name etc.
# All of these have to be set in system environment for this to work
# -------------------------------------------------------------------

SERVICENOW_INSTANCE = os.getenv("SERVICENOW_INSTANCE", "").rstrip("/")

# We are no longer using User/Password-based authentication
# Instead, we are using OAuth-based authentication
#SERVICENOW_USERNAME = os.getenv("SERVICENOW_USERNAME", "")
#SERVICENOW_PASSWORD = os.getenv("SERVICENOW_PASSWORD", "")
SERVICENOW_CLIENT_ID = os.getenv("SERVICENOW_CLIENT_ID", "")
SERVICENOW_CLIENT_SECRET = os.getenv("SERVICENOW_CLIENT_SECRET", "")

SERVICENOW_POLL_INTERVAL_SECONDS = 3600
SERVICENOW_SYNC_FILE = os.path.join(DATA_DIR, "servicenow_sync_state.json",)
SERVICENOW_SOURCE_IDS_FILE = os.path.join(DATA_DIR, "servicenow_source_ids.json",)
servicenow_poll_task = None
servicenow_poll_stop_event = asyncio.Event()


# -------------------------------------------------------------------
# Salesforce-related configuration is given below 
# All of these have to be set in system environment for this to work
# -------------------------------------------------------------------

SALESFORCE_POLL_INTERVAL_SECONDS = 3600
SALESFORCE_SYNC_STATE_FILE = os.path.join(DATA_DIR, "salesforce_sync_state.json",)
salesforce_poll_task = None
salesforce_poll_stop_event = asyncio.Event()

# -------------------------------------------------------------------
# JIRA-related configuration is given below, such as Base URL, 
# JIRA Email and API Token etc.
# All of these have to be set in system environment for this to work
# -------------------------------------------------------------------

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "",).rstrip("/")
ATLASSIAN_EMAIL = os.getenv("CONFLUENCE_EMAIL", "",)
ATLASSIAN_API_TOKEN = os.getenv("CONFLUENCE_API_TOKEN", "",)
JIRA_SOURCE_IDS_FILE = os.path.join(BASE_DIR, "jira_source_ids.json",)
JIRA_SYNC_STATE_FILE = os.path.join(BASE_DIR, "jira_sync_state.json",) 
JIRA_POLL_INTERVAL_SECONDS = 3600
jira_poll_task = None
jira_poll_stop_event = asyncio.Event()

# -------------------------------------------------------------------
# ServiceNow Request Model 
# -------------------------------------------------------------------

class ServiceNowIngestRequest(BaseModel):
    notebook_id: Optional[str] = None
    query: Optional[str] = None
    limit: int = 100

# -------------------------------------------------------------------
# Search Request Model 
# -------------------------------------------------------------------

class SearchRequest(BaseModel):
    query: str
    top_k: int = 8

# -------------------------------------------------------------------
# ServiceNow terms and record patterns
# -------------------------------------------------------------------

SERVICENOW_TABLE_TERMS = {
    "incident": "incident",
    "incidents": "incident",

    "problem": "problem",
    "problems": "problem",

    "change": "change_request",
    "changes": "change_request",
    "change request": "change_request",
    "change requests": "change_request",
}

SERVICENOW_RECORD_PATTERN = re.compile(
    r"\b(?:INC|PRB|CHG)\d+\b",
    re.IGNORECASE,
)

SERVICENOW_DISTANCE_THRESHOLD = float(
    os.getenv("SERVICENOW_DISTANCE_THRESHOLD", "0.45")
)

# -------------------------------------------------------------------
# JIRA Request Model 
# -------------------------------------------------------------------

class JiraIngestRequest(BaseModel):
    notebook_id: str | None = None
    project_key: str
    max_results: int = 100


# Inject macOS system Keychain into Python's SSL — fixes corporate proxy CA errors
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # truststore optional; run: pip install truststore

JOBS = {}
JOBS_LOCK = threading.Lock()

# Return a default notebook Id
def get_notebook_id() -> str:
    return STATIC_NOTEBOOK_ID

# app = FastAPI()
@asynccontextmanager
async def lifespan(app: FastAPI):
    global servicenow_poll_task
    global confluence_poll_task
    global jira_poll_task
    global salesforce_poll_task

    # ----------------------------------------------------------
    # Salesforce Polling
    # ----------------------------------------------------------
    servicenow_poll_stop_event.clear()
    salesforce_poll_task = asyncio.create_task(
        salesforce_poll_loop()
    )

    # ----------------------------------------------------------
    # ServiceNow Polling
    # ----------------------------------------------------------
    servicenow_poll_stop_event.clear()
    servicenow_poll_task = asyncio.create_task(
        servicenow_poll_loop()
    )

    # ----------------------------------------------------------
    # Confluence Polling
    # ----------------------------------------------------------

    confluence_poll_stop_event.clear()
    confluence_poll_task = (
        asyncio.create_task(
            confluence_poll_loop()
        )
    )

    # ----------------------------------------------------------
    # JIRA Polling
    # ----------------------------------------------------------

    jira_poll_stop_event.clear()
    jira_poll_task = (
        asyncio.create_task(
            jira_poll_loop()
        )
    )

    yield

    # ----------------------------------------------------------
    # Stop Salesforce Polling
    # ----------------------------------------------------------
    salesforce_poll_stop_event.set()

    if salesforce_poll_task:
        try:
            await salesforce_poll_task
        except asyncio.CancelledError:
            pass

    # ----------------------------------------------------------
    # Stop ServiceNow Polling
    # ----------------------------------------------------------
    servicenow_poll_stop_event.set()

    if servicenow_poll_task:
        try:
            await servicenow_poll_task
        except asyncio.CancelledError:
            pass

    # ----------------------------------------------------------
    # Stop Confluence Polling
    # ----------------------------------------------------------

    confluence_poll_stop_event.set()

    if confluence_poll_task:
        try:
            await confluence_poll_task
        except asyncio.CancelledError:
            pass

    # ----------------------------------------------------------
    # Stop JIRA Polling
    # ----------------------------------------------------------

    jira_poll_stop_event.set()

    if jira_poll_task:
        try:
            await jira_poll_task
        except asyncio.CancelledError:
            pass

# Create FastAPI App
app = FastAPI(lifespan=lifespan)

# Specify LLM Model to be used
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-6-luna",)

# Create OpenAI-compatible Client - connects to OpenAI URL
openai_client = OpenAI()

#Connecting to LMProxy running inside VSCode
#openai_client = OpenAI(base_url="http://localhost:4000/openai")

#Connecting to local proxy that routes to GitHub Copilot Hosted LLMs and Ollama Hosted Embedding Models
#openai_client = OpenAI(base_url="http://localhost:9000/v1")

github_client = Github(os.getenv("GITHUB_TOKEN"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

chroma_client = chromadb.PersistentClient(path="./data/chroma")
collection = chroma_client.get_or_create_collection(name="knowledge_base")
ALLOWED_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".yaml",
    ".yml",
    ".json",
    ".md",
    ".sql",
    ".sh",
    ".html",
    ".css",
}

IGNORED_FOLDERS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "__pycache__",
}

SUPPORTED_DIRECTORY_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".docx",
    ".pptx",
    ".xlsx",
}

# Create Vector Embeddings for a given text
def embed_texts(texts):
    try:
        response = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=texts,)
        return [item.embedding for item in response.data]
    except Exception as exc:
        print(f"****** Error while creating Vector Embeddings -> {exc} ********\n\n")
        print(f"********* Error while creating Embeddings !!! Check whether Vector Embedding Server is running or not !!! \n\n") 

# Create Knowledge Repository Object
knowledge_repository = KnowledgeRepository(collection=collection, chroma_client=chroma_client,)

# Create Lexical Repository Object
lexical_repository = LexicalRepository(db_path="./data/knowledge_lexical.db")

# Create Record Identity Service
record_identity_service = RecordIdentityService(
    storage_file=os.path.join(
        BASE_DIR,
        "knowledge_record_index.json",
    )
)

# Create Relationship Service
relationship_service = RelationshipService(
    storage_file=RELATIONSHIP_FILE,
    record_identity_service=record_identity_service,
)

# Create SensitiveDataSanitizer 
datasanitizer = SensitiveDataSanitizer(hmac_secret=HMAC_SECRET,)

# Create Knowledge Service
knowledge_service = KnowledgeService(
    repository=knowledge_repository,
    embed_fn=embed_texts,
    relationship_service=relationship_service,
    record_identity_service=record_identity_service,
    lexical_repository = lexical_repository,
    openai_client=openai_client,
    model=LLM_MODEL,
    datasanitizer=datasanitizer,
)

# Salesforce Connector 
salesforce_connector = None

# Get Salesforce Connector
def get_salesforce_connector():
    global salesforce_connector

    if salesforce_connector is None:
        salesforce_connector = SalesforceConnector(
            knowledge_service=knowledge_service,
            state_file=SALESFORCE_SYNC_STATE_FILE,
        )

    return salesforce_connector

# ServiceNow Connector
servicenow_connector = None

# Get ServiceNow Connector
def get_servicenow_connector():
    global servicenow_connector

    if servicenow_connector is None:
        servicenow_connector = ServiceNowConnector(
            instance_url=SERVICENOW_INSTANCE,
            client_id=SERVICENOW_CLIENT_ID,
            client_secret=SERVICENOW_CLIENT_SECRET,
        )

    return servicenow_connector

# Polling of Salesforce
async def salesforce_poll_loop():

    while not salesforce_poll_stop_event.is_set():

        try:
            # Only poll Sync State file exists
            if os.path.isfile(SALESFORCE_SYNC_STATE_FILE):
                print("Starting Salesforce polling since previous Salesforce Ingestion record exists\n\n")

                connector = get_salesforce_connector()

                result = await asyncio.to_thread(
                    connector.poll_once
                )

                print("Salesforce polling completed:", result,)

            else:
                print("Salesforce polling skipped since initial ingestion not registered.\n\n")

        except Exception as exc:
            print(
                "Salesforce polling failed:",
                type(exc).__name__,
                str(exc),
            )
            traceback.print_exc()

        try:
            await asyncio.wait_for(
                salesforce_poll_stop_event.wait(),
                timeout=SALESFORCE_POLL_INTERVAL_SECONDS,
            )

        except asyncio.TimeoutError:
            pass

# Fetch all Confluence pages under a given space
def fetch_changed_confluence_pages_for_space(
    space_key: str,
    last_updated: str | None,
    limit: int = 100,
):
    base_url = os.getenv("CONFLUENCE_BASE_URL")
    email = os.getenv("CONFLUENCE_EMAIL")
    api_token = os.getenv("CONFLUENCE_API_TOKEN")

    if not base_url or not email or not api_token:
        raise RuntimeError(
            "Missing Confluence credentials. "
            "Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL "
            "and CONFLUENCE_API_TOKEN."
        )

    space_id = get_confluence_space_id(space_key)
    api_url = (
        f"{base_url.rstrip('/')}"
        f"/api/v2/spaces/{space_id}/pages"
    )

    pages = []
    next_url = api_url
    first_request = True

    while next_url:
        if first_request:
            params = {
                "limit": min(max(limit, 1), 100),
                "status": "current",
                "sort": "modified-date",
                "body-format": "storage",
            }

            response = requests.get(
                next_url,
                params=params,
                auth=HTTPBasicAuth(
                    email,
                    api_token,
                ),
                headers={
                    "Accept": "application/json",
                },
                timeout=60,
            )

            first_request = False

        else:
            response = requests.get(
                next_url,
                auth=HTTPBasicAuth(
                    email,
                    api_token,
                ),
                headers={
                    "Accept": "application/json",
                },
                timeout=60,
            )

        print(
            "Confluence v2 pages URL:",
            response.url,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Confluence v2 page fetch failed: "
                f"{response.status_code} - "
                f"{response.text}"
            )

        payload = response.json()

        results = payload.get(
            "results",
            [],
        ) or []

        for page in results:
            updated_at = (
                page.get(
                    "version",
                    {}
                )
                .get(
                    "createdAt",
                    "",
                )
                or page.get(
                    "version",
                    {}
                )
                .get(
                    "when",
                    "",
                )
            )

            # Keep the exact timestamp string returned by
            # Confluence and compare it to the checkpoint.
            if last_updated and updated_at:
                if updated_at <= last_updated:
                    continue

            pages.append(page)

        next_url = (
            payload.get(
                "_links",
                {}
            ).get(
                "next"
            )
        )

        if next_url and not next_url.startswith("http"):
            next_url = (
                f"{base_url.rstrip('/')}"
                f"{next_url}"
            )

    return pages

# Index Confluence Page
def index_confluence_page_for_polling(
    page_id: str,
    source_id: str,
    source_name: str,
):
    page = fetch_confluence_page_for_polling(
        page_id
    )

    if not page:
        return {
            "status": "skipped",
            "reason": "Page content unavailable",
            "page_id": page_id,
        }

    content = (
        page.get(
            "text",
            ""
        )
        or ""
    ).strip()

    if not content:
        return {
            "status": "skipped",
            "reason": "Empty page content",
            "page_id": page_id,
        }

    title = (
        page.get(
            "title",
            "",
        )
        or "Confluence Page"
    )

    locator = (
        page.get(
            "url",
            "",
        )
        or (
            f"{CONFLUENCE_BASE_URL}"
            f"/pages/viewpage.action"
            f"?pageId={page_id}"
        )
    )

    updated_raw = (
        page.get(
            "updated_at",
            "",
        )
        or ""
    )

    updated_at = updated_raw

    record = KnowledgeRecord(
        record_id=(
            f"confluence:{page_id}"
        ),
        source_id=source_id,
        source_type="confluence",
        entity_type="confluence",
        external_id=str(
            page_id
        ),
        title=title,
        locator=locator,
        content=content,
        content_hash=text_hash(
            content
        ),
        updated_at=updated_at,
    )

    result = knowledge_service.index_record(
        record=record,
        source_name=source_name,
    )

    return {
        "status": result.get(
            "status",
            "indexed",
        ),
        "page_id": str(
            page_id
        ),
        "title": title,
        "updated_at": updated_raw,
        "chunks_indexed": result.get(
            "chunks_indexed",
            0,
        ),
    }

# Get details of a Confluence Page
def fetch_confluence_page_for_polling(page_id: str):
    if not page_id:
        return None

    page_url = (
        f"{CONFLUENCE_BASE_URL.rstrip('/')}"
        f"/pages/viewpage.action"
        f"?pageId={page_id}"
    )

    try:
        title, pages = extract_confluence_page(
            page_url
        )
    except Exception as exc:
        print(
            f"Failed to fetch Confluence page "
            f"{page_id}: {type(exc).__name__}: {exc}"
        )
        traceback.print_exc()
        return None

    if not pages:
        return None

    page = pages[0]

    page["page_id"] = str(page_id)
    page["title"] = page.get("title") or title
    page["url"] = (
        page.get("url")
        or page.get("file_path")
        or page_url
    )

    return page

# Poll Confluence to get new content under a given space
def poll_confluence_space_source(
    source: dict,
    sync_state: dict,
):
    source_id = source.get(
        "source_id"
    )

    space_key = source.get(
        "space_key"
    )

    if not source_id or not space_key:
        print(
            "Invalid Confluence space source:",
            source,
        )
        return 0

    source_state = (
        sync_state
        .setdefault(
            "sources",
            {}
        )
        .setdefault(
            source_id,
            {}
        )
    )

    last_updated = source_state.get(
        "last_updated",
        "",
    )

    print(
        f"Confluence space polling: "
        f"space={space_key}, "
        f"checkpoint={last_updated}"
    )

    candidates = (
        fetch_changed_confluence_pages_for_space(
            space_key=space_key,
            last_updated=last_updated,
            limit=25,
        )
    )

    if not candidates:
        print(
            f"Confluence space {space_key}: "
            "no changed pages."
        )
        return 0

    indexed_count = 0
    if last_updated:
        newest_updated = last_updated
    else:
        newest_updated = None

    source_name = (
        source.get(
            "source_name"
        )
        or f"Confluence Space - {space_key}"
    )

    # ----------------------------------------------------------
    # Process candidates
    # ----------------------------------------------------------

    for candidate in candidates:
        page_id = candidate.get(
            "id"
        )

        if not page_id:
            continue

        page_id = str(
            page_id
        )

        # Fetch authoritative content.
        page = fetch_confluence_page_for_polling(
            page_id
        )

        if not page:
            print(
                f"Unable to fetch Confluence "
                f"page {page_id}"
            )
            continue

        updated_raw = (
            page.get(
                "updated_at",
                "",
            )
            or ""
        )

        print(f"************** The raw last update date/time of Confluence Page being polled is {updated_raw} *************** ")
        updated_at = updated_raw
        
        print(f"The actual last update date/time of Confluence Page being polled is {updated_at}")

        # ------------------------------------------------------
        # Additional local protection.
        #
        # Don't index anything older/equal to the checkpoint.
        # ------------------------------------------------------

        if (
            newest_updated
            and updated_at
            and updated_at <= newest_updated
        ):
            print("No update to Confluence Page, hence skipping")
            continue

        print("********* The page has changed, so will ingest new page content *********")
        result = (
            index_confluence_page_for_polling(
                page_id=page_id,
                source_id=source_id,
                source_name=source_name,
            )
        )

        if result.get(
            "status"
        ) != "indexed":
            continue

        indexed_count += 1

        if (
            updated_at
            and (
                newest_updated is None
                or updated_at > newest_updated
            )
        ):
            newest_updated = updated_at

    # ----------------------------------------------------------
    # Save checkpoint only after processing completes
    # ----------------------------------------------------------

    if newest_updated:
        source_state[
            "last_updated"
        ] = newest_updated

        save_confluence_sync_state(
            sync_state
        )

    print(
        f"Confluence space {space_key}: "
        f"indexed {indexed_count} pages; "
        f"checkpoint="
        f"{source_state.get('last_updated', '')}"
    )

    return indexed_count

# Poll Confluence to get new content for a given Page
def poll_confluence_page_source(
    source: dict,
    sync_state: dict,
):
    source_id = source.get(
        "source_id"
    )

    page_id = source.get(
        "page_id"
    )

    if not source_id or not page_id:
        print(
            "Invalid Confluence page source:",
            source,
        )
        return 0

    source_state = (
        sync_state
        .setdefault(
            "sources",
            {}
        )
        .setdefault(
            source_id,
            {}
        )
    )

    last_updated = source_state.get(
        "last_updated",
        "",
    )

    print(
        f"Confluence page polling: "
        f"page_id={page_id}, "
        f"checkpoint={last_updated}"
    )

    page = fetch_confluence_page_for_polling(
        str(page_id)
    )

    if not page:
        print(
            f"Confluence page {page_id} "
            "could not be retrieved."
        )
        return 0

    updated_raw = (
        page.get(
            "updated_at",
            "",
        )
        or ""
    )

    print(f"************ Raw value of last update {updated_raw}")

    updated_at = updated_raw

    print(f"************ Parsed value of last update {updated_at}")

    last_dt = last_updated

    print(f"************ Final value of last update {updated_at}")

    # ----------------------------------------------------------
    # No change
    # ----------------------------------------------------------

    if (
        last_dt
        and updated_at
        and updated_at <= last_dt
    ):
        print(
            f"Confluence page {page_id}: "
            "no change."
        )
        return 0

    print("************* Confluence Page has changed **************")

    source_name = (
        source.get(
            "source_name"
        )
        or page.get(
            "title"
        )
        or "Confluence"
    )

    result = index_confluence_page_for_polling(
        page_id=str(page_id),
        source_id=source_id,
        source_name=source_name,
    )

    if result.get(
        "status"
    ) != "indexed":
        return 0

    if updated_at:
        print(f"*************** Value of updated_at -> {updated_at}")

        source_state["last_updated"] = updated_at

        print(f"********* Value of sync state {sync_state} **********")
        save_confluence_sync_state(
            sync_state
        )

    print(
        f"Confluence page {page_id}: "
        f"indexed successfully."
    )

    return 1

# Load Confluence Sources
def load_confluence_sources():
    if not os.path.exists(
        CONFLUENCE_SOURCES_FILE
    ):
        return []

    try:
        with open(
            CONFLUENCE_SOURCES_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(data, list):
            return []

        return data

    except Exception as exc:
        print(
            "Failed to load Confluence sources:",
            type(exc).__name__,
            str(exc),
        )

        return []


# Save Confluence Sources
def save_confluence_sources(
    sources: list[dict],
):
    print(f"Saving Confluence Source {sources}")
    temp_file = (
        f"{CONFLUENCE_SOURCES_FILE}.tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            sources,
            f,
            indent=2,
        )

    os.replace(
        temp_file,
        CONFLUENCE_SOURCES_FILE,
    )


# Load active Confluence Sources
def get_active_confluence_sources():
    sources = load_confluence_sources()

    return [
        source
        for source in sources
        if source.get(
            "active",
            True,
        )
    ]

def load_confluence_sync_state():
    if not os.path.exists(
        CONFLUENCE_SYNC_FILE
    ):
        return {
            "last_updated": ""
        }

    try:
        with open(
            CONFLUENCE_SYNC_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)

    except Exception as exc:
        print(
            "Failed to load Confluence sync state:",
            exc,
        )

        return {
            "last_updated": ""
        }


def save_confluence_sync_state(
    state: dict,
):
    temp_file = (
        f"{CONFLUENCE_SYNC_FILE}.tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            indent=2,
        )

    os.replace(
        temp_file,
        CONFLUENCE_SYNC_FILE,
    )

def parse_confluence_datetime(
    value: str | None,
):
    if not value:
        return None

    value = value.strip()

    try:
        parsed = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )

        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=timezone.utc
            )

        return parsed

    except ValueError:
        return None

def format_confluence_checkpoint(
    value: datetime,
):
    if value.tzinfo is None:
        value = value.replace(
            tzinfo=timezone.utc
        )

    return value.astimezone(
        timezone.utc
    ).isoformat()


def fetch_new_confluence_pages(
    last_updated: str | None,
    limit: int = 25,
):
    """
    Fetch pages modified after last_updated.

    Returns lightweight page metadata.
    Full page content is fetched separately.
    """

    base_url = (
        f"{CONFLUENCE_BASE_URL}"
        "/wiki/rest/api/content/search"
    )

    params = {
        "limit": min(
            max(limit, 1),
            25,
        ),
        "cql": (
            "type=page "
            "AND status=current "
            "ORDER BY lastmodified ASC"
        ),
    }

    if last_updated:
        # Convert checkpoint into a CQL-friendly datetime.
        dt = parse_confluence_datetime(
            last_updated
        )

        if dt:
            cql_timestamp = dt.strftime(
                "%Y-%m-%d %H:%M"
            )

            params["cql"] = (
                "type=page "
                "AND status=current "
                f"AND lastmodified > "
                f"'{cql_timestamp}' "
                "ORDER BY lastmodified ASC"
            )

    response = requests.get(
        base_url,
        params=params,
        auth=(
            CONFLUENCE_USERNAME,
            CONFLUENCE_PASSWORD,
        ),
        headers={
            "Accept": "application/json",
        },
        timeout=60,
    )

    print(
        "Confluence search URL:",
        response.url,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Confluence search failed "
            f"with status "
            f"{response.status_code}: "
            f"{response.text}"
        )

    data = response.json()

    results = data.get(
        "results",
        [],
    ) or []

    print(
        f"Confluence polling: "
        f"received {len(results)} candidate pages"
    )

    return results

def fetch_confluence_page(
    page_id: str,
):
    url = (
        f"{CONFLUENCE_BASE_URL}"
        f"/wiki/rest/api/v2/pages/{page_id}"
    )

    response = requests.get(
        url,
        params={
            "expand": (
                "body.storage,"
                "version,"
                "space,"
                "history"
            )
        },
        auth=(
            CONFLUENCE_USERNAME,
            CONFLUENCE_PASSWORD,
        ),
        headers={
            "Accept": "application/json",
            "Serialize-Version-Property": true,
        },
        timeout=60,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Failed to fetch Confluence page "
            f"{page_id}: "
            f"{response.status_code}: "
            f"{response.text}"
        )

    return response.json()

def confluence_html_to_text(
    html: str,
):
    if not html:
        return ""

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    return " ".join(
        soup.get_text(
            " ",
            strip=True,
        ).split()
    )

def get_or_create_confluence_source_id(
    space_key: str,
):
    state_file = os.path.join(
        BASE_DIR,
        "confluence_source_ids.json",
    )

    if os.path.exists(
        state_file
    ):
        try:
            with open(
                state_file,
                "r",
                encoding="utf-8",
            ) as f:
                state = json.load(f)
        except Exception:
            state = {}
    else:
        state = {}

    key = (
        space_key
        or "default"
    )

    if key not in state:
        state[key] = str(
            uuid.uuid4()
        )

        temp_file = (
            f"{state_file}.tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                state,
                f,
                indent=2,
            )

        os.replace(
            temp_file,
            state_file,
        )

    return state[key]

def index_confluence_page(
    page_id: str,
):
    data = fetch_confluence_page(
        page_id
    )

    title = data.get(
        "title",
        "",
    )

    space = data.get(
        "space",
        {}
    ) or {}

    space_key = space.get(
        "key",
        "",
    )

    space_name = space.get(
        "name",
        "",
    )

    body = (
        data.get(
            "body",
            {}
        )
        .get(
            "storage",
            {}
        )
        .get(
            "value",
            ""
        )
    )

    content = confluence_html_to_text(
        body
    )

    if not content.strip():
        return {
            "status": "skipped",
            "reason": "Empty page content",
            "page_id": page_id,
        }

    version = data.get(
        "version",
        {}
    ) or {}

    updated_raw = version.get(
        "when",
        "",
    )

    updated_at = updated_raw

    source_id = (
        get_or_create_confluence_source_id(
            space_key
        )
    )

    source_name = (
        f"Confluence - "
        f"{space_name or space_key or 'Unknown Space'}"
    )

    record_id = (
        f"confluence:{page_id}"
    )

    page_url = (
        f"{CONFLUENCE_BASE_URL}"
        f"/wiki/pages/viewpage.action?"
        f"pageId={page_id}"
    )

    record = KnowledgeRecord(
        record_id=record_id,
        source_id=source_id,
        source_type="confluence",
        entity_type="confluence",
        external_id=page_id,
        title=title,
        locator=page_url,
        content=content,
        content_hash=text_hash(
            content
        ),
        updated_at=updated_at,
    )

    result = knowledge_service.index_record(
        record=record,
        source_name=source_name,
    )

    return {
        "status": result.get(
            "status",
            "indexed",
        ),
        "page_id": page_id,
        "title": title,
        "space_key": space_key,
        "updated_at": updated_raw,
        "chunks_indexed": result.get(
            "chunks_indexed",
            0,
        ),
    }

def poll_confluence_once():
    print(
        "**** Starting Confluence Polling Process ****"
    )

    sources = get_active_confluence_sources()

    if not sources:
        print(
            "Confluence polling skipped: "
            "no active Confluence sources."
        )
        return

    sync_state = load_confluence_sync_state()
    print(f"************ Value of sync_state after loading {sync_state} *********** ")

    total_indexed = 0

    for source in sources:
        try:
            scope = source.get(
                "scope"
            )

            if scope == "page":

                total_indexed += (
                    poll_confluence_page_source(
                        source=source,
                        sync_state=sync_state,
                    )
                )

            elif scope == "space":

                total_indexed += (
                    poll_confluence_space_source(
                        source=source,
                        sync_state=sync_state,
                    )
                )

            else:
                print(
                    "Skipping unsupported "
                    "Confluence source scope:",
                    scope,
                )

        except Exception as exc:
            print(
                "Confluence polling failed for "
                f"{source.get('url', '')}: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            traceback.print_exc()

            # Continue with the next source.
            continue

    print(
        f"**** Confluence Polling Complete. "
        f"Indexed {total_indexed} pages ****"
    )

# Confluence Poll loop
async def confluence_poll_loop():
    while not (
        confluence_poll_stop_event.is_set()
    ):
        try:
            await asyncio.to_thread(
                poll_confluence_once
            )

        except Exception as exc:
            print(
                "Confluence polling failed:",
                type(exc).__name__,
                str(exc),
            )

            traceback.print_exc()

        try:
            await asyncio.wait_for(
                confluence_poll_stop_event.wait(),
                timeout=(
                    CONFLUENCE_POLL_INTERVAL_SECONDS
                ),
            )

        except asyncio.TimeoutError:
            pass

# JIRA poll loop
async def jira_poll_loop():
    while not (
        jira_poll_stop_event.is_set()
    ):
        try:
            await asyncio.to_thread(
                poll_jira_once
            )

        except Exception as exc:
            print(
                "JIRA polling failed:",
                type(exc).__name__,
                str(exc),
            )

            traceback.print_exc()

        try:
            await asyncio.wait_for(
                jira_poll_stop_event.wait(),
                timeout=(
                    JIRA_POLL_INTERVAL_SECONDS
                ),
            )

        except asyncio.TimeoutError:
            pass

# Search for all JIRA Issues for the given JQL Query
def search_jira_issues(
    jql: str,
    next_page_token: str | None = None,
    max_results: int = 100,
) -> dict:

    url = (
        f"{JIRA_BASE_URL}"
        "/rest/api/3/search/jql"
    )

    payload = {
        "jql": jql,
        "maxResults": max_results,
        "fields": [
            "summary",
            "description",
            "status",
            "priority",
            "assignee",
            "reporter",
            "created",
            "updated",
            "resolutiondate",
            "labels",
            "components",
            "issuetype",
            "issuelinks",
            "parent",
        ],
    }

    if next_page_token:
        payload[
            "nextPageToken"
        ] = next_page_token


    response = requests.post(
        url,
        headers=jira_headers(),
        auth=jira_auth(),
        json=payload,
        timeout=60,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            "Jira search failed: "
            f"{response.status_code} - "
            f"{response.text}"
        )

        response.raise_for_status()

    return response.json()

# Fetch all JIRA changes happened under a project 
def fetch_changed_jira_issues(
    project_key: str,
    last_jira_updated_at: str | None,
) -> list[dict]:

    if last_jira_updated_at:

        dt = datetime.strptime(last_jira_updated_at, "%Y-%m-%dT%H:%M:%S.%f%z",)
        jql_timestamp = dt.strftime("%Y-%m-%d %H:%M")
        jql = (
            f'project = "{project_key}" '
            f'AND updated > "{jql_timestamp}" '
            f'ORDER BY updated ASC'
        )

    else:

        jql = (
            f'project = "{project_key}" '
            f'ORDER BY updated ASC'
        )

    print(
        "Jira polling JQL:",
        jql,
    )

    issues = []

    next_page_token = None

    while True:

        result = search_jira_issues(
            jql=jql,
            next_page_token=next_page_token,
            max_results=100,
        )

        page_issues = result.get(
            "issues",
            [],
        )

        if not isinstance(
            page_issues,
            list,
        ):
            page_issues = []

        issues.extend(
            page_issues
        )

        next_page_token = result.get(
            "nextPageToken"
        )

        if not next_page_token:
            break

    print(
        f"Jira returned {len(issues)} "
        f"changed issues for project "
        f"{project_key}"
    )

    return issues

# Poll a JIRA project to look for changes
def poll_jira_project(notebook_id: str, project_key: str,) -> dict:

    print("==========================================")

    print(f"Polling Jira project: {project_key}")

    print("==========================================")

    # ----------------------------------------------------------
    # Existing stable source ID
    # ----------------------------------------------------------

    source_id = get_or_create_jira_source_id(
        notebook_id=notebook_id,
        project_key=project_key,
    )

    # ----------------------------------------------------------
    # Existing sync state
    # ----------------------------------------------------------

    sync_state = get_jira_sync_state(
        notebook_id=notebook_id,
        project_key=project_key,
    )

    last_jira_updated_at = sync_state.get(
        "last_jira_updated_at"
    )

    print(
        "Last Jira Issue Updated :",
        last_jira_updated_at,
    )

    # ----------------------------------------------------------
    # Fetch changed issues
    # ----------------------------------------------------------

    issues = fetch_changed_jira_issues(
        project_key=project_key,
        last_jira_updated_at=last_jira_updated_at,
    )

    indexed_count = 0

    failed_count = 0

    newest_updated_at = last_jira_updated_at

    if not issues:
        set_jira_sync_state(
            notebook_id=notebook_id,
            project_key=project_key,
            state={
                "last_jira_updated_at": newest_updated_at,
                "source_id": source_id,
            },
        )

        return {
            "status": "success",
            "project_key": project_key,
            "source_id": source_id,
            "issues_found": 0,
            "issues_indexed": 0,
            "relationships_created": 0,
        }

    # ==========================================================
    # Build KnowledgeRecords
    # ==========================================================

    knowledge_records = []

    # ----------------------------------------------------------
    # Process issues
    # ----------------------------------------------------------

    for issue in issues:

        jira_issue_updated = issue["fields"]["updated"]

        if (last_jira_updated_at and jira_issue_updated <= last_jira_updated_at):
            continue

        issue_key = (
            issue.get(
                "key",
                "",
            )
            or ""
        )

        issue_id = (
            issue.get(
                "id",
                "",
            )
            or ""
        )

        fields = (
            issue.get(
                "fields",
                {},
            )
            or {}
        )

        updated_at = (
            fields.get(
                "updated",
                "",
            )
            or ""
        )

        print(
            "Processing Jira issue:",
            issue_key,
            "id=",
            issue_id,
            "updated=",
            updated_at,
        )

        try:

            # --------------------------------------------------
            # Build KnowledgeRecord using your existing method.
            # --------------------------------------------------

            knowledge_record = (
                jira_issue_to_knowledge_record(
                    notebook_id=notebook_id,
                    project_key=project_key,
                    issue=issue,
                )
            )

            # --------------------------------------------------
            # Now, Index the JIRA Issue without creating relationships
            # --------------------------------------------------

            result = (
                knowledge_service.index_record(
                    record=knowledge_record,
                    source_name=(
                        f"Jira Service Management - "
                        f"{project_key}"
                    ),
                    index_relationships=False,
                )
            )

            # Append the KnowledgeRecord to knowledge_records array
            knowledge_records.append(knowledge_record)

            indexed_count += 1

            # --------------------------------------------------
            # Track newest update timestamp.
            # --------------------------------------------------

            if updated_at:
                if (newest_updated_at is None or updated_at > newest_updated_at):
                    newest_updated_at = updated_at
                    print(f"******** New value of Updated for {project_key} is {newest_updated_at} *********")

        except Exception as exc:

            failed_count += 1

            print(
                f"Failed to index Jira issue "
                f"{issue_key}: "
                f"{type(exc).__name__}: {exc}"
            )

    print("********** PASS 1 Complete **********")
    # Now, re-process the KnowledgeRecords to create the Relationships

    relationships_created = 0

    for record in knowledge_records:

        try:

            # First, remove all existing relationships for the given record
            relationship_service.remove_relationships_for_record(
                record_id=record.record_id,
                source="jira-issue-link",
            )

            print("********** Removed existing relationships  **********")

            # Now, create fresh relationships for the record
            relationship_service.index_record(
                record=record,
                record_identity_service=(
                    record_identity_service
                ),
            )

            relationships_created += (
                result.get(
                    "relationships_created",
                    0,
                )
            )

            print(
                f"PASS 2 complete: "
                f"{record.external_id}"
            )

        except Exception as exc:

            print(
                f"PASS 2 failed for "
                f"{record.external_id}: "
                f"{type(exc).__name__}: {exc}"
            )

    # ----------------------------------------------------------
    # Advance polling watermark only after processing.
    #
    # We use "now" rather than the newest Jira timestamp so
    # the next poll is based on our actual polling boundary.
    # ----------------------------------------------------------

    new_state = {
        "last_jira_updated_at": (
            newest_updated_at
        ),
        "source_id": source_id,
    }

    set_jira_sync_state(
        notebook_id=notebook_id,
        project_key=project_key,
        state=new_state,
    )

    result = {
        "status": "success",
        "notebook_id": notebook_id,
        "project_key": project_key,
        "source_id": source_id,
        "issues_found": len(issues),
        "issues_indexed": indexed_count,
        "issues_failed": failed_count,
    }

    print(
        "Jira poll result:",
        result,
    )

    return result

# Poll JIRA once
def poll_jira_once():

    print(
        "**** Starting JIRA Polling Process ****"
    )

    source_ids = load_jira_source_ids()

    if not source_ids:
        print(
            "JIRA polling skipped: "
            "no active JIRA sources."
        )
        return

    results = []

    notebook_id = get_notebook_id()

    prefix = (
        f"{notebook_id}:project:"
    )

    for key in source_ids:

        if not key.startswith(prefix):
            continue

        project_key = key[
            len(prefix):
        ]

        try:

            print("******** Calling Poll JIRA Project *********")
            result = poll_jira_project(
                notebook_id=notebook_id,
                project_key=project_key,
            )
            print("******** Called Poll JIRA Project *********")

            results.append(
                result
            )

        except Exception as exc:

            print(
                f"Jira polling failed for "
                f"{project_key}: "
                f"{type(exc).__name__}: {exc}"
            )

            results.append(
                {
                    "status": "failed",
                    "project_key": project_key,
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            )

    print(
        f"**** JIRA Polling Complete. "
        f"**** Results {results} ******"
    )

# Load JIRA sync state
def load_jira_sync_state() -> dict:
    if not os.path.exists(
        JIRA_SYNC_STATE_FILE
    ):
        return {}

    with open(
        JIRA_SYNC_STATE_FILE,
        "r",
        encoding="utf-8",
    ) as file:
        data = json.load(file)

    return (
        data
        if isinstance(data, dict)
        else {}
    )

# Get JIRA sync state for a given project key
def get_jira_sync_state(
    notebook_id: str,
    project_key: str,
) -> dict:

    data = load_jira_sync_state()

    key = (
        f"{notebook_id}:"
        f"project:"
        f"{project_key}"
    )

    state = data.get(
        key,
        {},
    )

    return (
        state
        if isinstance(state, dict)
        else {}
    )

# Set JIRA sync state for a project key
def set_jira_sync_state(
    notebook_id: str,
    project_key: str,
    state: dict,
):

    print("********* LOADING JIRA SYNC STATE **********")
    data = load_jira_sync_state()
    print(f"********* JIRA SYNC STATE -> {data} **********")

    key = (
        f"{notebook_id}:"
        f"project:"
        f"{project_key}"
    )

    data[key] = state

    print(f"********* NEW JIRA SYNC STATE -> {data} **********")

    save_jira_sync_state(
        data
    )

# Save JIRA sync state
def save_jira_sync_state(
    data: dict,
):

    print(f"********* SAVING JIRA SYNC STATE -> {data} ***********")
    temp_file = (
        f"{JIRA_SYNC_STATE_FILE}.tmp"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            indent=2,
        )

    os.replace(
        temp_file,
        JIRA_SYNC_STATE_FILE,
    )

# Get last polling date/time for JIRA
def get_jira_last_polled_at(
    notebook_id: str,
    project_key: str,
) -> str | None:

    data = load_jira_sync_state()

    key = (
        f"{notebook_id}:"
        f"project:"
        f"{project_key}"
    )

    project_state = data.get(
        key,
        {},
    )

    return project_state.get(
        "last_polled_at"
    )

# Set last polling date/time for JIRA
def set_jira_last_polled_at(
    notebook_id: str,
    project_key: str,
    timestamp: str,
):
    data = load_jira_sync_state()

    key = (
        f"{notebook_id}:"
        f"project:"
        f"{project_key}"
    )

    data[key] = {
        "last_polled_at": timestamp
    }

    save_jira_sync_state(
        data
    )

async def servicenow_poll_loop():
    while not servicenow_poll_stop_event.is_set():
        try:
            await asyncio.to_thread(
                poll_servicenow_once
            )
        except Exception as exc:
            print(
                "ServiceNow polling failed:",
                exc,
            )

        try:
            await asyncio.wait_for(
                servicenow_poll_stop_event.wait(),
                timeout=SERVICENOW_POLL_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass

def split_text_for_embedding(
    text: str,
    max_chars: int = MAX_EMBED_CHUNK_CHARS,
    overlap: int = MAX_EMBED_SUBCHUNK_OVERLAP,
):
    text = clean_text(text or "")

    if len(text) <= max_chars:
        return [text]

    parts = []
    start = 0

    while start < len(text):
        end = min(start + max_chars, len(text))
        parts.append(text[start:end])

        if end >= len(text):
            break

        start = max(0, end - overlap)

    return parts

def text_hash(text: str) -> str:
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()

def is_supported_directory_file(path: str) -> bool:
    if not path:
        return False

    filename = os.path.basename(path).lower()
    relative = path.lower()

    if (
        filename.startswith(".")
        or filename.startswith("~$")
        or filename in {".ds_store", ".localized"}
    ):
        return False

    if any(part.startswith(".") or part.startswith("~$") for part in relative.split("/")):
        return False

    _, ext = os.path.splitext(filename)
    return ext in SUPPORTED_DIRECTORY_EXTENSIONS

def index_single_uploaded_file(
    *,
    file_path: str,
    original_filename: str,
    notebook_id: str,
    relative_path: str | None = None,
):
    source_id = str(uuid.uuid4())

    source_name = (
        original_filename
        or os.path.basename(file_path)
        or "Untitled"
    )

    # ----------------------------------------------------------
    # Existing extraction logic
    # ----------------------------------------------------------

    pages, source_type = extract_uploaded_file(
        file_path,
        original_filename,
    )

    if not pages:
        return {
            "notebook_id": notebook_id,
            "source_id": source_id,
            "source_name": source_name,
            "source_type": source_type,
            "chunks_indexed": 0,
            "pages_indexed": 0,
        }

    # ----------------------------------------------------------
    # Folder/file path metadata
    # ----------------------------------------------------------

    if relative_path:
        for page in pages:
            page["file_path"] = relative_path

    # ----------------------------------------------------------
    # Index each logical page/section as a KnowledgeRecord
    # ----------------------------------------------------------

    total_chunks = 0
    total_pages = 0

    for index, page in enumerate(pages, start=1):
        content = (
            page.get("text")
            or ""
        ).strip()

        if not content:
            continue

        page_number = page.get(
            "page",
            index,
        )

        file_path_value = (
            page.get("file_path")
            or relative_path
            or original_filename
        )

        # For a normal file, keep one logical record for the file.
        # For multi-page formats, use page number to make the
        # record ID stable and independently re-indexable.
        record_id = (
            f"file:{source_id}:{page_number}"
        )

        record = KnowledgeRecord(
            record_id=record_id,
            source_id=source_id,
            source_type=source_type,
            entity_type="file",
            external_id=file_path_value,
            title=source_name,
            locator=file_path_value,
            content=content,
            content_hash=text_hash(content),
            updated_at=None,
        )

        result = knowledge_service.index_record(
            record=record,
            source_name=source_name,
        )

        if result.get("status") == "indexed":
            total_chunks += result.get(
                "chunks_indexed",
                0,
            )
            total_pages += 1

    return {
        "notebook_id": notebook_id,
        "source_id": source_id,
        "source_name": source_name,
        "source_type": source_type,
        "chunks_indexed": total_chunks,
        "pages_indexed": total_pages,
    }

class DirectoryIngestRequest(BaseModel):
    notebook_id: str | None = None

def create_job(job_type: str, notebook_id: str, source_name: str):
    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "type": job_type,
            "status": "queued",
            "notebook_id": notebook_id,
            "source_name": source_name,
            "progress": 0,
            "message": "Queued",
            "result": None,
            "error": None,
        }
    return job_id


def update_job(job_id: str, **fields):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(fields)

@app.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

    if not job:
        return {"error": "Job not found"}

    return job

def get_directory_root_name(relative_paths: list[str]) -> str:
    for path in relative_paths:
        normalized = (path or "").replace("\\", "/")
        parts = [p for p in normalized.split("/") if p]
        if len(parts) > 1 and parts[0]:
            return parts[0]

    return "Uploaded Folder"

def process_directory_job(
    job_id: str,
    notebook_id: str,
    source_name: str,
    saved_files: list[dict],
):
    try:
        total = len(saved_files)
        results = []
        indexed_count = 0

        if total == 0:
            update_job(
                job_id,
                status="failed",
                progress=100,
                message="No files found in the folder.",
                error="No files found in the folder.",
            )
            return

        update_job(
            job_id,
            status="running",
            progress=5,
            message="Indexing files in the background...",
        )

        for index, item in enumerate(saved_files, start=1):
            temp_path = item["temp_path"]
            relative_path = item["relative_path"]
            original_filename = item["original_filename"]

            try:
                result = index_single_uploaded_file(
                    file_path=temp_path,
                    original_filename=original_filename,
                    notebook_id=notebook_id,
                    relative_path=relative_path,
                )

                results.append(result)

                if result.get("chunks_indexed", 0) > 0:
                    indexed_count += 1

            except Exception as exc:
                print(f"Skipping {relative_path}: {exc}")

            finally:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

            progress = 5 + int((index / total) * 85)

            update_job(
                job_id,
                status="running",
                progress=min(progress, 95),
                message=f"Processed {index}/{total} files",
            )

        update_job(
            job_id,
            status="done",
            progress=100,
            message="Folder indexing complete.",
            result={
                "notebook_id": notebook_id,
                "source_name": source_name,
                "files_indexed": indexed_count,
                "pages_indexed": sum(
                    item.get("pages_indexed", 0) for item in results
                ),
                "chunks_indexed": sum(
                    item.get("chunks_indexed", 0) for item in results
                ),
                "results": results,
            },
        )

    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            progress=100,
            message="Folder indexing failed.",
            error=f"{type(exc).__name__}: {exc}",
        )


SERVICENOW_TABLE_CONFIG = {
    "incident": {
        "display_name": "ServiceNow Incidents",
        "fields": [
            "sys_id",
            "number",
            "short_description",
            "description",
            "close_notes",
            "state",
            "priority",
            "severity",
            "impact",
            "urgency",
            "category",
            "subcategory",
            "assignment_group",
            "assigned_to",
            "caller_id",
            "opened_at",
            "resolved_at",
            "closed_at",
            "sys_updated_on",
        ],
    },
    "problem": {
        "display_name": "ServiceNow Problems",
        "fields": [
            "sys_id",
            "number",
            "short_description",
            "description",
            "known_error",
            "workaround",
            "cause_notes",
            "fix_notes",
            "state",
            "priority",
            "impact",
            "urgency",
            "assignment_group",
            "assigned_to",
            "opened_at",
            "resolved_at",
            "closed_at",
            "sys_updated_on",
        ],
    },
    "change_request": {
        "display_name": "ServiceNow Change Requests",
        "fields": [
            "sys_id",
            "number",
            "short_description",
            "description",
            "justification",
            "implementation_plan",
            "risk_and_impact_analysis",
            "backout_plan",
            "test_plan",
            "close_notes",
            "type",
            "state",
            "risk",
            "impact",
            "priority",
            "assignment_group",
            "assigned_to",
            "requested_by",
            "start_date",
            "end_date",
            "closed_at",
            "sys_updated_on",
        ],
    },
}


def get_servicenow_settings():
    if not SERVICENOW_INSTANCE or not SERVICENOW_USERNAME or not SERVICENOW_PASSWORD:
        raise RuntimeError(
            "Missing ServiceNow config. Set SERVICENOW_INSTANCE, SERVICENOW_USERNAME, and SERVICENOW_PASSWORD."
        )
    return SERVICENOW_INSTANCE, SERVICENOW_USERNAME, SERVICENOW_PASSWORD


def clean_servicenow_html(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return clean_text(soup.get_text(separator=" "))


def servicenow_value(record: dict, field: str) -> str:
    value = record.get(field)
    if value is None:
        return ""

    if isinstance(value, dict):
        return str(value.get("display_value") or value.get("value") or "").strip()

    return str(value).strip()


def clean_servicenow_field(record: dict, field: str) -> str:
    return clean_servicenow_html(servicenow_value(record, field))

# ------------------------------------------------------------
# The following code is commented out since we are no longer
# using User/Password based authentication for ServiceNow
# Instead, we are using OAuth-based authentication
# ------------------------------------------------------------
"""
def fetch_servicenow_records(
    table: str,
    encoded_query: str | None = None,
    page_size: int = 100,
    last_updated: str | None = None,
):
    if table not in SERVICENOW_TABLE_CONFIG:
        raise ValueError(
            f"Unsupported ServiceNow table: {table}"
        )

    instance, username, password = get_servicenow_settings()
    config = SERVICENOW_TABLE_CONFIG[table]

    base_url = f"{instance}/api/now/table/{table}"


    credentials = f"{username}:{password}"
    credentials_bytes = credentials.encode("utf-8")
    base64_bytes = base64.b64encode(credentials_bytes)
    base64_string = base64_bytes.decode("utf-8")

    all_records = []
    offset = 0

    while True:
        params = {
            "sysparm_limit": page_size,
            "sysparm_offset": offset,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
            "sysparm_fields": ",".join(
                config["fields"]
            ),
            "sysparm_order_by": "sys_updated_on",
        }

        if last_updated:
            query_value = (
                f"sys_updated_on>{last_updated}"
            )

            # Encode the query exactly as Postman does:
            #   >  -> %3E
            #   space -> %20
            #   : remains :
            encoded_query_value = quote(
                query_value,
                safe=":",
            )

            query_params = "&".join(
                [
                    f"{key}={quote(str(value), safe=':,')}"
                    for key, value in params.items()
                ]
            )

            url = (
                f"{base_url}?"
                f"{query_params}"
                f"&sysparm_query={encoded_query_value}"
            )

        else:
            if encoded_query:
                params["sysparm_query"] = encoded_query

            query_params = "&".join(
                [
                    f"{key}={quote(str(value), safe=':,')}"
                    for key, value in params.items()
                ]
            )

            url = f"{base_url}?{query_params}"

        print(
            f"ServiceNow {table} URL: {url}"
        )

        response = requests.get(
            url,
            headers = {"Authorization": f"Basic {base64_string}", "X-WantSessionDebugMessages": "true"},
            timeout=60,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"ServiceNow {table} API failed "
                f"with status {response.status_code}: "
                f"{response.text}"
            )

        records = (
            response.json().get("result", [])
        )

        print(
            f"ServiceNow {table}: received "
            f"{len(records)} records "
            f"(total={len(all_records) + len(records)})"
        )

        if not records:
            break

        all_records.extend(records)

        if len(records) < page_size:
            break

        offset += page_size

    print(
        f"ServiceNow {table}: "
        f"total records fetched = "
        f"{len(all_records)}"
    )

    return all_records

"""

def fetch_servicenow_records(
    table: str,
    encoded_query: str | None = None,
    page_size: int = 100,
    last_updated: str | None = None,
):
    """
    Fetch ServiceNow records using OAuth 2.0.

    Supports:
        - Initial ingestion
        - Incremental polling
        - Pagination
        - ServiceNow table configuration
    """

    if table not in SERVICENOW_TABLE_CONFIG:
        raise ValueError(
            f"Unsupported ServiceNow table: {table}"
        )

    if page_size <= 0:
        raise ValueError(
            "page_size must be greater than zero."
        )

    config = SERVICENOW_TABLE_CONFIG[table]

    all_records = []
    offset = 0

    while True:

        params = {
            "sysparm_limit": page_size,
            "sysparm_offset": offset,
            "sysparm_display_value": "false",
            "sysparm_exclude_reference_link": "true",
            "sysparm_fields": ",".join(
                config["fields"]
            ),
        }

        # ------------------------------------------------------
        # Incremental polling
        # ------------------------------------------------------

        if last_updated:

            params["sysparm_query"] = (
                f"sys_updated_on>{last_updated}"
                "^ORDERBYsys_updated_on"
            )

        # ------------------------------------------------------
        # Initial ingestion with optional query
        # ------------------------------------------------------

        elif encoded_query:

            params["sysparm_query"] = encoded_query

        # ------------------------------------------------------
        # Call ServiceNow using OAuth
        # ------------------------------------------------------

        servicenow_connector = get_servicenow_connector()

        result = servicenow_connector.get(
            path=f"/api/now/table/{table}",
            params=params,
        )

        records = result.get(
            "result",
            [],
        )

        if not isinstance(records, list):
            raise RuntimeError(
                "Unexpected ServiceNow API response."
            )

        print(
            f"ServiceNow {table}: "
            f"received {len(records)} records"
        )

        if not records:
            break

        all_records.extend(records)

        if len(records) < page_size:
            break

        offset += page_size

    print(
        f"ServiceNow {table}: "
        f"total records fetched = {len(all_records)}"
    )

    return all_records


def build_incident_text(record: dict) -> str:
    return f"""
SERVICENOW INCIDENT

Number: {servicenow_value(record, "number")}
Short description: {clean_servicenow_field(record, "short_description")}
Description: {clean_servicenow_field(record, "description")}

State: {servicenow_value(record, "state")}
Priority: {servicenow_value(record, "priority")}
Severity: {servicenow_value(record, "severity")}
Impact: {servicenow_value(record, "impact")}
Urgency: {servicenow_value(record, "urgency")}

Category: {servicenow_value(record, "category")}
Subcategory: {servicenow_value(record, "subcategory")}

Assignment group: {servicenow_value(record, "assignment_group")}
Assigned to: {servicenow_value(record, "assigned_to")}
Caller: {servicenow_value(record, "caller_id")}

Opened: {servicenow_value(record, "opened_at")}
Resolved: {servicenow_value(record, "resolved_at")}
Closed: {servicenow_value(record, "closed_at")}
Updated: {servicenow_value(record, "sys_updated_on")}

Close notes:
{clean_servicenow_field(record, "close_notes")}
""".strip()


def build_problem_text(record: dict) -> str:
    return f"""
SERVICENOW PROBLEM

Number: {servicenow_value(record, "number")}
Short description: {clean_servicenow_field(record, "short_description")}
Description: {clean_servicenow_field(record, "description")}

State: {servicenow_value(record, "state")}
Priority: {servicenow_value(record, "priority")}
Impact: {servicenow_value(record, "impact")}
Urgency: {servicenow_value(record, "urgency")}

Known error: {servicenow_value(record, "known_error")}

Cause notes:
{clean_servicenow_field(record, "cause_notes")}

Workaround:
{clean_servicenow_field(record, "workaround")}

Fix notes:
{clean_servicenow_field(record, "fix_notes")}

Assignment group: {servicenow_value(record, "assignment_group")}
Assigned to: {servicenow_value(record, "assigned_to")}

Opened: {servicenow_value(record, "opened_at")}
Resolved: {servicenow_value(record, "resolved_at")}
Closed: {servicenow_value(record, "closed_at")}
Updated: {servicenow_value(record, "sys_updated_on")}
""".strip()


def build_change_request_text(record: dict) -> str:
    return f"""
SERVICENOW CHANGE REQUEST

Number: {servicenow_value(record, "number")}
Short description: {clean_servicenow_field(record, "short_description")}
Description: {clean_servicenow_field(record, "description")}

Type: {servicenow_value(record, "type")}
State: {servicenow_value(record, "state")}
Risk: {servicenow_value(record, "risk")}
Impact: {servicenow_value(record, "impact")}
Priority: {servicenow_value(record, "priority")}

Justification:
{clean_servicenow_field(record, "justification")}

Implementation plan:
{clean_servicenow_field(record, "implementation_plan")}

Risk and impact analysis:
{clean_servicenow_field(record, "risk_and_impact_analysis")}

Test plan:
{clean_servicenow_field(record, "test_plan")}

Backout plan:
{clean_servicenow_field(record, "backout_plan")}

Close notes:
{clean_servicenow_field(record, "close_notes")}

Assignment group: {servicenow_value(record, "assignment_group")}
Assigned to: {servicenow_value(record, "assigned_to")}
Requested by: {servicenow_value(record, "requested_by")}

Planned start: {servicenow_value(record, "start_date")}
Planned end: {servicenow_value(record, "end_date")}
Closed: {servicenow_value(record, "closed_at")}
Updated: {servicenow_value(record, "sys_updated_on")}
""".strip()


def extract_servicenow_record_pages(table: str, encoded_query: str | None = None, page_size: int = 100):
    records = fetch_servicenow_records(
        table=table,
        encoded_query=encoded_query,
        page_size=page_size,
    )

    pages = []

    for index, record in enumerate(records, start=1):
        sys_id = servicenow_value(record, "sys_id")
        number = servicenow_value(record, "number")

        if table == "incident":
            content = build_incident_text(record)
        elif table == "problem":
            content = build_problem_text(record)
        elif table == "change_request":
            content = build_change_request_text(record)
        else:
            continue

        if not content.strip():
            continue

        pages.append(
            {
                "page": index,
                "file_path": f"servicenow/{table}/{sys_id}",
                "source_type": "servicenow",
                "servicenow_table": table,
                "servicenow_sys_id": sys_id,
                "servicenow_number": number,
                "servicenow_updated_on": servicenow_value(record, "sys_updated_on"),
                "text": content,
            }
        )

    return pages

def index_servicenow_table(
    notebook_id: str,
    table: str,
    query: str | None,
    page_size: int = 100,
):
    newest_updated = ""
    pages = extract_servicenow_record_pages(
        table=table,
        encoded_query=query,
        page_size=page_size,
    )

    if not pages:
        return {
            "table": table,
            "records_indexed": 0,
            "chunks_indexed": 0,
            "pages_indexed": 0,
            "source_id": "",
            "source_name": "",
            "_records": [],
        }

    # Keep a stable source ID for each ServiceNow table.
    source_id = get_or_create_servicenow_source_id(
        notebook_id,
        table,
    )

    source_name = SERVICENOW_TABLE_CONFIG[table][
        "display_name"
    ]

    records = []
    total_chunks_indexed = 0
    records_indexed = 0

    # --------------------------------------------------------------
    # NEW:
    # Instead of passing the entire pages array to
    # add_source_to_collection(), convert each ServiceNow page/
    # record into a KnowledgeRecord.
    # --------------------------------------------------------------

    for page in pages:
        sys_id = (
            page.get("servicenow_sys_id")
            or ""
        )

        number = (
            page.get("servicenow_number")
            or ""
        )

        updated_on = (
            page.get("servicenow_updated_on")
            or ""
        )

        content = (
            page.get("text")
            or ""
        ).strip()

        if not content:
            continue

        # Use sys_id whenever available because it is the stable
        # ServiceNow record identifier.
        if sys_id:
            record_id = (
                f"servicenow:{table}:{sys_id}"
            )
        else:
            record_id = str(uuid.uuid4())

        updated_at = None

        if updated_on:
            try:
                updated_at = datetime.strptime(
                    updated_on,
                    "%Y-%m-%d %H:%M:%S",
                )
            except ValueError:
                updated_at = None

        knowledge_record = KnowledgeRecord(
            record_id=record_id,
            source_id=source_id,
            source_type="servicenow",
            entity_type=table,
            external_id=sys_id or None,
            title=number or None,
            locator=(
                page.get("file_path")
                or f"servicenow/{table}/{sys_id}"
            ),
            content=content,
            content_hash=text_hash(content),
            updated_at=updated_at,

            # These require the ServiceNow fields we added
            # to KnowledgeRecord.
            servicenow_table=table,
            servicenow_sys_id=sys_id or None,
            servicenow_number=number or None,
        )

        # We will first Index all the ServiceNow records
        # The relationships will be created in the second pass
        # This is because while indexing records, KnowledgeService 
        # will create unique identifiers for each ServiceNow record
        # that will be used during relationship building
        result = knowledge_service.index_record(
            record=knowledge_record,
            source_name=source_name,
            index_relationships=False,
        )

        if result.get("status") == "indexed":
            records.append(knowledge_record)
            records_indexed += 1

            total_chunks_indexed += result.get(
                "chunks_indexed",
                0,
            )

            if updated_on and (
                not newest_updated
                or updated_on > newest_updated
            ):
                newest_updated = updated_on

    if newest_updated:
        sync_state = load_servicenow_sync_state()

        sync_state[table] = {
            "last_updated": newest_updated,
        }

        save_servicenow_sync_state(
            sync_state
        )

        print(
            f"ServiceNow {table}: "
            f"initial sync checkpoint saved: "
            f"{newest_updated}"
        )

    return {
        "table": table,
        "records_indexed": records_indexed,
        "chunks_indexed": total_chunks_indexed,

        # Keep this for compatibility with your existing response.
        # One ServiceNow record was previously represented as a page.
        "pages_indexed": records_indexed,

        "source_id": source_id,
        "source_name": source_name,
        "_records": records,
    }

# Intial ingestion of Salesforce records
@app.post("/ingest-salesforce")
def ingest_salesforce():
    try:

        print(
            "=========================================="
        )
        print(
            "******** Calling Salesforce Initial Ingestion ********"
        )
        print(
            "=========================================="
        )

        connector = get_salesforce_connector()

        result = connector.initial_sync(notebook_id=get_notebook_id())

        print(
            "Salesforce initial ingestion completed:"
        )

        print(
            result
        )

        return result

    except Exception as exc:

        print(
            "Salesforce ingestion failed:",
            type(exc).__name__,
            str(exc),
        )

        traceback.print_exc()

        raise HTTPException(
            status_code=500,
            detail=(
                "Salesforce ingestion failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

# Initial ingestion of ServiceNow records
@app.post("/ingest-servicenow")
def ingest_servicenow(req: ServiceNowIngestRequest):

    """
    Perform a full ServiceNow ingestion.

    Indexes these tables separately:
      - incident
      - problem
      - change_request

    Each ServiceNow record becomes one KnowledgeRecord.
    KnowledgeService handles:
      - chunking
      - embeddings
      - deleting/replacing existing record chunks
      - storing chunks in Chroma
    """

    notebook_id = get_notebook_id()

    try:
        table_order = [
            "incident",
            "problem",
            "change_request",
        ]

        results = []
        total_records = 0
        total_chunks = 0
        total_pages = 0
        all_records = []

        # First, we will index all the ServiceNow records
        for table in table_order:
            print(
                f"===== Starting ServiceNow "
                f"{table} ingestion ====="
            )
            table_result = index_servicenow_table(
                notebook_id=notebook_id,
                table=table,
                query=req.query,
                page_size=100,
            )

            records = table_result.get(
                "_records",
                [],
            )

            # We store all records in an array
            # Later, this array will be used to
            # create Relationships

            if records:
                all_records.extend(
                    records
                )

            results.append(table_result)
            total_records += table_result["records_indexed"]
            total_chunks += table_result["chunks_indexed"]
            total_pages += table_result["pages_indexed"]

        if total_records == 0:
            return {
                "error": "No readable ServiceNow records were found."
            }

        # Now, we will create Relationships for each ServiceNow record, wherever applicable
        relationships_created = 0

        print(f"**** Starting creation of Relationships for {len(all_records)} ServiceNow records *****")

        for record in all_records:
            try:
                relationships = (
                    knowledge_service.index_relationships(
                        record
                    )
                )

                created_count = len(
                    relationships or []
                )

                relationships_created += (
                    created_count
                )

                if created_count:
                    print(
                        f"ServiceNow relationships created "
                        f"for {record.record_id}: "
                        f"{created_count}"
                    )
            except Exception as exc:

                print(
                    f"Relationship indexing failed for "
                    f"{record.record_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

                traceback.print_exc()

        print(
            f"**** ServiceNow relationship pass complete: "
            f"{relationships_created} relationships created ****"
        )

        return {
            "status": "indexed",
            "notebook_id": notebook_id,
            "source_name": "ServiceNow",
            "source_type": "servicenow",
            "results": results,
            "records_indexed": total_records,
            "pages_indexed": total_pages,
            "chunks_indexed": total_chunks,
        }

    except Exception as exc:
        print("ServiceNow Exception")
        print(exc)
        return {
            "error": str(exc),
            "source_type": "servicenow",
        }

@app.post("/ingest-directory")
async def ingest_directory(
    background_tasks: BackgroundTasks,
    notebook_id: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
):
    notebook_id = notebook_id or str(uuid.uuid4())

    if not files:
        return {"error": "No files provided."}

    relative_paths = [
        file.filename or f"file_{i + 1}"
        for i, file in enumerate(files)
    ]
    source_name = get_directory_root_name(relative_paths)
    job_id = create_job("directory_index", notebook_id, source_name)

    saved_files = []

    try:
        update_job(
            job_id,
            status="running",
            progress=1,
            message="Saving uploaded folder files...",
        )

        for i, upload in enumerate(files, start=1):
            relative_path = upload.filename or f"file_{i}"
            original_filename = os.path.basename(relative_path) or f"file_{i}"
            safe_name = f"{uuid.uuid4()}_{original_filename}"
            temp_path = os.path.join(UPLOAD_DIR, safe_name)

            try:
                file_bytes = await upload.read()

                with open(temp_path, "wb") as out:
                    out.write(file_bytes)

                saved_files.append(
                    {
                        "temp_path": temp_path,
                        "relative_path": relative_path,
                        "original_filename": original_filename,
                    }
                )
            finally:
                try:
                    await upload.close()
                except Exception:
                    pass

        background_tasks.add_task(
            process_directory_job,
            job_id,
            notebook_id,
            source_name,
            saved_files,
        )

        return {
            "status": "queued",
            "job_id": job_id,
            "notebook_id": notebook_id,
            "message": "Folder indexing started in background.",
        }

    except Exception as exc:
        for item in saved_files:
            try:
                os.remove(item["temp_path"])
            except OSError:
                pass

        update_job(
            job_id,
            status="failed",
            progress=100,
            message="Folder upload failed.",
            error=str(exc),
        )

        return {"error": f"Failed to receive folder upload: {exc}"}

class DeleteAudioRequest(BaseModel):
    audio_url: str


@app.post("/delete-audio")
def delete_audio(req: DeleteAudioRequest):
    filename = os.path.basename(req.audio_url)

    if not filename:
        return {"error": "Invalid audio URL."}

    audio_path = os.path.abspath(os.path.join(AUDIO_DIR, filename))
    audio_root = os.path.abspath(AUDIO_DIR)

    if not audio_path.startswith(audio_root + os.sep):
        return {"error": "Invalid audio path."}

    if os.path.exists(audio_path):
        os.remove(audio_path)

    return {
        "status": "deleted",
        "filename": filename,
    }

def get_used_chunk_hashes(metas):
    return [
        meta.get("chunk_hash")
        for meta in metas
        if meta.get("chunk_hash")
    ]

def is_confluence_url(url: str) -> bool:
    return "atlassian.net/wiki" in url.lower() or "confluence" in url.lower()

def is_confluence_page_url(url: str) -> bool:
    return "/pages/" in url.lower() or "pageId=" in url

def is_confluence_space_url(url: str) -> bool:
    return "/spaces/" in url.lower() and not is_confluence_page_url(url)

def extract_confluence_page_id(url: str) -> str | None:
    # Handles URLs like:
    # /wiki/spaces/ABC/pages/123456/Page+Title
    match = re.search(r"/pages/(\d+)", url)
    if match:
        return match.group(1)

    # Handles URLs like:
    # ?pageId=123456
    match = re.search(r"[?&]pageId=(\d+)", url)
    if match:
        return match.group(1)

    return None


def extract_confluence_page(url: str):
    base_url = os.getenv("CONFLUENCE_BASE_URL")
    email = os.getenv("CONFLUENCE_EMAIL")
    api_token = os.getenv("CONFLUENCE_API_TOKEN")

    if not base_url or not email or not api_token:
        raise RuntimeError(
            "Missing Confluence credentials. Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, and CONFLUENCE_API_TOKEN."
        )

    page_id = extract_confluence_page_id(url)

    if not page_id:
        raise RuntimeError("Could not find Confluence page ID in URL.")

    print("******** Using new URL to get Confluence Page details ********")
    # api_url = f"{base_url}/rest/api/content/{page_id}?expand=body.storage,title"
    api_url = f"{base_url}/api/v2/pages/{page_id}?body-format=storage"
    print(api_url)

    res = requests.get(
        api_url,
        auth=HTTPBasicAuth(email, api_token),
        timeout=30,
    )

    if res.status_code >= 400:
        raise RuntimeError(
            f"Confluence API failed: {res.status_code} - {res.text}"
        )

    data = res.json()

    print(f"************   Content of Confluence Page -> {data} ************ ")
    title = data.get("title", "Confluence Page")
    html = data.get("body", {}).get("storage", {}).get("value", "")

    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(separator=" "))

    print(title)
    print(text)
    if not text:
        return title, []

    updated_at = (
        data.get("version", {})
        .get("createdAt", "")
    )

    print(f"Updated at value is -> {updated_at}")
    space = data.get(
        "space",
        {}
    ) or {}

    space_key = space.get(
        "key",
        ""
    )

    space_name = space.get(
        "name",
        ""
    )

    page_url = (
        f"{base_url}/pages/viewpage.action"
        f"?pageId={page_id}"
    )

    return title, [
        {
            "page": 1,
            "page_id": str(page_id),
            "title": title,
            "file_path": page_url,
            "url": page_url,
            "text": text,
            "updated_at": updated_at,
            "space_key": space_key,
            "space_name": space_name,
        }
    ]


class ChatRequest(BaseModel):
    notebook_id: str
    question: str


class GenerateRequest(BaseModel):
    notebook_id: str
    prompt: str | None = None


class UrlIngestRequest(BaseModel):
    url: str
    notebook_id: str | None = None


class DeleteNotebookRequest(BaseModel):
    notebook_id: str


class UpdateCodeFileRequest(BaseModel):
    notebook_id: str
    repo_full_name: str
    file_path: str

def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()

def extract_docx(file_path: str):
    document = Document(file_path)
    sections = []

    paragraph_text = [
        paragraph.text.strip()
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    ]

    if paragraph_text:
        sections.append("\n".join(paragraph_text))

    for table_number, table in enumerate(document.tables, start=1):
        rows = []

        for row in table.rows:
            values = [
                clean_text(cell.text)
                for cell in row.cells
                if clean_text(cell.text)
            ]

            if values:
                rows.append(" | ".join(values))

        if rows:
            sections.append(
                f"TABLE {table_number}\n" + "\n".join(rows)
            )

    text = clean_text("\n\n".join(sections))

    if not text:
        return []

    return [
        {
            "page": 1,
            "text": text,
        }
    ]


def extract_pptx(file_path: str):
    presentation = Presentation(file_path)
    pages = []

    for slide_number, slide in enumerate(
        presentation.slides,
        start=1,
    ):
        parts = []

        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = clean_text(shape.text)

                if text:
                    parts.append(text)

            if getattr(shape, "has_table", False):
                table_rows = []

                for row in shape.table.rows:
                    values = [
                        clean_text(cell.text)
                        for cell in row.cells
                        if clean_text(cell.text)
                    ]

                    if values:
                        table_rows.append(" | ".join(values))

                if table_rows:
                    parts.append(
                        "TABLE\n" + "\n".join(table_rows)
                    )

        slide_text = clean_text("\n\n".join(parts))

        if slide_text:
            pages.append(
                {
                    "page": slide_number,
                    "text": (
                        f"SLIDE {slide_number}\n\n"
                        f"{slide_text}"
                    ),
                }
            )

    return pages


def extract_xlsx(file_path: str):
    workbook = openpyxl.load_workbook(
        file_path,
        read_only=True,
        data_only=True,
    )

    pages = []

    try:
        for sheet_number, worksheet in enumerate(
            workbook.worksheets,
            start=1,
        ):
            rows = []

            for row in worksheet.iter_rows(values_only=True):
                values = [
                    clean_text(str(value))
                    for value in row
                    if value is not None
                    and clean_text(str(value))
                ]

                if values:
                    rows.append(" | ".join(values))

            if rows:
                pages.append(
                    {
                        "page": sheet_number,
                        "file_path": worksheet.title,
                        "text": (
                            f"EXCEL SHEET: {worksheet.title}\n\n"
                            + "\n".join(rows)
                        ),
                    }
                )
    finally:
        workbook.close()

    return pages

def extract_pdf(file_path: str):
    doc = fitz.open(file_path)
    pages = []

    for i, page in enumerate(doc):
        text = clean_text(page.get_text())
        if text:
            pages.append({"page": i + 1, "text": text})

    return pages

def extract_uploaded_file(
    file_path: str,
    original_filename: str,
):
    filename_lower = (original_filename or "").lower()

    if filename_lower.endswith(".pdf"):
        return extract_pdf(file_path), "pdf"

    if filename_lower.endswith(".txt"):
        return extract_txt(file_path), "txt"

    if filename_lower.endswith(".docx"):
        return extract_docx(file_path), "docx"

    if filename_lower.endswith(".pptx"):
        return extract_pptx(file_path), "pptx"

    if filename_lower.endswith(".xlsx"):
        return extract_xlsx(file_path), "xlsx"

    raise ValueError(
        "Unsupported file type. "
        "Upload PDF, TXT, DOCX, PPTX, or XLSX."
    )



def extract_confluence_space_key(url: str):
    match = re.search(r"/spaces/([^/?]+)", url)

    if match:
        return match.group(1)

    return None

def get_confluence_space_id(
    space_key: str,
) -> str:
    base_url = os.getenv("CONFLUENCE_BASE_URL")
    email = os.getenv("CONFLUENCE_EMAIL")
    api_token = os.getenv("CONFLUENCE_API_TOKEN")

    if not base_url or not email or not api_token:
        raise RuntimeError(
            "Missing Confluence credentials. "
            "Set CONFLUENCE_BASE_URL, "
            "CONFLUENCE_EMAIL and "
            "CONFLUENCE_API_TOKEN."
        )

    api_url = (
        f"{base_url.rstrip('/')}"
        f"/api/v2/spaces"
    )

    response = requests.get(
        api_url,
        params={
            "keys": space_key,
            "limit": 1,
        },
        auth=HTTPBasicAuth(
            email,
            api_token,
        ),
        headers={
            "Accept": "application/json",
        },
        timeout=30,
    )

    if response.status_code >= 400:
        raise RuntimeError(
            f"Confluence space lookup failed: "
            f"{response.status_code} - "
            f"{response.text}"
        )

    data = response.json()

    results = data.get(
        "results",
        [],
    ) or []

    if not results:
        raise RuntimeError(
            f"Confluence space not found "
            f"for key '{space_key}'."
        )

    return str(
        results[0]["id"]
    )

def extract_confluence_space_pages(url: str):
    base_url = os.getenv("CONFLUENCE_BASE_URL")
    email = os.getenv("CONFLUENCE_EMAIL")
    api_token = os.getenv("CONFLUENCE_API_TOKEN")

    if not base_url or not email or not api_token:
        raise RuntimeError(
            "Missing Confluence credentials. "
            "Set CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN."
        )

    space_key = extract_confluence_space_key(url)

    if not space_key:
        raise RuntimeError("Unable to determine Confluence Space Key.")

    print(f"Confluence space key is {space_key}")

    space_id = get_confluence_space_id(space_key)

    print(f"Confluence space id is {space_id}")
    pages = []

    start = 0
    limit = 50

    while True:

        api_url = f"{base_url}/api/v2/spaces/{space_id}/pages?limit={limit}&body-format=storage"
        """
        api_url = (
            f"{base_url}/rest/api/content"
            f"?spaceKey={space_key}"
            f"&type=page"
            f"&expand=body.storage,title"
            f"&start={start}"
            f"&limit={limit}"
        )
        """

        response = requests.get(
            api_url,
            auth=HTTPBasicAuth(email, api_token),
            timeout=30,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                f"Confluence API failed: "
                f"{response.status_code} - {response.text}"
            )

        payload = response.json()

        results = payload.get("results", [])

        if not results:
            break

        print(f"********** HTTP Response for Confluence Space Pages *********** -> {results}") 
        for page in results:

            print(f"********** Confluence Page details -> {page} ************")
            page_title = page.get("title", "Untitled")

            page_id = page.get("id")

            html = (
                page.get("body", {})
                    .get("storage", {})
                    .get("value", "")
            )

            soup = BeautifulSoup(html, "html.parser")

            text = clean_text(
                soup.get_text(separator=" ")
            )

            updated_at = (
                page.get("version", {})
                .get("createdAt", "")
            )

            print(f"********** Confluence Page inside Spaces updated_at value is {updated_at} **********")
            space = (
                page.get(
                    "space",
                    {}
                )
                or {}
            )

            space_name = space.get(
                "name",
                "",
            )

            page_url = (
                f"{base_url}"
                f"/pages/viewpage.action"
                f"?pageId={page_id}"
            )

            if text:

                pages.append(
                    {
                        "page": len(pages) + 1,
                        "page_id": str(
                            page_id
                        ),
                        "title": page_title,
                        "url": page_url,
                        "space_key": space_key,
                        "space_name": space_name,
                        "updated_at": updated_at,
                        "file_path":
                            f"confluence/{space_key}/{page_id}",
                        "text":
                            f"""
CONFLUENCE PAGE: {page_title}

PAGE ID: {page_id}

SPACE: {space_key}

{text}
""",
                    }
                )

        if len(results) < limit:
            break

        start += limit

    return (
        f"Confluence Space - {space_key}",
        pages,
    )

def extract_txt(file_path: str):
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        text = clean_text(f.read())

    if not text:
        return []

    return [{"page": 1, "text": text}]


def extract_website(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; NotebookRAG/1.0)"
    }

    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "header", "footer", "svg"]):
        tag.decompose()

    title = clean_text(soup.title.string) if soup.title and soup.title.string else url
    text = clean_text(soup.get_text(separator=" "))

    if not text:
        return title, []

    return title, [{"page": 1, "text": text}]

def is_sharepoint_url(url: str) -> bool:
    if not url:
        return False

    normalized = url.lower()

    return (
        "sharepoint.com" in normalized
        or "sharepoint-df.com" in normalized
        or "onedrive.com" in normalized
        or "1drv.ms" in normalized
    )

def is_github_repo(url: str):
    return "github.com" in url


def parse_github_repo(url: str):
    parts = url.rstrip("/").split("/")

    owner = parts[-2]
    repo = parts[-1]

    return owner, repo


def fetch_github_repo_contents(repo, path=""):
    collected = []

    contents = repo.get_contents(path)

    for item in contents:
        name = item.name.lower()

        if item.type == "dir":
            if name in IGNORED_FOLDERS:
                continue

            collected.extend(
                fetch_github_repo_contents(repo, item.path)
            )

        elif item.type == "file":
            _, ext = os.path.splitext(name)

            if ext not in ALLOWED_EXTENSIONS:
                continue

            try:
                content = item.decoded_content.decode(
                    "utf-8",
                    errors="ignore",
                )

                collected.append(
                    {
                        "path": item.path,
                        "content": content,
                    }
                )

            except Exception:
                pass

    return collected


def chunk_text(text: str, size: int = 800, overlap: int = 100):
    chunks = []
    start = 0
    text = text.strip()

    while start < len(text):
        end = min(start + size, len(text))
        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end == len(text):
            break

        start += size - overlap

    return chunks


def build_grounded_prompt(
    user_prompt: str,
    context: str,
    output_instruction: str,
) -> str:
    return f"""
You are a grounded research assistant.

Use only the supplied notebook context.
Do not use outside knowledge.
Do not invent details.
If the requested information is not supported by the context, say:
"Not found in the provided sources."

User request:
{user_prompt}

Output instruction:
{output_instruction}

Notebook context:
{context}
"""

def add_source_to_collection(
    notebook_id: str,
    source_id: str,
    source_name: str,
    pages: list[dict],
):
    documents = []
    metadatas = []
    ids = []

    for page_data in pages:
        page_text = page_data.get("text", "")
        if not page_text.strip():
            continue

        chunks = chunk_text(page_text)

        for chunk_index, chunk in enumerate(chunks):
            subchunks = split_text_for_embedding(chunk)

            for subchunk_index, subchunk in enumerate(subchunks):
                if not subchunk.strip():
                    continue

                documents.append(subchunk)
                metadatas.append(
                    {
                        "notebook_id": notebook_id,
                        "source_id": source_id,
                        "source_name": source_name,
                        "file_path": page_data.get("file_path", ""),
                        "file_name": os.path.basename(page_data.get("file_path", "")),
                        "page": page_data.get("page", 1),
                        "chunk_index": chunk_index,
                        "chunk_subindex": subchunk_index,
                        "chunk_hash": text_hash(subchunk),
                        "source_type": page_data.get("source_type", ""),
                        "servicenow_table": page_data.get("servicenow_table", ""),
                        "servicenow_sys_id": page_data.get("servicenow_sys_id", ""),
                        "servicenow_number": page_data.get("servicenow_number", ""),
                        "servicenow_updated_on": page_data.get("servicenow_updated_on", ""),
                    }
                )
                ids.append(str(uuid.uuid4()))

    if not documents:
        return 0, 0

    total_chunks = len(documents)
    added_chunks = 0
    page_count = len(pages)

    batch_documents = []
    batch_metadatas = []
    batch_ids = []
    batch_chars = 0

    def flush_batch():
        nonlocal added_chunks, batch_documents, batch_metadatas, batch_ids, batch_chars

        if not batch_documents:
            return

        print(
            f"Embedding batch: docs={len(batch_documents)}, chars={batch_chars}"
        )

        embeddings = embed_texts(batch_documents)

        collection.add(
            documents=batch_documents,
            embeddings=embeddings,
            metadatas=batch_metadatas,
            ids=batch_ids,
        )

        added_chunks += len(batch_documents)

        batch_documents = []
        batch_metadatas = []
        batch_ids = []
        batch_chars = 0

    for doc, meta, doc_id in zip(documents, metadatas, ids):
        doc_len = len(doc)

        if (
            batch_documents
            and (
                len(batch_documents) >= MAX_EMBED_BATCH_DOCS
                or batch_chars + doc_len > MAX_EMBED_BATCH_CHARS
            )
        ):
            flush_batch()

        batch_documents.append(doc)
        batch_metadatas.append(meta)
        batch_ids.append(doc_id)
        batch_chars += doc_len

    flush_batch()

    return added_chunks, page_count

def get_notebook_context(notebook_id: str, limit: int = 12):
    results = collection.get(
        where={"notebook_id": notebook_id},
        include=["documents", "metadatas"],
    )

    docs = results.get("documents", [])
    metas = results.get("metadatas", [])

    return docs[:limit], metas[:limit]


def github_error_response(exc: Exception):
    message = str(exc)

    if isinstance(exc, RateLimitExceededException):
        return {
            "error": "GitHub rate limit exceeded. Please set GITHUB_TOKEN or try again later.",
            "error_type": "github_rate_limit",
        }

    if isinstance(exc, GithubException):
        return {
            "error": f"GitHub API error: {message}",
            "error_type": "github_api_error",
        }

    return {
        "error": f"Unexpected GitHub error: {message}",
        "error_type": "github_unknown_error",
    }

@app.get("/")
def home():
    return FileResponse("static/role.html")


@app.post("/update-source")
async def update_source(
    source_id: str = Form(...),
    notebook_id: str = Form(...),
    file: UploadFile = File(...),
):

    safe_name = f"{uuid.uuid4()}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    with open(file_path, "wb") as f:
        f.write(await file.read())

    try:
        pages, source_type = extract_uploaded_file(
            file_path=file_path,
            original_filename=file.filename or "",
        )
    except ValueError as exc:
        try:
            os.remove(file_path)
        except OSError:
            pass

        return {"error": str(exc)}
    except Exception as exc:
        try:
            os.remove(file_path)
        except OSError:
            pass

        return {
            "error": f"Unable to read the uploaded file: {exc}"
        }

    if not pages:
        return {
            "error": "No readable text was found in the uploaded file."
        }

    collection.delete(
        where={
            "$and": [
                {"notebook_id": notebook_id},
                {"source_id": source_id},
            ]
        }
    )

    chunks_indexed, pages_indexed = add_source_to_collection(
        notebook_id=notebook_id,
        source_id=source_id,
        source_name=file.filename,
        pages=pages,
    )

    return {
        "notebook_id": notebook_id,
        "source_id": source_id,
        "source_name": file.filename,
        "source_type": source_type,
        "chunks_indexed": chunks_indexed,
        "pages_indexed": pages_indexed,
        "status": "updated",
    }

def count_chunks_for_file(
    notebook_id: str,
    repo_full_name: str,
    file_path: str,
):
    record_id = (
        f"github:"
        f"{repo_full_name}:"
        f"{file_path}"
    )

    result = knowledge_repository.get_record_chunks(
        record_id
    )

    return len(
        result.get(
            "documents",
            [],
        ) or []
    )

@app.post("/update-code-file")
def update_code_file(req: UpdateCodeFileRequest):
    print("Update code file request received")

    try:
        # ------------------------------------------------------
        # 1. Fetch latest file from GitHub
        # ------------------------------------------------------

        repo = github_client.get_repo(
            req.repo_full_name
        )

        file_content = repo.get_contents(
            req.file_path
        )

        content = file_content.decoded_content.decode(
            "utf-8",
            errors="ignore",
        )

    except Exception as exc:
        return github_error_response(exc)

    # ----------------------------------------------------------
    # 2. Existing diagnostic: count current chunks
    # ----------------------------------------------------------

    chunks_before_delete = count_chunks_for_file(
        req.notebook_id,
        req.repo_full_name,
        req.file_path,
    )

    # ----------------------------------------------------------
    # 3. Build the normalized GitHub KnowledgeRecord
    # ----------------------------------------------------------

    full_content = (
        f"FILE: {req.file_path}\n\n"
        f"{content}"
    ).strip()

    if not full_content:
        return {
            "status": "failed",
            "message": "GitHub file contains no content.",
            "notebook_id": req.notebook_id,
            "repo_full_name": req.repo_full_name,
            "file_path": req.file_path,
        }

    # IMPORTANT:
    # This must be the SAME record_id used by your original
    # GitHub ingestion logic for this file.
    record_id = (
        f"github:"
        f"{req.repo_full_name}:"
        f"{req.file_path}"
    )

    # Use a stable source ID for the GitHub repository.
    #
    # If your existing GitHub ingestion already has a stable
    # source_id, use that value instead of repo_full_name.
    source_id = req.repo_full_name

    record = KnowledgeRecord(
        record_id=record_id,
        source_id=source_id,
        source_type="github",
        entity_type="github",
        external_id=req.file_path,
        title=req.file_path,
        locator=(
            f"github://"
            f"{req.repo_full_name}/"
            f"{req.file_path}"
        ),
        content=full_content,
        content_hash=text_hash(
            full_content
        ),
        updated_at=None,
        # These are ServiceNow-specific and therefore left unset.
        servicenow_table=None,
        servicenow_sys_id=None,
        servicenow_number=None,
    )

    # ----------------------------------------------------------
    # 4. Re-index through KnowledgeService
    #
    # This replaces:
    #
    #   collection.delete(...)
    #   add_source_to_collection(...)
    #
    # KnowledgeService:
    #   - deletes old chunks for record_id
    #   - chunks the new content
    #   - generates embeddings
    #   - stores the new chunks in Chroma
    # ----------------------------------------------------------

    result = knowledge_service.index_record(
        record=record,
        source_name=req.repo_full_name,
    )

    if result.get("status") != "indexed":
        return {
            "status": "failed",
            "message": result.get(
                "reason",
                "Unable to index GitHub file.",
            ),
            "notebook_id": req.notebook_id,
            "repo_full_name": req.repo_full_name,
            "file_path": req.file_path,
            "chunks_before_delete": chunks_before_delete,
            "chunks_indexed": result.get(
                "chunks_indexed",
                0,
            ),
        }

    chunks_indexed = result.get(
        "chunks_indexed",
        0,
    )

    # The current endpoint indexes one logical GitHub file.
    pages_indexed = 1

    # ----------------------------------------------------------
    # 5. Existing diagnostic: count chunks after reindex
    # ----------------------------------------------------------

    chunks_after_reindex = count_chunks_for_file(
        req.notebook_id,
        req.repo_full_name,
        req.file_path,
    )

    # ----------------------------------------------------------
    # 6. Response - preserve your existing structure
    # ----------------------------------------------------------

    return {
        "status": "updated",
        "notebook_id": req.notebook_id,
        "repo_full_name": req.repo_full_name,
        "file_path": req.file_path,
        "chunks_before_delete": chunks_before_delete,

        # Kept for backward compatibility.
        # There is no separate delete step anymore.
        "chunks_after_delete": 0,

        "chunks_indexed": chunks_indexed,

        "chunks_after_reindex": chunks_after_reindex,

        "pages_indexed": pages_indexed,
    }

@app.post("/upload")
async def upload_source(
    file: UploadFile = File(...),
    notebook_id: str | None = Form(default=None),
):
    notebook_id = notebook_id or str(uuid.uuid4())
    source_id = str(uuid.uuid4())

    safe_name = f"{uuid.uuid4()}_{file.filename}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    try:
        file_bytes = await file.read()

        with open(file_path, "wb") as f:
            f.write(file_bytes)

        result = index_single_uploaded_file(
            file_path=file_path,
            original_filename=file.filename or "",
            notebook_id=notebook_id,
        )

        return {
            "notebook_id": result["notebook_id"],
            "source_id": result["source_id"],
            "source_name": result["source_name"],
            "source_type": result["source_type"],
            "chunks_indexed": result["chunks_indexed"],
            "pages_indexed": result["pages_indexed"],
        }
    except Exception as exc:
        return {"error": f"Upload failed: {exc}"}

    finally:
        try:
            await file.close()
        except Exception:
            pass

        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass

def index_url_pages(
    source_id: str,
    source_name: str,
    source_type: str,
    url: str,
    pages: list[dict],
):
    total_chunks = 0
    total_pages = 0

    for index, page in enumerate(pages):
        content = (page.get("text") or "").strip()

        if not content:
            continue

        page_number = page.get("page", index + 1)

        # Use a stable ID for each logical page.
        record_id = (
            f"{source_type}:{source_id}:"
            f"{page_number}"
        )

        record_locator = (
            page.get("file_path")
            or page.get("url")
            or url
        )

        record = KnowledgeRecord(
            record_id=record_id,
            source_id=source_id,
            source_type=source_type,
            entity_type="website",
            external_id=record_locator,
            title=(
                page.get("title")
                or source_name
            ),
            locator=record_locator,
            content=content,
            content_hash=text_hash(content),
            updated_at=None,
        )

        result = knowledge_service.index_record(
            record=record,
            source_name=source_name,
        )

        if result.get("status") == "indexed":
            total_chunks += result.get(
                "chunks_indexed",
                0,
            )
            total_pages += 1

    return total_chunks, total_pages

def register_confluence_source(
    source_id: str,
    url: str,
    scope: str,
    space_key: str | None = None,
    page_id: str | None = None,
    source_name: str | None = None,
):
    """
    Register a Confluence source for background polling.

    scope:
        "page"  -> poll only page_id
        "space" -> poll the entire space_key
    """

    if scope not in ("page", "space"):
        raise ValueError(
            f"Invalid Confluence scope: {scope}"
        )

    if scope == "page" and not page_id:
        raise ValueError(
            "page_id is required for a Confluence page source."
        )

    if scope == "space" and not space_key:
        raise ValueError(
            "space_key is required for a Confluence space source."
        )

    sources = load_confluence_sources()

    # ----------------------------------------------------------
    # Check whether this source already exists.
    #
    # Prefer source_id, but also check URL so that adding the
    # same Confluence URL again does not create duplicates.
    # ----------------------------------------------------------

    for source in sources:
        if (
            source.get("source_id") == source_id
            or source.get("url") == url
        ):
            print(f"Registering -> Existing Confluence Source {source_id}")
            source.update({
                "source_id": source_id,
                "url": url,
                "scope": scope,
                "space_key": space_key,
                "page_id": (
                    str(page_id)
                    if page_id
                    else None
                ),
                "source_name": (
                    source_name
                    or source.get(
                        "source_name",
                        "",
                    )
                ),
                "active": True,
            })

            save_confluence_sources(
                sources
            )

            print(
                "Updated Confluence polling source:",
                source_name or url,
            )

            return

    # ----------------------------------------------------------
    # New source
    # ----------------------------------------------------------

    print(f"Registering -> New Confluence Source {source_id}")
    source = {
        "source_id": source_id,
        "url": url,
        "scope": scope,
        "space_key": space_key,
        "page_id": (
            str(page_id)
            if page_id
            else None
        ),
        "source_name": (
            source_name
            or ""
        ),
        "active": True,
    }

    sources.append(source)

    save_confluence_sources(
        sources
    )

    print(
        "Registered Confluence polling source:",
        source_name or url,
    )

def initialize_confluence_sync_state(
    source_id: str,
    last_updated: str = "",
):
    print(f"Value of last_updated is {last_updated}")
    state = load_confluence_sync_state()

    state.setdefault("sources", {})

    # Do not overwrite an existing checkpoint.
    if source_id not in state["sources"]:
        state["sources"][source_id] = {
            "last_updated": last_updated
        }

        save_confluence_sync_state(state)

@app.post("/ingest-url")
def ingest_url(req: UrlIngestRequest):
    notebook_id = req.notebook_id or str(uuid.uuid4())
    source_id = str(uuid.uuid4())

    try:
        source_name = ""
        source_type = "website"
        pages = []

        # Used only for Confluence polling.
        confluence_scope = None
        confluence_space_key = None
        confluence_page_id = None

        if is_sharepoint_url(req.url):
            file_name, file_bytes = download_sharepoint_file(req.url)

            pages, extracted_type = extract_sharepoint_file(
                file_name,
                file_bytes,
            )

            source_name = file_name
            source_type = f"sharepoint-{extracted_type}"

        elif is_github_repo(req.url):
            owner, repo_name = parse_github_repo(req.url)
            repo = github_client.get_repo(f"{owner}/{repo_name}")
            files = fetch_github_repo_contents(repo)

            pages = [
                {
                    "page": 1,
                    "file_path": file["path"],
                    "text": (
                        f"FILE: {file['path']}\n\n"
                        f"{file['content']}"
                    ),
                }
                for file in files
            ]

            source_name = repo.full_name
            source_type = "github"

        elif is_confluence_url(req.url):
            if is_confluence_page_url(req.url):
                print(f"{req.url} is a Confluence Page URL")
                page_title, pages = extract_confluence_page(req.url)
                source_name = page_title
                source_type = "confluence-page"
                confluence_scope = "page"
                confluence_page_id = (
                    extract_confluence_page_id(
                        req.url
                    )
                )
                if not confluence_page_id:
                    return {
                        "error": (
                            "Unable to determine "
                            "Confluence page ID."
                        )
                    }

                # Find the timestamp from the page we just fetched.
                if pages:
                    confluence_latest_updated_at = (
                        pages[0].get(
                            "updated_at",
                            "",
                        )
                        or ""
                    )

            elif is_confluence_space_url(req.url):
                print(f"{req.url} is a Confluence Space URL")
                space_name, pages = extract_confluence_space_pages(req.url)
                source_name = space_name
                source_type = "confluence-space"
                confluence_scope = "space"
                confluence_space_key = (
                    extract_confluence_space_key(
                        req.url
                    )
                )
                if not confluence_space_key:
                    return {
                        "error": (
                            "Unable to determine "
                            "Confluence space key."
                        )
                    }

                # Find the newest page timestamp from the initial
                # full space ingestion.
                confluence_latest_updated_at = ""
                for page in pages:
                    updated_at = (
                        page.get(
                            "updated_at",
                            "",
                        )
                        or ""
                    )

                    if updated_at and (
                        not confluence_latest_updated_at
                        or updated_at
                        > confluence_latest_updated_at
                    ):
                        confluence_latest_updated_at = (
                            updated_at
                        )
                print(f"********** Final value of Confluence Space Page latest_updated_at is {confluence_latest_updated_at}")
            else:
                return {
                    "error": (
                        "Unsupported Confluence URL. "
                        "Provide a Confluence page URL or space URL."
                    )
                }

        else:
            website_title, pages = extract_website(req.url)

            source_name = (
                website_title
                or urlparse(req.url).netloc
                or req.url
            )

            source_type = "website"

        if not source_name:
            source_name = urlparse(req.url).netloc or req.url

        if not pages:
            return {
                "error": "No readable content found in this source."
            }

        chunks_indexed, pages_indexed = index_url_pages(
            source_id=source_id,
            source_name=source_name,
            source_type=source_type,
            url=req.url,
            pages=pages,
        )

        # ----------------------------------------------------------
        # Register Confluence source for polling
        # ----------------------------------------------------------

        if confluence_scope == "page":

            register_confluence_source(
                source_id=source_id,
                url=req.url,
                scope="page",
                space_key=confluence_space_key,
                page_id=confluence_page_id,
                source_name=source_name,
            )

            initialize_confluence_sync_state(
                source_id=source_id,
                last_updated=(
                    confluence_latest_updated_at
                ),
            )


        elif confluence_scope == "space":

            register_confluence_source(
                source_id=source_id,
                url=req.url,
                scope="space",
                space_key=confluence_space_key,
                page_id=None,
            )

            initialize_confluence_sync_state(
                source_id=source_id,
                last_updated=(
                    confluence_latest_updated_at
                ),
            )

        response = {
            "status": "indexed",
            "notebook_id": notebook_id,
            "source_id": source_id,
            "source_name": source_name,
            "source_type": source_type,
            "chunks_indexed": chunks_indexed,
            "pages_indexed": pages_indexed,
            "url": req.url,
        }

        if confluence_scope:
            response["confluence_scope"] = (
                confluence_scope
            )

        if confluence_page_id:
            response["confluence_page_id"] = (
                confluence_page_id
            )

        if confluence_space_key:
            response["confluence_space_key"] = (
                confluence_space_key
            )

        return response

    except Exception as exc:
        return {
            "error": str(exc),
            "source_type": (
                "confluence"
                if is_confluence_url(req.url)
                else "website"
            ),
        }

@app.post("/chat")
def chat(req: ChatRequest):
    try:
        search_result = perform_search(
            query=req.question,
            top_k=8,
        )

        matches = search_result["results"]

        if not matches:
            return {
                "answer": "Not found in the provided sources.",
                "citations": [],
            }

        context_blocks = []

        for index, match in enumerate(matches, start=1,):
            location = (
                match.get("file_path")
                or f"page {match.get('page', 1)}"
            )

            block = [
                f"[Source {index} | "
                f"{match.get('source_name', 'Unknown')} | "
                f"{location}]",
                "",
                "RECORD EVIDENCE",
                f"Record ID: {match.get('record_id', '')}",
                f"Entity Type: {match.get('entity_type', '')}",
                f"Source Type: {match.get('source_type', '')}",
                "",
                "CONTENT:",
                match.get('text', ''),
            ]

            # ----------------------------------------------------------
            # Graph evidence
            # ----------------------------------------------------------

            if match.get("retrieval_source") == "relationship_query":

                block.extend(
                    [
                        "",
                        "GRAPH RELATIONSHIP EVIDENCE:",
                        (
                            "Seed Record ID: "
                            f"{match.get('relationship_seed_record_id', '')}"
                        ),
                        (
                            "Seed Entity Type: "
                            f"{match.get('relationship_seed_entity_type', '')}"
                        ),
                        (
                            "Seed Source Type: "
                            f"{match.get('relationship_seed_source_type', '')}"
                        ),
                        (
                            "Seed External ID: "
                            f"{match.get('relationship_seed_external_id', '')}"
                        ),
                        (
                            "Relationship Type: "
                            f"{match.get('relationship_type', '')}"
                        ),
                        (
                            "Relationship Confidence: "
                            f"{match.get('relationship_confidence', 0.0)}"
                        ),
                        (
                            "Relationship Depth: "
                            f"{match.get('relationship_depth', 0)}"
                        ),
                        (
                            "Relationship Path: "
                            f"{match.get('relationship_path', [])}"
                        ),
                    ]
                )

            context_blocks.append("\n".join(block))
        
        context = "\n\n".join(context_blocks)

        system_prompt = """
You are an enterprise knowledge-base assistant.

STRICT GROUNDING POLICY:

1. Use the supplied knowledge base context as the ONLY source
   of factual information.
2. Do NOT use outside knowledge or pretrained knowledge to add facts.
3. You MAY synthesize information across multiple supplied context
   items when they are relevant to the user's question.
4. You MAY summarize, combine, or restate information contained
   in the context.
5. You MAY answer when the context provides partial but useful
   information. 
6. Do NOT invent missing steps, values, commands, prerequisites,
   causes, or conclusions.
7. If the context supports only part of the requested task,
   answer the supported part
8. Only respond that the knowledge base lacks enough information
   when there is genuinely no useful information in the supplied
   context.
9. Ignore context that is merely related to the topic but does not
   help answer the user's specific request.
10. Do not ask follow-up questions.

GRAPH EVIDENCE:

11. A GRAPH RELATIONSHIP EVIDENCE section is authoritative
    evidence that the referenced records are connected.

12. Do NOT require the seed record's human-readable identifier
    to appear inside the candidate document text.

13. Use the supplied Seed External ID, Relationship Type,
    Relationship Confidence, and Relationship Path when
    reasoning about relationships.

14. Do not invent or strengthen a relationship beyond what the
    graph evidence establishes.

15. For example, if the graph says:

       INC0010007 -- REFERENCES --> RSI-2

    you may state that RSI-2 is linked/referenced by
    INC0010007.

    You must NOT state that RSI-2 caused or resolved INC0010007
    unless the supplied evidence explicitly establishes that.

16. Distinguish:
       - record content
       - graph relationship evidence
       - conclusions supported by both.

"""

        prompt = f"""
You are a grounded AI assistant.

You must use only the supplied knowledge base context.
You must not use outside knowledge.
You must not invent details.

User question:
{req.question}

Knowledge base context:
{context}

Do not ask any follow-up question or clarification to user. Just provide the precise and clear answer using supplied knowledge.
"""

        response = openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        f"Question:\n{req.question}\n\n"
                        f"Knowledge base context:\n{context}"
                    ),
                },
            ],
        )

        answer = (
            response.choices[0].message.content or ""
        ).strip()

        citations = [
            {
                "source_name": match.get(
                    "source_name",
                    "",
                ),
                "file_path": match.get(
                    "file_path",
                    "",
                ),
                "page": match.get(
                    "page",
                    1,
                ),
                "servicenow_number": match.get(
                    "servicenow_number",
                    "",
                ),
            }
            for match in matches
        ]

        return {
            "answer": answer or "Not found in the provided sources.",
            "citations": citations,
            "search_type": search_result["search_type"],
            "fallback_used": search_result.get(
                "fallback_used",
                False,
            ),
        }

    except Exception as exc:
        print(
            f"/chat failed: "
            f"{type(exc).__name__}: {exc}"
        )

        return {
            "error": str(exc)
        }

@app.post("/summary")
def summary(req: GenerateRequest):
    user_prompt = (
        req.prompt.strip()
        if req.prompt and req.prompt.strip()
        else "Summarize this notebook in clear bullet points."
    )

    matches = knowledge_service.search(
        query=user_prompt,
        top_k=8,
    )

    if not matches:
        return {
            "summary": (
                "No relevant content was found "
                "in the knowledge base."
            ),
            "content": (
                "No relevant content was found "
                "in the knowledge base."
            ),
            "used_chunk_hashes": [],
           "output_id": "",
            "title": (
                getattr(req, "title", None)
                or "Summary"
            ),
        }

    context_blocks = []

    for index, match in enumerate(
        matches,
        start=1,
    ):
        source_name = (
            match.get(
                "source_name",
                "",
            )
            or "Unknown source"
        ) 

        file_path = (
            match.get(
                "file_path",
                "",
            )
            or ""
        )

        page = match.get(
            "page",
            1,
        )
        
        source_type = (
            match.get(
                "source_type",
                "",
            )
            or ""
        )

        servicenow_number = (
            match.get(
                "servicenow_number",
                "",
            )
            or ""
        )

        source_line = (
            f"[Source {index}] "
            f"Name: {source_name}"
        )

        if file_path:
            source_line += (
                f" | Path: {file_path}"
            )

        if page:
            source_line += (
                f" | Page: {page}"
            )

        if source_type:
            source_line += (
                f" | Type: {source_type}"
            )

        if servicenow_number:
            source_line += (
                f" | Record: "
                f"{servicenow_number}"
            )

        context_blocks.append(
            f"{source_line}\n"
            f"{match.get('text', '')}"
        )

    context = "\n\n".join(
        context_blocks
    ).strip()

    if not context:
        return {
            "summary": (
                "No relevant content was found "
                "in the knowledge base."
            ),
            "content": (
                "No relevant content was found "
                "in the knowledge base."
            ),
            "used_chunk_hashes": [],
            "output_id": "",
            "title": (
                getattr(req, "title", None)
                or "Summary"
            ),
        }

    prompt = f"""
You are a grounded research assistant.

Use only the supplied notebook context.
Do not use outside knowledge.
Do not invent details.
If the requested information is not supported by the context, say:
"Not found in the provided sources."

User request:
{user_prompt}

Output instruction:
Respond as a concise summary. Keep the same factual conclusions a direct answer would provide.

Notebook context:
{context}
"""

    response = openai_client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )

    content = (response.choices[0].message.content or "").strip()

    if not content:
        content = "Summary generation returned an empty response. Please try again."

    used_chunk_hashes = [
            match.get(
                "chunk_hash",
                "",
            )
            for match in matches
            if match.get("chunk_hash")
        ]

    # Remove duplicates while preserving order
    used_chunk_hashes = list(
        dict.fromkeys(
            used_chunk_hashes
        )
    )

    output_id = str(uuid.uuid4())

    title = (
        getattr(req, "title", None)
        or "Summary"
    )

    return {
        "id": output_id,
        "output_id": output_id,
        "title": title,
        "notebook_id": get_notebook_id(),
        "type": "summary",
        "prompt": user_prompt,
        "summary": content,
        "content": content,
        "used_chunk_hashes": used_chunk_hashes,
        "sources": [
            {
                "source_id": match.get(
                    "source_id",
                    "",
                ),
                "source_name": match.get(
                    "source_name",
                    "",
                ),
                "source_type": match.get(
                    "source_type",
                    "",
                ),
                "file_path": match.get(
                    "file_path",
                    "",
                ),
                "page": match.get(
                    "page",
                    1,
                ),
                "servicenow_number": match.get(
                    "servicenow_number",
                    "",
                ),
            }
            for match in matches
        ],
    }

@app.post("/quiz")
def generate_quiz(req: GenerateRequest):
    try:
        # ----------------------------------------------------------
        # 1. Get prompt
        # ----------------------------------------------------------

        user_prompt = (
            (req.prompt or "").strip()
            if hasattr(req, "prompt")
            else ""
        )

        if not user_prompt:
            user_prompt = (
                "Create a quiz based on the relevant "
                "information available in the knowledge base."
            )

        title = (
            getattr(req, "title", None)
            or "Quiz"
        )

        # ----------------------------------------------------------
        # 2. Retrieve context using the new KnowledgeService
        # ----------------------------------------------------------

        matches = knowledge_service.search(
            query=user_prompt,
            top_k=8,
        )

        if not matches:
            return {
                "error": (
                    "No relevant content was found "
                    "in the knowledge base."
                ),
                "quiz": [],
                "questions": [],
                "used_chunk_hashes": [],
                "output_id": "",
                "title": title,
            }

        # ----------------------------------------------------------
        # 3. Build context
        # ----------------------------------------------------------

        context_blocks = []

        for index, match in enumerate(
            matches,
            start=1,
        ):
            source_name = (
                match.get(
                    "source_name",
                    "",
                )
                or "Unknown source"
            )

            file_path = (
                match.get(
                    "file_path",
                    "",
                )
                or ""
            )

            page = match.get(
                "page",
                1,
            )

            source_type = (
                match.get(
                    "source_type",
                    "",
                )
                or ""
            )

            servicenow_number = (
                match.get(
                    "servicenow_number",
                    "",
                )
                or ""
            )

            source_line = (
                f"[Source {index}] "
                f"Name: {source_name}"
            )

            if file_path:
                source_line += (
                    f" | Path: {file_path}"
                )

            if page:
                source_line += (
                    f" | Page: {page}"
                )

            if source_type:
                source_line += (
                    f" | Type: {source_type}"
                )

            if servicenow_number:
                source_line += (
                    f" | Record: {servicenow_number}"
                )

            context_blocks.append(
                f"{source_line}\n"
                f"{match.get('text', '')}"
            )

        context = "\n\n".join(
            context_blocks
        ).strip()

        if not context:
            return {
                "error": (
                    "No relevant content was found "
                    "in the knowledge base."
                ),
                "quiz": [],
                "questions": [],
                "used_chunk_hashes": [],
                "output_id": "",
                "title": title,
            }

        # ----------------------------------------------------------
        # 4. Generate quiz
        # ----------------------------------------------------------

        quiz_prompt = f"""
You are an enterprise knowledge-base quiz generator.

Use ONLY the information contained in the supplied
knowledge-base context.

Do not use outside knowledge.
Do not invent facts.
Do not infer unsupported details.

User's request:
{user_prompt}

Create a useful multiple-choice quiz based only on
the supplied context.

Requirements:
- Generate 5 questions unless the user specifies a
  different number.
- Each question must have exactly one correct answer.
- Provide 4 options per question.
- Keep questions grounded in the supplied context.
- Include a concise explanation for the correct answer.
- Do not mention that you are an AI.
- Do not include a Sources section.
- Do not refer to "the passage" or "the document".
- Every question must contain enough context to stand
  on its own.

Knowledge-base context:
{context}
"""

        response = openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": quiz_prompt,
                }
            ],
        )

        quiz_content = (
            response.choices[0].message.content
            or ""
        ).strip()

        if not quiz_content:
            return {
                "error": (
                    "Quiz generation returned "
                    "an empty response."
                ),
                "quiz": [],
                "questions": [],
                "used_chunk_hashes": [],
                "output_id": "",
                "title": title,
            }

        # ----------------------------------------------------------
        # 5. Track source chunks
        # ----------------------------------------------------------

        used_chunk_hashes = [
            match.get(
                "chunk_hash",
                "",
            )
            for match in matches
            if match.get("chunk_hash")
        ]

        used_chunk_hashes = list(
            dict.fromkeys(
                used_chunk_hashes
            )
        )

        # ----------------------------------------------------------
        # 6. Generate output ID
        # ----------------------------------------------------------

        output_id = str(uuid.uuid4())

        # ----------------------------------------------------------
        # 7. Return response
        #
        # Keep both quiz and content so existing frontend code
        # can continue to work.
        # ----------------------------------------------------------

        return {
            "id": output_id,
            "output_id": output_id,
            "title": title,
            "notebook_id": get_notebook_id(),
            "type": "quiz",
            "prompt": user_prompt,

            "quiz": quiz_content,
            "content": quiz_content,
            "questions": quiz_content,

            "used_chunk_hashes": used_chunk_hashes,

            "sources": [
                {
                    "source_id": match.get(
                        "source_id",
                        "",
                    ),
                    "source_name": match.get(
                        "source_name",
                        "",
                    ),
                    "source_type": match.get(
                        "source_type",
                        "",
                    ),
                    "file_path": match.get(
                        "file_path",
                        "",
                    ),
                    "page": match.get(
                        "page",
                        1,
                    ),
                    "servicenow_number": match.get(
                        "servicenow_number",
                        "",
                    ),
                }
                for match in matches
            ],
        }

    except Exception as exc:
        print(
            "Quiz generation failed:",
            type(exc).__name__,
            str(exc),
        )

        traceback.print_exc()

        return {
            "error": (
                f"Quiz generation failed: "
                f"{type(exc).__name__}: {exc}"
            ),
            "quiz": [],
            "questions": [],
        }

@app.post("/audio")
def generate_audio(req: GenerateRequest):
    try:
        # ----------------------------------------------------------
        # 1. Validate prompt
        # ----------------------------------------------------------

        user_prompt = (
            (req.prompt or "").strip()
            if hasattr(req, "prompt")
            else ""
        )

        if not user_prompt:
            user_prompt = (
                "Create audio notes covering the relevant "
                "information available in the knowledge base."
            )

        # ----------------------------------------------------------
        # 2. Retrieve context using the SAME retrieval path
        #    used by /search, /chat and /summary
        # ----------------------------------------------------------

        matches = knowledge_service.search(
            query=user_prompt,
            top_k=8,
        )

        if not matches:
            return {
                "error": (
                    "No relevant content was found "
                    "in the knowledge base."
                ),
                "audio_url": "",
                "script": "",
                "used_chunk_hashes": [],
                "output_id": "",
                "title": (
                    getattr(req, "title", None)
                    or "Audio"
                ),
            }

        # ----------------------------------------------------------
        # 3. Build grounded context
        # ----------------------------------------------------------

        context_blocks = []

        for index, match in enumerate(
            matches,
            start=1,
        ):
            source_name = (
                match.get(
                    "source_name",
                    "",
                )
                or "Unknown source"
            )

            file_path = (
                match.get(
                    "file_path",
                    "",
                )
                or ""
            )

            page = match.get(
                "page",
                1,
            )

            source_type = (
                match.get(
                    "source_type",
                    "",
                )
                or ""
            )

            servicenow_number = (
                match.get(
                    "servicenow_number",
                    "",
                )
                or ""
            )

            source_line = (
                f"[Source {index}] "
                f"Name: {source_name}"
            )

            if file_path:
                source_line += (
                    f" | Path: {file_path}"
                )

            if page:
                source_line += (
                    f" | Page: {page}"
                )

            if source_type:
                source_line += (
                    f" | Type: {source_type}"
                )

            if servicenow_number:
                source_line += (
                    f" | Record: {servicenow_number}"
                )

            context_blocks.append(
                f"{source_line}\n"
                f"{match.get('text', '')}"
            )

        context = "\n\n".join(
            context_blocks
        ).strip()

        if not context:
            return {
                "error": (
                    "No relevant content was found "
                    "in the knowledge base."
                ),
                "audio_url": "",
                "script": "",
                "used_chunk_hashes": [],
                "output_id": "",
                "title": (
                    getattr(req, "title", None)
                    or "Audio"
                ),
            }

        # ----------------------------------------------------------
        # 4. Generate audio script
        # ----------------------------------------------------------

        script_prompt = f"""
You are an enterprise knowledge-base audio-note writer.

Use ONLY the information contained in the supplied
knowledge-base context.

Do not use outside knowledge.
Do not invent facts.
Do not infer unsupported details.

User's request:
{user_prompt}

Create a clear, natural spoken script suitable for
audio notes.

Requirements:
- Directly address the user's request.
- Explain the important facts and technical details.
- Keep the script concise but complete.
- Preserve names, numbers, record IDs, decisions,
  and important technical terms from the sources.
- Do not mention that you are an AI.
- Do not include a Sources section.
- Do not use markdown tables.
- Write prose suitable for text-to-speech.

Knowledge-base context:
{context}
"""

        script_response = openai_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": script_prompt,
                }
            ],
        )

        script = (
            script_response.choices[0].message.content
            or ""
        ).strip()

        if not script:
            return {
                "error": (
                    "Audio script generation returned "
                    "an empty response."
                ),
                "audio_url": "",
                "script": "",
                "used_chunk_hashes": [],
                "output_id": "",
                "title": (
                    getattr(req, "title", None)
                    or "Audio"
                ),
            }

        # ----------------------------------------------------------
        # 5. Generate audio
        # ----------------------------------------------------------

        # IMPORTANT:
        # Do not pass an empty string to the TTS API.
        if not script.strip():
            return {
                "error": (
                    "Cannot generate audio because "
                    "the generated script is empty."
                ),
                "audio_url": "",
                "script": "",
                "used_chunk_hashes": [],
                "output_id": "",
                "title": (
                    getattr(req, "title", None)
                    or "Audio"
                ),
            }

        output_id = str(uuid.uuid4())

        audio_filename = (
            f"audio_{output_id}.mp3"
        )

        audio_path = os.path.join(
            AUDIO_DIR,
            audio_filename,
        )

        # Make sure the directory exists.
        os.makedirs(
            AUDIO_DIR,
            exist_ok=True,
        )

        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=script,
        ) as response:
            response.stream_to_file(
                audio_path
            )

        audio_url = (
            f"/audio/{audio_filename}"
        )

        # ----------------------------------------------------------
        # 6. Track chunks used
        # ----------------------------------------------------------

        used_chunk_hashes = [
            match.get(
                "chunk_hash",
                "",
            )
            for match in matches
            if match.get("chunk_hash")
        ]

        # Remove duplicates while preserving order.
        used_chunk_hashes = list(
            dict.fromkeys(
                used_chunk_hashes
            )
        )

        title = (
            getattr(req, "title", None)
            or "Audio"
        )

        # ----------------------------------------------------------
        # 7. Return result
        # ----------------------------------------------------------

        return {
            "id": output_id,
            "output_id": output_id,
            "title": title,
            "notebook_id": get_notebook_id(),
            "type": "audio",
            "prompt": user_prompt,
            "script": script,
            "audio_url": audio_url,
            "used_chunk_hashes": used_chunk_hashes,
            "sources": [
                {
                    "source_id": match.get(
                        "source_id",
                        "",
                    ),
                    "source_name": match.get(
                        "source_name",
                        "",
                    ),
                    "source_type": match.get(
                        "source_type",
                        "",
                    ),
                    "file_path": match.get(
                        "file_path",
                        "",
                    ),
                    "page": match.get(
                        "page",
                        1,
                    ),
                    "servicenow_number": match.get(
                        "servicenow_number",
                        "",
                    ),
                }
                for match in matches
            ],
        }

    except Exception as exc:
        print(
            "Audio generation failed:",
            type(exc).__name__,
            str(exc),
        )
        traceback.print_exc()

        return {
            "error": (
                f"Audio generation failed: "
                f"{type(exc).__name__}: {exc}"
            ),
            "audio_url": "",
            "script": "",
        }

@app.get("/audio/{filename}")
def get_audio(filename: str):
    path = os.path.join(AUDIO_DIR, filename)
    return FileResponse(path, media_type="audio/mpeg", filename=filename)


@app.post("/delete-notebook")
def delete_notebook(req: DeleteNotebookRequest):
    collection.delete(where={"notebook_id": req.notebook_id})
    return {"status": "ok", "deleted_notebook_id": req.notebook_id}

def reset_servicenow_sync_state():
    save_servicenow_sync_state({})

@app.delete("/knowledge-base")
def clean_knowledge_base():
    try:
        result = knowledge_service.reset()
        reset_servicenow_sync_state()

        return {
            "status": "success",
            "message": "Knowledge base reset successfully.",
            "chunks_deleted": result.get(
                "chunks_deleted",
                0,
            ),
        }

    except Exception as exc:
        print(
            "Knowledge base reset failed:",
            type(exc).__name__,
            str(exc),
        )

        traceback.print_exc()

        raise HTTPException(
            status_code=500,
            detail=(
                f"Knowledge base reset failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

def load_servicenow_sync_state():
    if not os.path.exists(SERVICENOW_SYNC_FILE):
        return {
            "incident": {},
        }

    try:
        with open(SERVICENOW_SYNC_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "incident": {},
        }


def save_servicenow_sync_state(state):
    temp_file = f"{SERVICENOW_SYNC_FILE}.tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    os.replace(temp_file, SERVICENOW_SYNC_FILE)

def normalize_servicenow_datetime(value: str) -> str:
    if not value:
        return ""

    value = value.strip()

    try:
        parsed = datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S",
        )

        return parsed.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    except ValueError:
        return value

def fetch_new_servicenow_incidents(
    last_updated: str | None,
):
    if last_updated:
        return fetch_servicenow_records(
            table="incident",
            page_size=100,
            last_updated=last_updated,
        )
    else:
        # First run: only fetch incidents created recently.
        # Change this period if you want a larger initial window.
        since = (
            datetime.now(timezone.utc)
            - timedelta(minutes=5)
        ).strftime("%Y-%m-%d %H:%M:%S")

        query = (
            f"sys_created_on>={since}"
            f"^ORDERBYsys_created_on"
        )

        return fetch_servicenow_records(
            table="incident",
            encoded_query=query,
            page_size=100,
        )

def fetch_new_servicenow_problems(
    last_updated: str | None,
):
    if last_updated:
        return fetch_servicenow_records(
            table="problem",
            page_size=100,
            last_updated=last_updated,
        )

    return fetch_servicenow_records(
        table="problem",
        page_size=100,
    )

def fetch_new_servicenow_change_requests(
    last_updated: str | None,
):
    if last_updated:
        return fetch_servicenow_records(
            table="change_request",
            page_size=100,
            last_updated=last_updated,
        )

    return fetch_servicenow_records(
        table="change_request",
        page_size=100,
    )

def index_single_servicenow_record(
    notebook_id: str,
    table: str,
    record: dict,
    index_relationships: bool = True,
):
    sys_id = servicenow_value(
        record,
        "sys_id",
    )

    number = servicenow_value(
        record,
        "number",
    )

    updated_on = servicenow_value(
        record,
        "sys_updated_on",
    )

    if not sys_id:
        return {
            "status": "skipped",
            "reason": "Missing sys_id",
        }

    # --------------------------------------------------------------
    # Build record content
    # --------------------------------------------------------------

    if table == "incident":
        content = build_incident_text(record)

    elif table == "problem":
        content = build_problem_text(record)

    elif table == "change_request":
        content = build_change_request_text(record)

    else:
        return {
            "status": "skipped",
            "reason": f"Unsupported ServiceNow table: {table}",
        }

    content = (content or "").strip()

    if not content:
        return {
            "status": "skipped",
            "reason": "Empty content",
            "sys_id": sys_id,
            "number": number,
        }

    # --------------------------------------------------------------
    # Get the SAME stable source ID used by initial ingestion
    # --------------------------------------------------------------

    source_id = get_or_create_servicenow_source_id(
        notebook_id,
        table,
    )

    source_name = SERVICENOW_TABLE_CONFIG[
        table
    ]["display_name"]

    # --------------------------------------------------------------
    # Parse ServiceNow timestamp
    # --------------------------------------------------------------

    updated_at = None

    if updated_on:
        try:
            updated_at = datetime.strptime(
                updated_on,
                "%Y-%m-%d %H:%M:%S",
            )
        except ValueError:
            print(
                f"Unable to parse sys_updated_on "
                f"'{updated_on}' for {number}"
            )

    # --------------------------------------------------------------
    # Create the SAME record ID format used by full ingestion
    # --------------------------------------------------------------

    record_id = (
        f"servicenow:{table}:{sys_id}"
    )

    knowledge_record = KnowledgeRecord(
        record_id=record_id,
        source_id=source_id,
        source_type="servicenow",
        entity_type=table,
        external_id=sys_id,
        title=number,
        locator=f"servicenow/{table}/{sys_id}",
        content=content,
        content_hash=text_hash(content),
        updated_at=updated_at,

        servicenow_table=table,
        servicenow_sys_id=sys_id,
        servicenow_number=number,
    )

    # --------------------------------------------------------------
    # KnowledgeService automatically replaces the old record chunks
    # --------------------------------------------------------------

    result = knowledge_service.index_record(
        record=knowledge_record,
        source_name=source_name,
        index_relationships=index_relationships,
    )

    return {
        "status": result.get(
            "status",
            "indexed",
        ),
        "table": table,
        "sys_id": sys_id,
        "number": number,
        "record_id": record_id,
        "record": knowledge_record,
        "chunks_indexed": result.get(
            "chunks_indexed",
            0,
        ),
    }

def index_single_servicenow_incident(
    notebook_id: str,
    record: dict,
    index_relationships: bool = True,
):
    return index_single_servicenow_record(
        notebook_id=notebook_id,
        table="incident",
        record=record,
        index_relationships=index_relationships,
    )

def load_servicenow_source_ids():
    if not os.path.exists(SERVICENOW_SOURCE_IDS_FILE):
        return {}

    try:
        with open(
            SERVICENOW_SOURCE_IDS_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            return json.load(f)
    except Exception:
        return {}


def save_servicenow_source_ids(data):
    temp_file = f"{SERVICENOW_SOURCE_IDS_FILE}.tmp"

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    os.replace(
        temp_file,
        SERVICENOW_SOURCE_IDS_FILE,
    )


def get_or_create_servicenow_source_id(
    notebook_id: str,
    table: str,
):
    state = load_servicenow_source_ids()

    key = f"{notebook_id}:{table}"

    if key not in state:
        state[key] = str(uuid.uuid4())
        save_servicenow_source_ids(state)

    return state[key]

def delete_existing_servicenow_incident_chunks(
    notebook_id: str,
    sys_id: str,
):
    collection.delete(
        where={
            "$and": [
                {"notebook_id": notebook_id},
                {"servicenow_table": "incident"},
                {"servicenow_sys_id": sys_id},
            ]
        }
    )

def index_single_servicenow_change_request(
    notebook_id: str,
    record: dict,
    index_relationships: bool = True,
):
    return index_single_servicenow_record(
        notebook_id=notebook_id,
        table="change_request",
        record=record,
        index_relationships=index_relationships,
    )

def index_single_servicenow_problem(
    notebook_id: str,
    record: dict,
    index_relationships: bool = True,
):
    return index_single_servicenow_record(
        notebook_id=notebook_id,
        table="problem",
        record=record,
        index_relationships=index_relationships,
    )

def poll_servicenow_table_once(
    notebook_id: str,
    table: str,
    fetch_function,
    index_function,
):
    sync_state = load_servicenow_sync_state()

    table_state = sync_state.get(
        table,
        {},
    )

    if isinstance(table_state, str):
        last_updated = table_state
    else:
        last_updated = table_state.get(
            "last_updated",
            "",
        )

    print(
        f"ServiceNow {table} polling. "
        f"Checkpoint: {last_updated}"
    )

    records = fetch_function(
        last_updated=last_updated
    )

    if not records:
        print(
            f"ServiceNow {table}: "
            "no new or updated records."
        )
        return {
            "table": table,
            "indexed_count": 0,
            "records": [],
            "checkpoint": last_updated,
        }

    newest_updated = last_updated
    indexed_count = 0
    indexed_records = []

    for record in records:
        number = servicenow_value(
            record,
            "number",
        )

        updated = normalize_servicenow_datetime(
            servicenow_value(
                record,
                "sys_updated_on",
            )
        )

        print(
            f"ServiceNow {table}: "
            f"processing {number} "
            f"updated={updated}"
        )

        try:
            result = index_function(
                notebook_id=notebook_id,
                record=record,
                index_relationships=False,
            )

            if result.get("status") != "indexed":
                raise RuntimeError(
                    f"Indexing failed: {result}"
                )

            indexed_count += 1

            knowledge_record = result.get(
                "record"
            )

            if knowledge_record is None:
                raise RuntimeError(
                    f"ServiceNow {table} "
                    f"{number} indexed successfully "
                    "but no KnowledgeRecord was returned."
                )

            indexed_records.append(
                knowledge_record
            )

        except Exception as exc:
            print(
                f"Failed to index "
                f"ServiceNow {table} record "
                f"{number}: "
                f"{type(exc).__name__}: {exc}"
            )

            # Do not advance this table's checkpoint
            # if indexing fails.
            raise

        if (
            updated
            and (
                not newest_updated
                or updated > newest_updated
            )
        ):
            newest_updated = updated

    sync_state[table] = {
        "last_updated": newest_updated,
    }

    save_servicenow_sync_state(
        sync_state
    )

    print(
        f"ServiceNow {table}: indexed "
        f"{indexed_count} records. "
        f"New checkpoint={newest_updated}"
    )

    return {
        "table": table,
        "indexed_count": indexed_count,
        "records": indexed_records,
        "checkpoint": newest_updated,
    }

def get_registered_servicenow_tables():
    state = load_servicenow_source_ids()

    if not isinstance(state, dict):
        return []

    prefix = f"{get_notebook_id()}:"

    registered_tables = []

    for key, source_id in state.items():
        if not key.startswith(prefix):
            continue

        table = key[len(prefix):]

        if table in {
            "incident",
            "problem",
            "change_request",
        } and source_id:
            registered_tables.append(table)

    return registered_tables

def poll_servicenow_once():

    """
    Poll ServiceNow for all registered tables.

    Pass 1:
        - Fetch new/updated records
        - Index records
        - Register RecordIdentity mappings
        - Do NOT create relationships yet

    Pass 2:
        - Use all records collected from this polling cycle
        - Resolve their relationships now that all identities
          from this batch are registered
    """

    print(
        "**** Starting ServiceNow Polling Process ****"
    )

    registered_tables = (
        get_registered_servicenow_tables()
    )

    if not registered_tables:
        print(
            "ServiceNow polling skipped: "
            "no ServiceNow sources registered."
        )
        return

    notebook_id = get_notebook_id()

    total_indexed = 0
    all_records = []

    if "incident" in registered_tables:
        result = poll_servicenow_table_once(
            notebook_id=notebook_id,
            table="incident",
            fetch_function=fetch_new_servicenow_incidents,
            index_function=index_single_servicenow_incident,
        )
        indexed_count = result.get("indexed_count", 0,)
        records = result.get("records", [],)
        total_indexed += indexed_count
        if records:
            all_records.extend(records)

    if "problem" in registered_tables:
        result = poll_servicenow_table_once(
            notebook_id=notebook_id,
            table="problem",
            fetch_function=fetch_new_servicenow_problems,
            index_function=index_single_servicenow_problem,
        )
        indexed_count = result.get("indexed_count", 0,)
        records = result.get("records", [],)
        total_indexed += indexed_count
        if records:
            all_records.extend(records)


    if "change_request" in registered_tables:
        result = poll_servicenow_table_once(
            notebook_id=notebook_id,
            table="change_request",
            fetch_function=fetch_new_servicenow_change_requests,
            index_function=index_single_servicenow_change_request,
        )
        indexed_count = result.get("indexed_count", 0,)
        records = result.get("records", [],)
        total_indexed += indexed_count
        if records:
            all_records.extend(records)

    # Now, create the Relationships for all records

    relationships_created = 0

    print(
        f"ServiceNow relationship pass: "
        f"{len(all_records)} records"
    )

    for record in all_records:
    
        try:
            relationships = (
                knowledge_service.index_relationships(
                    record
                )
            )

            created_count = len(
                relationships or []
            )

            relationships_created += (
                created_count
            )

            if created_count:
                print(
                    f"ServiceNow relationship pass: "
                    f"{record.record_id}: "
                    f"created={created_count}"
                )

        except Exception as exc:

            print(
                f"ServiceNow relationship processing failed "
                f"for {record.record_id}: "
                f"{type(exc).__name__}: {exc}"
            )

            traceback.print_exc()

            # Relationship failure should not invalidate the
            # successful record indexing that already happened.
            continue

    print(
        f"**** ServiceNow Polling Complete. "
        f"Indexed {total_indexed} records ****"
        f"Created {relationships_created} relationships"
    )

    return {
        "status": "completed",
        "records_indexed": total_indexed,
        "relationships_created": (
            relationships_created
        ),
        "records": all_records,
    }

def poll_servicenow_incidents_once():
    print("**** Starting ServiceNow Polling Process ****")
    sync_state = load_servicenow_sync_state()

    last_updated = sync_state.get(
        "incident",
        "",
    )

    print(f"Last updated  {last_updated}")
    records = fetch_new_servicenow_incidents(
        last_updated=last_updated,
    )

    if not records:
        print("ServiceNow polling: no new or updated incidents.")
        return

    newest_timestamp = last_updated
    indexed_count = 0

    notebook_id = get_notebook_id()
    for record in records:
        result = index_single_servicenow_incident(
            notebook_id,
            record
        )

        if result.get("status") == "indexed":
            indexed_count += 1

        updated = normalize_servicenow_datetime(
            servicenow_value(
                record,
                "sys_updated_on",
            )
        )

        print("ServiceNow checkpoint:", updated, "raw:", servicenow_value(record, "sys_updated_on"),)

        if updated and (
            not newest_timestamp
            or updated > newest_timestamp
        ):
            newest_timestamp = updated

    if newest_timestamp:
        sync_state["incident"] = newest_timestamp
        save_servicenow_sync_state(sync_state)

    print("Saving ServiceNow checkpoint:", sync_state["incident"],)
    print(
        f"ServiceNow polling: "
        f"indexed {indexed_count} incidents; "
        f"checkpoint={newest_timestamp}"
    )

def extract_servicenow_number(query: str):
    match = SERVICENOW_RECORD_PATTERN.search(query or "")

    if not match:
        return None

    return match.group(0).upper()


def classify_search_query(query: str):
    normalized = (query or "").lower().strip()

    # Exact ServiceNow record number
    record_number = extract_servicenow_number(query)

    if record_number:
        prefix = record_number[:3]

        if prefix == "INC":
            table = "incident"
        elif prefix == "PRB":
            table = "problem"
        elif prefix == "CHG":
            table = "change_request"
        else:
            table = None

        return {
            "type": "servicenow",
            "table": table,
            "record_number": record_number,
        }

    # Explicit ServiceNow mention
    if "servicenow" in normalized:
        return {
            "type": "servicenow",
            "table": None,
            "record_number": None,
        }

    # Explicit ServiceNow table terms
    for term, table in SERVICENOW_TABLE_TERMS.items():
        if term in normalized:
            return {
                "type": "servicenow",
                "table": table,
                "record_number": None,
            }

    # Generic search
    return {
        "type": "generic",
        "table": None,
        "record_number": None,
    }


# -------------------------------------------------------------------
# Unified search endpoint
# -------------------------------------------------------------------

@app.get("/search")
def search(query: str):

    if not query:
        return {"error": "Query is required."}

    try:
        result = perform_search(
            query=query,
            top_k=20,
        )

        # ------------------------------------------------------
        # Add relationships for the records returned by search
        # ------------------------------------------------------

        relationships = (
            knowledge_service.get_relationships_for_results(
                result.get(
                    "results",
                    []
                )
            )
        )

        result["relationships"] = relationships

        return format_search_response(
            query=query,
            result=result,
        )

        """
        return {
            "query": query,
            **result,
            "count": len(result["results"]),
        }
        """

    except Exception as exc:
        print(f"/search failed: {type(exc).__name__}: {exc}")
        return {"error": str(exc)}

@app.post("/search")
def search(req: SearchRequest):
    query = (req.query or "").strip()

    if not query:
        return {"error": "Query is required."}

    try:
        result = perform_search(
            query=query,
            top_k=req.top_k,
        )

        # ------------------------------------------------------
        # Add relationships for the records returned by search
        # ------------------------------------------------------

        relationships = (
            knowledge_service.get_relationships_for_results(
                result.get(
                    "results",
                    []
                )
            )
        )

        result["relationships"] = relationships

        return format_search_response(
            query=req.query,
            result=result,
        )

    except Exception as exc:
        print(f"/search failed: {type(exc).__name__}: {exc}")
        return {"error": str(exc)}

def format_search_response(
    query: str,
    result: dict,
):
    matches = result.get(
        "results",
        [],
    )

    return {
        "query": query,
        "search_type": result.get(
            "search_type",
            "generic",
        ),
        "original_search_type": result.get(
            "original_search_type",
        ),
        "servicenow_table": result.get(
            "servicenow_table",
        ),
        "servicenow_number": result.get(
            "servicenow_number",
        ),
        "fallback_used": result.get(
            "fallback_used",
            False,
        ),
        "results": matches,
        "count": len(matches),
        "relationships": result.get("relationships", [],),
    }

def has_relevant_results(
    matches: list[dict],
    threshold: float = SERVICENOW_DISTANCE_THRESHOLD,
) -> bool:
    if not matches:
        return False

    for match in matches:
        distance = match.get("distance")

        if distance is None:
            continue

        if distance <= threshold:
            return True

    return False

def search_exact_servicenow_record(
    record_number: str,
    top_k: int = 8,
):
    record_number = (record_number or "").strip().upper()

    if not record_number:
        return []

    # First determine the table from the prefix.
    if record_number.startswith("INC"):
        table = "incident"
    elif record_number.startswith("PRB"):
        table = "problem"
    elif record_number.startswith("CHG"):
        table = "change_request"
    else:
        return []

    results = collection.get(
        where={
            "$and": [
                {"source_type": "servicenow"},
                {"servicenow_table": table},
                {"servicenow_number": record_number},
            ]
        },
        include=[
            "documents",
            "metadatas",
        ],
    )

    docs = results.get("documents", []) or []
    metas = results.get("metadatas", []) or []

    matches = []

    for doc, meta in zip(docs, metas):
        meta = meta or {}

        matches.append({
            "text": doc,
            "source_id": meta.get("source_id", ""),
            "source_name": meta.get("source_name", ""),
            "source_type": meta.get("source_type", ""),
            "file_path": meta.get("file_path", ""),
            "file_name": meta.get("file_name", ""),
            "page": meta.get("page", 1),
            "chunk_index": meta.get("chunk_index", 0),
            "chunk_hash": meta.get("chunk_hash", ""),
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
        })

        if len(matches) >= top_k:
            break

    return matches

def perform_search(query: str, top_k: int = 8):

    print("****************************************\n")
    print(f"********** Performing search with query -> {query} ***************\n")
    print("****************************************\n")
    classification = classify_search_query(query)
    print(f"************ Search Query Classification result -> {classification} ************\n")

    search_type = classification["type"]
    servicenow_table = classification["table"]
    servicenow_number = classification["record_number"]

    # Exact ServiceNow record
    if servicenow_number:
        print("****************************************\n")
        print("************ Exact ServiceNow record mentioned in search query **********\n")
        print("****************************************\n")
        matches = knowledge_service.search(
            query=query,
            top_k=top_k,
            source_type="servicenow",
            servicenow_table=servicenow_table,
            servicenow_number=servicenow_number,
            exact_record=True,
        )

        if matches:
            return {
                "search_type": "servicenow-record",
                "servicenow_table": servicenow_table,
                "servicenow_number": servicenow_number,
                "fallback_used": False,
                "results": matches,
            }

        # Fall back to generic
        print("****************************************\n")
        print("************ Could not find any record for the given ServiceNow record, so using generic search as fallback **********\n")
        print("****************************************\n")
        matches = knowledge_service.search(
            query=query,
            top_k=top_k,
        )

        return {
            "search_type": "generic",
            "original_search_type": "servicenow-record",
            "servicenow_table": servicenow_table,
            "servicenow_number": servicenow_number,
            "fallback_used": True,
            "results": matches,
        }

    # ServiceNow semantic search
    if search_type == "servicenow":
        print("****************************************\n")
        print("************ Search Query is related to ServiceNow but no specific ServiceNow record mentioned  **********\n")
        print("****************************************\n")
        matches = knowledge_service.search(
            query=query,
            top_k=top_k,
            source_type="servicenow",
            servicenow_table=servicenow_table,
        )

        if has_relevant_results(matches):
            return {
                "search_type": "servicenow",
                "servicenow_table": servicenow_table,
                "fallback_used": False,
                "results": matches,
            }

        # Fall back to generic
        print("****************************************\n")
        print("************ Could not find any record for the given ServiceNow-related query, so using generic search as fallback **********\n")
        print("****************************************\n")
        matches = knowledge_service.search(
            query=query,
            top_k=top_k,
        )

        return {
            "search_type": "generic",
            "original_search_type": "servicenow",
            "servicenow_table": servicenow_table,
            "fallback_used": True,
            "results": matches,
        }

    # Generic search
    print("****************************************\n")
    print("************ User has entered generic query, so, performing generic search **********\n")
    print("****************************************\n")
    matches = knowledge_service.search(
        query=query,
        top_k=top_k,
    )

    return {
        "search_type": "generic",
        "fallback_used": False,
        "results": matches,
    }

def jira_headers():
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def jira_auth():
    if not JIRA_BASE_URL:
        raise RuntimeError(
            "Missing JIRA_BASE_URL."
        )

    if not ATLASSIAN_EMAIL:
        raise RuntimeError(
            "Missing Atlassian email."
        )

    if not ATLASSIAN_API_TOKEN:
        raise RuntimeError(
            "Missing Atlassian API token."
        )

    return HTTPBasicAuth(
        ATLASSIAN_EMAIL,
        ATLASSIAN_API_TOKEN,
    )

def jira_adf_to_text(value) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        return value.strip()

    if not isinstance(value, dict):
        return str(value)

    parts = []

    def walk(node):
        if isinstance(node, dict):

            text = node.get("text")

            if text:
                parts.append(str(text))

            node_type = node.get("type")

            if node_type in {
                "paragraph",
                "heading",
                "blockquote",
                "codeBlock",
                "listItem",
            }:
                parts.append("\n")

            for child in node.get(
                "content",
                [],
            ):
                walk(child)

        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)

    text = "".join(parts)

    lines = [
        line.strip()
        for line in text.splitlines()
    ]

    return "\n".join(
        line
        for line in lines
        if line
    )

def fetch_jira_issues(
    project_key: str,
    jql_extra: str | None = None,
    max_results: int = 100,
):
    jql = (
        f'project = "{project_key}" '
        f"ORDER BY updated ASC"
    )

    if jql_extra:
        jql = (
            f'project = "{project_key}" '
            f"AND ({jql_extra}) "
            f"ORDER BY updated ASC"
        )

    fields = [
        "summary",
        "description",
        "status",
        "priority",
        "assignee",
        "reporter",
        "created",
        "updated",
        "resolutiondate",
        "labels",
        "components",
        "project",
        "issuetype",
        "parent",
        "issuelinks",
        "comment",
    ]

    issues = []

    next_page_token = None

    while True:

        body = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields,
        }

        if next_page_token:
            body["nextPageToken"] = (
                next_page_token
            )

        api_url = (
            f"{JIRA_BASE_URL}"
            f"/rest/api/3/search/jql"
        )

        response = requests.post(
            api_url,
            headers=jira_headers(),
            auth=jira_auth(),
            json=body,
            timeout=60,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                "Jira search failed: "
                f"{response.status_code} - "
                f"{response.text}"
            )

        payload = response.json()

        page_issues = payload.get(
            "issues",
            [],
        )

        issues.extend(
            page_issues
        )

        next_page_token = payload.get(
            "nextPageToken"
        )

        if not next_page_token:
            break

    return issues

def fetch_jira_comments(
    issue_id_or_key: str,
):
    comments = []

    start_at = 0
    max_results = 100

    while True:

        api_url = (
            f"{JIRA_BASE_URL}"
            f"/rest/api/3/issue/"
            f"{issue_id_or_key}/comment"
        )

        params = {
            "startAt": start_at,
            "maxResults": max_results,
        }

        response = requests.get(
            api_url,
            headers=jira_headers(),
            auth=jira_auth(),
            params=params,
            timeout=60,
        )

        if response.status_code >= 400:
            raise RuntimeError(
                "Jira comment fetch failed: "
                f"{response.status_code} - "
                f"{response.text}"
            )

        payload = response.json()

        values = payload.get(
            "comments",
            [],
        )

        comments.extend(
            values
        )

        if len(values) < max_results:
            break

        start_at += max_results

    return comments

def build_jira_issue_text(
    issue: dict,
    comments: list[dict],
) -> str:

    fields = issue.get(
        "fields",
        {},
    )

    key = issue.get(
        "key",
        "",
    )

    issue_type = (
        fields.get(
            "issuetype",
            {},
        ) or {}
    ).get(
        "name",
        "",
    )

    summary = (
        fields.get(
            "summary",
            "",
        )
        or ""
    )

    description = jira_adf_to_text(
        fields.get(
            "description"
        )
    )

    status = (
        fields.get(
            "status",
            {},
        ) or {}
    ).get(
        "name",
        "",
    )

    priority = (
        fields.get(
            "priority",
            {},
        ) or {}
    ).get(
        "name",
        "",
    )

    assignee = (
        fields.get(
            "assignee",
            {},
        ) or {}
    ).get(
        "displayName",
        "",
    )

    reporter = (
        fields.get(
            "reporter",
            {},
        ) or {}
    ).get(
        "displayName",
        "",
    )

    created = (
        fields.get(
            "created",
            "",
        )
        or ""
    )

    updated = (
        fields.get(
            "updated",
            "",
        )
        or ""
    )

    resolution_date = (
        fields.get(
            "resolutiondate",
            "",
        )
        or ""
    )

    labels = fields.get(
        "labels",
        [],
    ) or []

    components = [
        component.get(
            "name",
            "",
        )
        for component in (
            fields.get(
                "components",
                [],
            )
            or []
        )
    ]

    parts = [
        f"JIRA ISSUE: {key}",
        f"TYPE: {issue_type}",
        f"SUMMARY: {summary}",
        f"STATUS: {status}",
        f"PRIORITY: {priority}",
        f"ASSIGNEE: {assignee}",
        f"REPORTER: {reporter}",
        f"CREATED: {created}",
        f"UPDATED: {updated}",
        f"RESOLUTION DATE: {resolution_date}",
        (
            "LABELS: "
            + ", ".join(labels)
            if labels
            else "LABELS:"
        ),
        (
            "COMPONENTS: "
            + ", ".join(components)
            if components
            else "COMPONENTS:"
        ),
        "",
        "DESCRIPTION:",
        description,
    ]

    if comments:

        parts.extend([
            "",
            "COMMENTS:",
        ])

        for comment in comments:

            author = (
                comment.get(
                    "author",
                    {},
                ) or {}
            ).get(
                "displayName",
                "",
            )

            created_at = (
                comment.get(
                    "created",
                    "",
                )
                or ""
            )

            body = jira_adf_to_text(
                comment.get(
                    "body"
                )
            )

            if not body:
                continue

            parts.extend([
                "",
                f"{author} [{created_at}]:",
                body,
            ])

    return "\n".join(parts).strip()

def jira_issue_to_knowledge_record(
    notebook_id: str,
    project_key: str,
    issue: dict,
) -> KnowledgeRecord:

    issue_id = (
        issue.get(
            "id",
            "",
        )
        or ""
    ).strip()

    issue_key = (
        issue.get(
            "key",
            "",
        )
        or ""
    ).strip()

    fields = issue.get(
        "fields",
        {},
    )

    if not issue_id:
        raise RuntimeError(
            "Jira issue is missing id."
        )

    if not issue_key:
        raise RuntimeError(
            "Jira issue is missing key."
        )

    source_id = (
        get_or_create_jira_source_id(
            notebook_id=notebook_id,
            project_key=project_key,
        )
    )

    source_name = (
        f"Jira Service Management - "
        f"{project_key}"
    )

    comments = fetch_jira_comments(
        issue_key
    )

    content = build_jira_issue_text(
        issue=issue,
        comments=comments,
    )

    record_id = (
        f"jira:"
        f"{project_key}:"
        f"{issue_id}"
    )

    updated_at = (
        fields.get(
            "updated",
            "",
        )
        or ""
    )

    knowledge_record = KnowledgeRecord(
        record_id=record_id,
        source_id=source_id,
        source_type="jira",
        entity_type="jira_issue",
        source_name=source_name,
        external_id=issue_key,
        title=(
            fields.get(
                "summary",
                "",
            )
            or issue_key
        ),
        locator=(
            f"{JIRA_BASE_URL}"
            f"/browse/"
            f"{issue_key}"
        ),
        content=content,
        content_hash=text_hash(
            content
        ),
        updated_at=None,
        jira_project_key=project_key,
        jira_issue_id=issue_id,
        jira_issue_key=issue_key,
        jira_updated_at=updated_at,
        metadata={
            "jira_issue_id": issue_id,
            "jira_project_key": project_key,
            "jira_issue_links": fields.get(
                "issuelinks",
                [],
            ),
            "parent": fields.get(
                "parent"
            ),
        },
    )

    return knowledge_record

def load_jira_source_ids():
    if not os.path.exists(
        JIRA_SOURCE_IDS_FILE
    ):
        return {}

    try:
        with open(
            JIRA_SOURCE_IDS_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        return (
            data
            if isinstance(data, dict)
            else {}
        )

    except Exception as exc:
        print(
            "Failed to load Jira source IDs:",
            type(exc).__name__,
            str(exc),
        )

        return {}


def save_jira_source_ids(
    data: dict,
):
    temp_file = (
        f"{JIRA_SOURCE_IDS_FILE}.tmp"
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
        )

    os.replace(
        temp_file,
        JIRA_SOURCE_IDS_FILE,
    )


def get_or_create_jira_source_id(
    notebook_id: str,
    project_key: str,
):
    data = load_jira_source_ids()

    key = (
        f"{notebook_id}:"
        f"project:"
        f"{project_key}"
    )

    source_id = data.get(
        key
    )

    if source_id:
        return source_id

    source_id = str(
        uuid.uuid4()
    )

    data[key] = source_id

    save_jira_source_ids(
        data
    )

    return source_id

@app.post("/ingest-jira")
def ingest_jira(
    req: JiraIngestRequest,
):

    notebook_id = (
        req.notebook_id
        or get_notebook_id()
    )

    project_key = (
        req.project_key
        or ""
    ).strip().upper()

    if not project_key:
        return {
            "error": "project_key is required."
        }

    try:

        print(
            "**** Starting Jira Service Management "
            "ingestion ****"
        )

        issues = fetch_jira_issues(
            project_key=project_key,
            max_results=req.max_results,
        )

        print(
            f"Jira {project_key}: "
            f"received {len(issues)} issues"
        )

        records_indexed = 0
        chunks_indexed = 0

        indexed_records = []

        # We have to store the latest value of Updated column across all JIRA Issues
        # so that during polling process, we can subsequent JIRA Issues only

        newest_updated_at = None

        for issue in issues:

            fields = issue.get("fields", {},) 
            updated = (fields.get("updated"))
            if updated:
                if (newest_updated_at is None or updated > newest_updated_at):
                    newest_updated_at = updated

            issue_key = issue.get(
                "key",
                "",
            )

            try:

                knowledge_record = (
                    jira_issue_to_knowledge_record(
                        notebook_id=notebook_id,
                        project_key=project_key,
                        issue=issue,
                    )
                )

                result = (
                    knowledge_service.index_record(
                        record=knowledge_record,
                        source_name=(
                            f"Jira Service Management - "
                            f"{project_key}"
                        ),
                        index_relationships=False,
                    )
                )

                if (
                    result.get("status")
                    == "indexed"
                ):

                    records_indexed += 1

                    chunks_indexed += (
                        result.get(
                            "chunks_indexed",
                            0,
                        )
                    )

                    indexed_records.append(
                        knowledge_record
                    )

            except Exception as exc:

                print(
                    f"Failed to index Jira issue "
                    f"{issue_key}: "
                    f"{type(exc).__name__}: {exc}"
                )

                traceback.print_exc()

        # At this time, all JIRA Issues have been Indexed
        # So, store the latest updated time in sync state

        source_id = get_or_create_jira_source_id(notebook_id=notebook_id, project_key=project_key,)

        print(f"********* SOURCE ID -> {source_id} ************")
        print(f"********* UPDATED -> {newest_updated_at} ************")
        set_jira_sync_state(
            notebook_id=notebook_id,
            project_key=project_key,
            state={
                "source_id": source_id,
                "last_jira_updated_at": newest_updated_at,
            },
        )
        print(f"********* SAVED JIRA SYNC STATE ************")

        # ------------------------------------------------------
        # Second pass:
        # identities now exist
        # ------------------------------------------------------

        relationships_created = 0

        for record in indexed_records:

            try:

                relationships = (
                    knowledge_service.index_relationships(
                        record
                    )
                )

                relationships_created += len(
                    relationships or []
                )

            except Exception as exc:

                print(
                    f"Jira relationship processing "
                    f"failed for {record.record_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

                traceback.print_exc()

        print(
            "**** Jira Service Management "
            "ingestion complete ****"
        )

        return {
            "status": "indexed",

            "notebook_id": notebook_id,

            "source_type": "jira",

            "project_key": project_key,

            "records_indexed": records_indexed,

            "chunks_indexed": chunks_indexed,

            "relationships_created": (
                relationships_created
            ),
        }

    except Exception as exc:

        print(
            "Jira ingestion failed:",
            type(exc).__name__,
            str(exc),
        )

        traceback.print_exc()

        return {
            "error": str(exc),
            "source_type": "jira",
        }
