"""
Persistent storage for NAS shares, app settings, and job history.
All data lives in /data (mounted Docker volume).
"""
import hashlib
import hmac as _hmac
import json
import os
import threading
from typing import Dict, List, Optional

from .models import NASShare, AppSettings, JobResult

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")

_lock = threading.Lock()


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Config (shares + settings)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    _ensure_data_dir()
    if not os.path.exists(CONFIG_FILE):
        return {"shares": [], "settings": {}}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def _save_config(data: dict):
    _ensure_data_dir()
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


# --- Shares ---

def get_shares() -> List[NASShare]:
    with _lock:
        cfg = _load_config()
        return [NASShare(**s) for s in cfg.get("shares", [])]


def get_share(share_id: str) -> Optional[NASShare]:
    for s in get_shares():
        if s.id == share_id:
            return s
    return None


def save_share(share: NASShare):
    with _lock:
        cfg = _load_config()
        shares = cfg.get("shares", [])
        idx = next((i for i, s in enumerate(shares) if s["id"] == share.id), None)
        share_dict = share.model_dump()
        if idx is not None:
            shares[idx] = share_dict
        else:
            shares.append(share_dict)
        cfg["shares"] = shares
        _save_config(cfg)


def delete_share(share_id: str) -> bool:
    with _lock:
        cfg = _load_config()
        shares = cfg.get("shares", [])
        new_shares = [s for s in shares if s["id"] != share_id]
        if len(new_shares) == len(shares):
            return False
        cfg["shares"] = new_shares
        _save_config(cfg)
        return True


# --- Settings ---

def get_settings() -> AppSettings:
    with _lock:
        cfg = _load_config()
        return AppSettings(**cfg.get("settings", {}))


def save_settings(settings: AppSettings):
    with _lock:
        cfg = _load_config()
        cfg["settings"] = settings.model_dump()
        _save_config(cfg)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _load_jobs() -> Dict[str, dict]:
    _ensure_data_dir()
    if not os.path.exists(JOBS_FILE):
        return {}
    with open(JOBS_FILE, "r") as f:
        return json.load(f)


def _save_jobs(jobs: Dict[str, dict]):
    _ensure_data_dir()
    with open(JOBS_FILE, "w") as f:
        json.dump(jobs, f, indent=2)


def get_job(job_id: str) -> Optional[JobResult]:
    with _lock:
        jobs = _load_jobs()
        if job_id not in jobs:
            return None
        return JobResult(**jobs[job_id])


def save_job(job: JobResult):
    with _lock:
        jobs = _load_jobs()
        jobs[job.job_id] = job.model_dump()
        _save_jobs(jobs)


def get_all_jobs() -> List[JobResult]:
    with _lock:
        jobs = _load_jobs()
        results = [JobResult(**v) for v in jobs.values()]
        results.sort(key=lambda j: j.created_at, reverse=True)
        return results


# ---------------------------------------------------------------------------
# Auth (password hashing + stored credentials)
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 600_000


def _hash_password(password: str, salt: bytes) -> str:
    """PBKDF2-HMAC-SHA256, returns hex digest."""
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, _PBKDF2_ITERATIONS
    )
    return dk.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    """Constant-time comparison of password against stored hash."""
    salt = bytes.fromhex(salt_hex)
    candidate = _hash_password(password, salt)
    return _hmac.compare_digest(candidate, hash_hex)


def init_auth(username: str, password: str):
    """Seed hashed credentials from env vars on first run.
    Skips if auth already exists in config."""
    with _lock:
        cfg = _load_config()
        if "auth" in cfg:
            return
        salt = os.urandom(32)
        cfg["auth"] = {
            "username": username,
            "password_hash": _hash_password(password, salt),
            "salt": salt.hex(),
        }
        _save_config(cfg)


def verify_credentials(username: str, password: str) -> bool:
    """Check username + password against stored hash."""
    with _lock:
        cfg = _load_config()
    auth = cfg.get("auth")
    if not auth:
        return False
    if not _hmac.compare_digest(username, auth["username"]):
        return False
    return _verify_password(password, auth["salt"], auth["password_hash"])


def change_password(new_password: str):
    """Generate new salt, hash, and save to config."""
    salt = os.urandom(32)
    with _lock:
        cfg = _load_config()
        auth = cfg.get("auth")
        if not auth:
            return
        auth["password_hash"] = _hash_password(new_password, salt)
        auth["salt"] = salt.hex()
        cfg["auth"] = auth
        _save_config(cfg)


def get_stored_username() -> Optional[str]:
    with _lock:
        cfg = _load_config()
    auth = cfg.get("auth")
    return auth["username"] if auth else None
