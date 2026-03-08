"""
NAS SMB share operations using smbprotocol (SMBv2/v3).
Preserves file creation and modification timestamps on the NAS after each upload.
"""
import os
from typing import Callable, Optional

import smbclient
import smbclient.path
from smbclient._os import _set_basic_information as _set_basic_info

from .models import NASShare


def _smb_path(share: NASShare, *parts: str) -> str:
    """Build a UNC path: \\\\ip\\share_name\\share.path\\...parts"""
    base = share.path.strip("/").replace("/", "\\")
    joined = "\\".join(p.strip("/\\").replace("/", "\\") for p in parts if p.strip("/\\"))
    if base and joined:
        return f"\\\\{share.ip}\\{share.share_name}\\{base}\\{joined}"
    elif base:
        return f"\\\\{share.ip}\\{share.share_name}\\{base}"
    elif joined:
        return f"\\\\{share.ip}\\{share.share_name}\\{joined}"
    else:
        return f"\\\\{share.ip}\\{share.share_name}"


def _register(share: NASShare):
    """Register (or re-use) an SMB session for this share."""
    smbclient.register_session(
        share.ip,
        username=share.username,
        password=share.password,
        port=445,
    )


def test_connection(share: NASShare) -> tuple[bool, str]:
    """Test SMB connection to a share. Returns (success, message)."""
    try:
        _register(share)
        root = _smb_path(share)
        smbclient.listdir(root)
        return True, "Connection successful"
    except Exception as e:
        return False, f"Error: {e}"


def _ensure_remote_dirs(share: NASShare, remote_unc: str):
    """Recursively create remote directories if they don't exist."""
    share_root = f"\\\\{share.ip}\\{share.share_name}"
    rel = remote_unc[len(share_root):].lstrip("\\")
    parts = [p for p in rel.split("\\") if p]
    current = share_root
    for part in parts:
        current = current + "\\" + part
        if not smbclient.path.isdir(current):
            smbclient.mkdir(current)


def copy_folder_to_share(
    share: NASShare,
    local_folder: str,
    remote_name: str | None = None,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> int:
    """
    Copy the entire local_folder tree to the NAS share.
    The folder is placed inside share.path.
    File creation and modification times are preserved after each upload.

    Returns the number of files copied.
    """
    _register(share)
    files_copied = 0

    local_folder = os.path.abspath(local_folder)
    folder_name = remote_name or os.path.basename(local_folder)

    base_remote = _smb_path(share, folder_name)
    _ensure_remote_dirs(share, base_remote)

    for root, dirs, files in os.walk(local_folder):
        rel = os.path.relpath(root, local_folder)
        if rel == ".":
            remote_dir = base_remote
        else:
            rel_smb = rel.replace(os.sep, "\\")
            remote_dir = base_remote + "\\" + rel_smb

        _ensure_remote_dirs(share, remote_dir)

        for filename in files:
            local_file = os.path.join(root, filename)
            remote_file = remote_dir + "\\" + filename

            local_mtime = os.path.getmtime(local_file)

            with open(local_file, "rb") as local_fh:
                with smbclient.open_file(remote_file, mode="wb") as remote_fh:
                    while True:
                        chunk = local_fh.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        remote_fh.write(chunk)

            # Preserve creation and modification times on the NAS
            try:
                filetime = int(local_mtime * 10000000) + 116444736000000000
                _set_basic_info(
                    remote_file,
                    creation_time=filetime,
                    last_access_time=filetime,
                    last_write_time=filetime,
                    file_attributes=0,
                )
            except Exception:
                pass  # Non-fatal: file is copied, timestamps may not be set

            files_copied += 1
            if progress_callback:
                progress_callback(files_copied)

    return files_copied
