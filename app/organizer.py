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
# Files for which hachoir is the primary date source (video + audio
# containers). Anything else is tried as an image first, with hachoir as a
# fallback so unusual extensions still get a chance at embedded metadata.
# ---------------------------------------------------------------------------
_HACHOIR_PRIMARY_EXTENSIONS = {
    # Video
    ".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi", ".mkv",
    ".wmv", ".flv", ".webm", ".3gp", ".3g2",
    # Audio
    ".wav", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".wma",
}


def _hachoir_first(file_path: str) -> bool:
    return os.path.splitext(file_path)[1].lower() in _HACHOIR_PRIMARY_EXTENSIONS


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

def _get_hachoir_date(file_path: str) -> Optional[datetime]:
    """Extract creation_date via hachoir. Works for video containers (MP4,
    MOV, MTS, AVI, MKV, ...) and audio formats with metadata (WAV/BEXT,
    MP3/ID3, FLAC, OGG). Returns None if hachoir is unavailable, the file
    is unparseable, or no creation_date is present."""
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


_EXIF_DATE_TAGS = (
    36867,  # DateTimeOriginal — when the photo was taken (preferred)
    36868,  # DateTimeDigitized
    306,    # DateTime — last modified by camera/software
)


def _parse_exif_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return None
    value = str(value).strip().rstrip("\x00")
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _get_image_date(file_path: str) -> Optional[datetime]:
    # `with` ensures the underlying file handle is released — important when
    # processing thousands of files in one job.
    try:
        with Image.open(file_path) as img:
            # Path 1: piexif on the raw EXIF blob in img.info — works for
            # most JPEGs and is the fastest.
            exif_bytes = img.info.get("exif")
            if exif_bytes:
                try:
                    exif_data = piexif.load(exif_bytes)
                    dto = exif_data.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
                    dt = _parse_exif_dt(dto)
                    if dt is not None:
                        return dt
                except Exception:
                    pass

            # Path 2: Pillow's getexif() — covers HEIC, some PNGs, and JPEGs
            # whose EXIF lives in an APP segment piexif doesn't reach.
            try:
                exif = img.getexif()
            except Exception:
                exif = None
            if exif:
                for tag in _EXIF_DATE_TAGS:
                    dt = _parse_exif_dt(exif.get(tag))
                    if dt is not None:
                        return dt
    except Exception:
        pass
    return None


def _get_date(file_path: str) -> datetime:
    # Try in extension-appropriate order, but always cross-check the other
    # extractor when the first one fails — that way an audio file (e.g. WAV)
    # tried as image first still gets hachoir, and a misnamed image still
    # gets EXIF.
    extractors = (
        (_get_hachoir_date, _get_image_date)
        if _hachoir_first(file_path)
        else (_get_image_date, _get_hachoir_date)
    )
    for extractor in extractors:
        dt = extractor(file_path)
        if dt is not None:
            return dt

    # Last resort: filesystem mtime. The upload step sets the staging file's
    # mtime to the browser-reported `lastModified`, so for files lacking any
    # embedded date this still gives the user's actual file date — not the
    # upload time.
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
        # Same-filesystem move (staging tmp → date subdir under the same
        # staging root): becomes a rename, so we don't double disk usage on
        # large imports. shutil.move falls back to copy+unlink across FSes.
        shutil.move(tmp_path, dest_path)

        # Set mtime to the original capture date so nas.py preserves it
        epoch = dt.timestamp()
        os.utime(dest_path, (epoch, epoch))

        dates_found[date_str] = dates_found.get(date_str, 0) + 1

    return list(date_dirs.values()), dates_found
