"""
Foto — FastAPI application entry point.
"""
import os
import random
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import List

import httpx

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .models import (
    AppSettings,
    JobResult,
    JobStatus,
    NASShare,
    NASShareCreate,
    NASShareUpdate,
    TestConnectionResult,
)
from . import storage, organizer, nas, discord

# ---------------------------------------------------------------------------
# Lifespan — seed auth on first boot
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.init_auth(
        os.environ.get("FOTO_USERNAME", "admin"),
        os.environ.get("FOTO_PASSWORD", "changeme"),
    )
    yield

app = FastAPI(
    title="Foto",
    description="Organize camera SD card photos by EXIF date and copy to NAS drives.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)):
    if not storage.verify_credentials(credentials.username, credentials.password):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic realm=\"Foto\""},
        )
    return credentials.username

# Serve static files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    index = os.path.join(STATIC_DIR, "index.html")
    with open(index, "r") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings", response_model=AppSettings)
def get_settings(_: str = Depends(require_auth)):
    return storage.get_settings()


@app.put("/api/settings", response_model=AppSettings)
def update_settings(settings: AppSettings, _: str = Depends(require_auth)):
    storage.save_settings(settings)
    return settings


@app.post("/api/settings/test-discord")
def test_discord(_: str = Depends(require_auth)):
    s = storage.get_settings()
    if not s.discord_webhook_url:
        raise HTTPException(status_code=400, detail="No Discord webhook URL configured.")
    payload = {
        "embeds": [
            {
                "title": "Foto — Test Notification",
                "description": "Your Discord webhook is configured correctly.",
                "color": 0x137FEC,
            }
        ]
    }
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(s.discord_webhook_url, json=payload)
        if r.status_code in (200, 204):
            return {"ok": True}
        raise HTTPException(status_code=502, detail=f"Discord returned {r.status_code}: {r.text}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---------------------------------------------------------------------------
# Password reset / change
# ---------------------------------------------------------------------------

_reset_state: dict = {}  # in-memory only: {code, expires, attempts}
_reset_requests: list = []  # timestamps of recent code requests
_RESET_RATE_WINDOW = 15 * 60  # 15 minutes
_RESET_RATE_MAX = 3
_RESET_CODE_TTL = 15 * 60  # 15 minutes
_RESET_MAX_ATTEMPTS = 5
_sysrandom = random.SystemRandom()


class ResetRequest(BaseModel):
    code: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/forgot-password")
def forgot_password():
    now = time.time()
    # Prune old entries
    _reset_requests[:] = [t for t in _reset_requests if now - t < _RESET_RATE_WINDOW]
    if len(_reset_requests) >= _RESET_RATE_MAX:
        raise HTTPException(status_code=429, detail="Too many reset requests. Try again later.")
    _reset_requests.append(now)

    code = str(_sysrandom.randint(100000, 999999))
    _reset_state.clear()
    _reset_state.update({"code": code, "expires": now + _RESET_CODE_TTL, "attempts": 0})

    # Always log to stdout (visible via docker logs)
    username = storage.get_stored_username() or "unknown"
    print(f"\n{'='*50}")
    print(f"  FOTO PASSWORD RESET CODE: {code}")
    print(f"  For user: {username}")
    print(f"  Expires in 15 minutes.")
    print(f"{'='*50}\n", flush=True)

    # Optionally send via Discord
    settings = storage.get_settings()
    discord.send_reset_code(settings.discord_webhook_url, code)

    return {"ok": True, "message": "Reset code sent. Check container logs or Discord."}


@app.post("/api/reset-password")
def reset_password(body: ResetRequest):
    if not _reset_state:
        raise HTTPException(status_code=400, detail="No reset code has been requested.")
    if time.time() > _reset_state.get("expires", 0):
        _reset_state.clear()
        raise HTTPException(status_code=400, detail="Reset code has expired. Request a new one.")
    if _reset_state.get("attempts", 0) >= _RESET_MAX_ATTEMPTS:
        _reset_state.clear()
        raise HTTPException(status_code=400, detail="Too many wrong attempts. Request a new code.")

    if body.code != _reset_state.get("code"):
        _reset_state["attempts"] = _reset_state.get("attempts", 0) + 1
        remaining = _RESET_MAX_ATTEMPTS - _reset_state["attempts"]
        if remaining <= 0:
            _reset_state.clear()
            raise HTTPException(status_code=400, detail="Too many wrong attempts. Request a new code.")
        raise HTTPException(status_code=400, detail=f"Invalid code. {remaining} attempt(s) remaining.")

    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    storage.change_password(body.new_password)
    _reset_state.clear()
    return {"ok": True, "message": "Password has been reset successfully."}


@app.put("/api/password")
def change_password(body: ChangePasswordRequest, user: str = Depends(require_auth)):
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if not storage.verify_credentials(user, body.current_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    storage.change_password(body.new_password)
    return {"ok": True, "message": "Password updated successfully."}


# ---------------------------------------------------------------------------
# NAS Shares
# ---------------------------------------------------------------------------

@app.get("/api/shares", response_model=List[NASShare])
def list_shares(_: str = Depends(require_auth)):
    return storage.get_shares()


@app.post("/api/shares", response_model=NASShare, status_code=201)
def create_share(payload: NASShareCreate, _: str = Depends(require_auth)):
    share = NASShare(**payload.model_dump())
    storage.save_share(share)
    return share


@app.put("/api/shares/{share_id}", response_model=NASShare)
def update_share(share_id: str, payload: NASShareUpdate, _: str = Depends(require_auth)):
    share = storage.get_share(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    updated = share.model_copy(update={k: v for k, v in payload.model_dump().items() if v is not None})
    storage.save_share(updated)
    return updated


@app.delete("/api/shares/{share_id}", status_code=204)
def delete_share(share_id: str, _: str = Depends(require_auth)):
    if not storage.delete_share(share_id):
        raise HTTPException(status_code=404, detail="Share not found")


@app.post("/api/shares/{share_id}/test", response_model=TestConnectionResult)
def test_share(share_id: str, _: str = Depends(require_auth)):
    share = storage.get_share(share_id)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    success, message = nas.test_connection(share)
    return TestConnectionResult(success=success, message=message)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@app.get("/api/jobs", response_model=List[JobResult])
def list_jobs(_: str = Depends(require_auth)):
    return storage.get_all_jobs()


@app.get("/api/jobs/{job_id}", response_model=JobResult)
def get_job(job_id: str, _: str = Depends(require_auth)):
    job = storage.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# Organize endpoint (main action)
# ---------------------------------------------------------------------------

def _log(job, message: str) -> None:
    """Append a timestamped log line to the job and save."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    job.logs.append(f"[{ts}] {message}")
    job.message = message
    storage.save_job(job)


def _run_job(
    job_id: str,
    share_ids: List[str],
    staged_files: List[tuple],  # [(original_name, tmp_path), ...]
    staging_dir: str,
):
    """Background task: organize photos, copy to shares, notify Discord."""
    job = storage.get_job(job_id)
    settings = storage.get_settings()
    shares = [storage.get_share(sid) for sid in share_ids]
    shares = [s for s in shares if s is not None]
    share_names = [s.name for s in shares]

    _log(job, f"Processing {len(staged_files)} uploaded file(s)")

    # --- Notify start ---
    discord.notify_start(settings.discord_webhook_url, len(staged_files), share_names)

    try:
        # --- Organize ---
        _log(job, "Analyzing EXIF / metadata to sort photos by date...")

        date_dirs, dates_found = organizer.organize_photos(
            staged_files, staging_dir,
            date_folder_format=settings.date_folder_format,
        )

        total = sum(dates_found.values())
        job.dates_found = dates_found
        job.processed_files = total
        storage.save_job(job)
        _log(job, f"Organised {total} file(s) into {len(dates_found)} date folder(s)")

        # --- Copy each date folder to each share ---
        for share in shares:
            _log(job, f"Connecting to NAS share \"{share.name}\"...")
            share_counter = [0]

            for date_dir in date_dirs:
                rel_path = os.path.relpath(date_dir, staging_dir)
                _log(job, f"Copying {rel_path} → {share.name}...")
                prev = [0]

                def _progress(n: int, _prev=prev, _sc=share_counter) -> None:
                    delta = n - _prev[0]
                    _prev[0] = n
                    _sc[0] += delta

                nas.copy_folder_to_share(share, date_dir, remote_name=rel_path, progress_callback=_progress)

            _log(job, f"Done copying to {share.name} ({share_counter[0]} file(s))")

        # --- Done ---
        job.status = JobStatus.done
        job.completed_at = datetime.now(timezone.utc).isoformat()
        _log(job, "All done.")

        discord.notify_success(settings.discord_webhook_url, dates_found, share_names)

    except Exception as e:
        err = str(e)
        job.status = JobStatus.error
        job.errors.append(err)
        job.completed_at = datetime.now(timezone.utc).isoformat()
        _log(job, f"Error: {err}")
        discord.notify_error(settings.discord_webhook_url, err)

    finally:
        try:
            shutil.rmtree(staging_dir, ignore_errors=True)
        except Exception:
            pass


@app.post("/api/organize", response_model=JobResult, status_code=202)
async def organize(
    background_tasks: BackgroundTasks,
    share_ids: str = Form(...),          # comma-separated share UUIDs
    files: List[UploadFile] = File(...),
    _: str = Depends(require_auth),
):
    selected_ids = [s.strip() for s in share_ids.split(",") if s.strip()]
    if not selected_ids:
        raise HTTPException(status_code=400, detail="At least one share must be selected")

    job_id = str(uuid.uuid4())
    staging_dir = tempfile.mkdtemp(prefix=f"foto_{job_id}_")

    job = JobResult(
        job_id=job_id,
        status=JobStatus.running,
        total_files=len(files),
        target_shares=selected_ids,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    job.logs.append(f"[{ts}] Receiving {len(files)} file(s) from browser...")
    job.message = f"Uploading {len(files)} file(s)..."
    storage.save_job(job)

    _UPLOAD_CHUNK = 8 * 1024 * 1024  # 8 MB read chunks
    staged: List[tuple] = []
    for upload in files:
        dest = os.path.join(staging_dir, upload.filename or "unnamed")
        with open(dest, "wb") as f:
            while True:
                chunk = await upload.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                f.write(chunk)
        staged.append((upload.filename or "unnamed", dest))

    background_tasks.add_task(
        _run_job, job_id, selected_ids, staged, staging_dir
    )

    return job
