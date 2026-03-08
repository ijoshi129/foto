from pydantic import BaseModel, Field
from typing import Optional, Dict, List
from enum import Enum
import uuid


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    error = "error"


class NASShare(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    ip: str
    share_name: str          # SMB share name (e.g. "Photos")
    path: str                # Sub-path within the share (e.g. "/Camera")
    username: str
    password: str


class NASShareCreate(BaseModel):
    name: str
    ip: str
    share_name: str
    path: str
    username: str
    password: str


class NASShareUpdate(BaseModel):
    name: Optional[str] = None
    ip: Optional[str] = None
    share_name: Optional[str] = None
    path: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class AppSettings(BaseModel):
    discord_webhook_url: Optional[str] = None
    date_folder_format: str = "MM-DD"  # MM-DD | MM.DD | MMDD | YYYY-MM-DD


class JobResult(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.pending
    total_files: int = 0
    processed_files: int = 0
    dates_found: Dict[str, int] = Field(default_factory=dict)
    target_shares: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    logs: List[str] = Field(default_factory=list)
    message: str = ""
    created_at: str = ""
    completed_at: str = ""


class TestConnectionResult(BaseModel):
    success: bool
    message: str
