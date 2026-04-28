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
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")  # legacy, migrated on first read
JOBS_DIR = os.path.join(DATA_DIR, "jobs")

_lock = threading.Lock()
_migrated_legacy_jobs = False


def _ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def _atomic_write_json(path: str, data) -> None:
    """Write JSON durably: temp file → fsync → os.replace (atomic on POSIX)."""
    _ensure_data_dir()
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_json(path: str, default):
    """Load JSON, falling back to the .tmp sibling if the main file is missing
    or unreadable (e.g. interrupted mid-rename on a previous run)."""
    _ensure_data_dir()
    for candidate in (path, f"{path}.tmp"):
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
    return default


# ---------------------------------------------------------------------------
# Config (shares + settings)
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    return _load_json(CONFIG_FILE, {"shares": [], "settings": {}})


def _save_config(data: dict):
    _atomic_write_json(CONFIG_FILE, data)


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

def _ensure_jobs_dir():
    os.makedirs(JOBS_DIR, exist_ok=True)


def _job_path(job_id: str) -> str:
    # job_id is a uuid4 we generated, so it's filesystem-safe by construction.
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def _migrate_legacy_jobs_locked():
    """One-time split of the old monolithic jobs.json into per-job files.
    Idempotent and cheap once complete. Caller holds _lock."""
    global _migrated_legacy_jobs
    if _migrated_legacy_jobs:
        return
    if not os.path.exists(JOBS_FILE):
        _migrated_legacy_jobs = True
        return
    try:
        with open(JOBS_FILE, "r") as f:
            jobs = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Old file is unreadable — nothing to migrate, mark done so we don't
        # keep retrying on every call.
        _migrated_legacy_jobs = True
        return
    _ensure_jobs_dir()
    for jid, jdata in (jobs or {}).items():
        path = _job_path(jid)
        if os.path.exists(path):
            continue  # already migrated on a prior partial run
        try:
            _atomic_write_json(path, jdata)
        except Exception:
            pass
    try:
        os.replace(JOBS_FILE, JOBS_FILE + ".migrated")
    except Exception:
        pass
    _migrated_legacy_jobs = True


def get_job(job_id: str) -> Optional[JobResult]:
    with _lock:
        _migrate_legacy_jobs_locked()
        data = _load_json(_job_path(job_id), None)
        if data is None:
            return None
        try:
            return JobResult(**data)
        except Exception:
            return None


def save_job(job: JobResult):
    with _lock:
        _migrate_legacy_jobs_locked()
        _ensure_jobs_dir()
        _atomic_write_json(_job_path(job.job_id), job.model_dump())


def get_all_jobs() -> List[JobResult]:
    with _lock:
        _migrate_legacy_jobs_locked()
        _ensure_jobs_dir()
        results: List[JobResult] = []
        try:
            entries = os.listdir(JOBS_DIR)
        except FileNotFoundError:
            entries = []
        for name in entries:
            # Skip our atomic-write temp files (".json.tmp") and anything that
            # isn't a per-job file.
            if not name.endswith(".json") or name.endswith(".json.tmp"):
                continue
            data = _load_json(os.path.join(JOBS_DIR, name), None)
            if data is None:
                continue
            try:
                results.append(JobResult(**data))
            except Exception:
                continue
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
