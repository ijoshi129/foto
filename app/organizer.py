"""
Photo and video organizer: extracts EXIF/metadata dates, sorts into dated
subfolders, handles duplicate filenames.

Supported media:
  - Images (JPEG, PNG, HEIC, TIFF, ...): EXIF DateTimeOriginal via Pillow + piexif
  - RAW camera files (CR2, NEF, ARW, DNG, RAF, ...): same EXIF path
  - Videos (MP4, MOV, MTS, AVI, ...): creation_date metadata via hachoir
  - Any other file: falls back to filesystem mtime
"""
import os
import shutil
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from PIL import Image
import piexif

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

_HACHOIR_AVAILABLE = False
try:
    from hachoir.parser import createParser as _hachoir_createParser
    from hachoir.metadata import extractMetadata as _hachoir_extractMetadata
    _HACHOIR_AVAILABLE = True
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Known video extensions (lower-case, with leading dot)
# ---------------------------------------------------------------------------
_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi", ".mkv",
    ".wmv", ".flv", ".webm", ".3gp", ".3g2",
}


def _is_video(file_path: str) -> bool:
    ext = os.path.splitext(file_path)[1].lower()
    return ext in _VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

def _get_video_date(file_path: str) -> Optional[datetime]:
    if not _HACHOIR_AVAILABLE:
        return None
    try:
        parser = _hachoir_createParser(file_path)
        if not parser:
            return None
        with parser:
            metadata = _hachoir_extractMetadata(parser)
        if not metadata:
            return None
        dt = metadata.get("creation_date")
        if dt is None:
            return None
        if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _get_image_date(file_path: str) -> Optional[datetime]:
    try:
        img = Image.open(file_path)
        exif_bytes = img.info.get("exif")
        if exif_bytes:
            exif_data = piexif.load(exif_bytes)
            dto = exif_data.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
            if dto:
                date_str = dto.decode("utf-8") if isinstance(dto, bytes) else dto
                return datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _get_date(file_path: str) -> datetime:
    dt: Optional[datetime] = None

    if _is_video(file_path):
        dt = _get_video_date(file_path)
    else:
        dt = _get_image_date(file_path)

    if dt is not None:
        return dt

    return datetime.fromtimestamp(os.path.getmtime(file_path))


# ---------------------------------------------------------------------------
# Filename deduplication
# ---------------------------------------------------------------------------

def _safe_filename(dest_dir: str, filename: str) -> str:
    dest_path = os.path.join(dest_dir, filename)
    if not os.path.exists(dest_path):
        return filename

    stem, ext = os.path.splitext(filename)
    counter = 1
    while True:
        new_name = f"{stem}_{counter}{ext}"
        if not os.path.exists(os.path.join(dest_dir, new_name)):
            return new_name
        counter += 1


# ---------------------------------------------------------------------------
# Date-folder format map
# ---------------------------------------------------------------------------

_FORMAT_MAP = {
    "MM-DD":      "%m-%d",
    "MM.DD":      "%m.%d",
    "MMDD":       "%m%d",
    "YYYY-MM-DD": "%Y-%m-%d",
}


# ---------------------------------------------------------------------------
# Main organizer
# ---------------------------------------------------------------------------

def organize_photos(
    source_files: List[Tuple[str, str]],  # list of (original_filename, tmp_path)
    staging_root: str,
    date_folder_format: str = "MM-DD",
) -> Tuple[List[str], Dict[str, int]]:
    strftime_fmt = _FORMAT_MAP.get(date_folder_format, _FORMAT_MAP.get("YYYY-MM-DD"))
    dates_found: Dict[str, int] = {}
    date_dirs: Dict[str, str] = {}

    file_date_map: Dict[str, datetime] = {}
    for orig_name, tmp_path in source_files:
        file_date_map[tmp_path] = _get_date(tmp_path)

    for orig_name, tmp_path in source_files:
        dt = file_date_map[tmp_path]
        date_str = dt.strftime(strftime_fmt)

        if date_str not in date_dirs:
            dest_subdir = os.path.join(staging_root, date_str)
            os.makedirs(dest_subdir, exist_ok=True)
            date_dirs[date_str] = dest_subdir

        dest_subdir = date_dirs[date_str]
        safe_name = _safe_filename(dest_subdir, orig_name)
        dest_path = os.path.join(dest_subdir, safe_name)
        shutil.copy2(tmp_path, dest_path)

        # Set mtime to the original capture date so nas.py preserves it
        epoch = dt.timestamp()
        os.utime(dest_path, (epoch, epoch))

        dates_found[date_str] = dates_found.get(date_str, 0) + 1

    return list(date_dirs.values()), dates_found
