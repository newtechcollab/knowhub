from datetime import datetime
from typing import Optional
from typing import Any
from pydantic import BaseModel, Field

class KnowledgeRelation(BaseModel):
    relation_id: str

    source_record_id: str
    target_record_id: str

    relation_type: str

    confidence: float = 1.0

    source: str = "deterministic"

    created_at: datetime = Field(
        default_factory=datetime.utcnow
    )


class Source(BaseModel):
    source_id: str
    source_type: str
    name: str
    locator: Optional[str] = None
    external_id: Optional[str] = None
    status: str = "active"
    last_synced_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_error: Optional[str] = None


class KnowledgeRecord(BaseModel):
    record_id: str
    source_id: str
    source_type: str
    entity_type: str | None
    external_id: Optional[str] = None
    title: Optional[str] = None
    locator: Optional[str] = None
    content: str
    updated_at: Optional[datetime] = None
    content_hash: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    servicenow_table: Optional[str] = None
    servicenow_sys_id: Optional[str] = None
    servicenow_number: Optional[str] = None
    jira_project_key: str | None = None
    jira_issue_id: str | None = None
    jira_issue_key: str | None = None
    jira_updated_at: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict
    )

class KnowledgeChunk(BaseModel):
    chunk_id: str
    record_id: str
    source_id: str
    source_type: str
    entity_type: str | None = None
    source_name: Optional[str] = None
    chunk_index: int
    text: str
    chunk_hash: str
    locator: Optional[str] = None
    page: Optional[int] = None
    updated_at: Optional[datetime] = None
    servicenow_table: Optional[str] = None
    servicenow_sys_id: Optional[str] = None
    servicenow_number: Optional[str] = None
