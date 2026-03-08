"""
Discord webhook notifications for Foto jobs.
"""
import httpx
from typing import Dict, List, Optional


def _post(webhook_url: str, payload: dict):
    """Fire-and-forget POST to Discord webhook."""
    try:
        with httpx.Client(timeout=10) as client:
            client.post(webhook_url, json=payload)
    except Exception:
        pass  # Never let Discord failures break the job


def notify_start(webhook_url: Optional[str], total_files: int, share_names: list[str]):
    if not webhook_url:
        return
    share_list = ", ".join(share_names) if share_names else "none"
    payload = {
        "embeds": [
            {
                "title": "Foto — Job Started",
                "color": 0x3B82F6,
                "fields": [
                    {"name": "Files", "value": str(total_files), "inline": True},
                    {"name": "Destinations", "value": share_list, "inline": False},
                ],
            }
        ]
    }
    _post(webhook_url, payload)


def notify_success(
    webhook_url: Optional[str],
    dates_found: Dict[str, int],
    share_names: list[str],
):
    if not webhook_url:
        return

    total = sum(dates_found.values())
    date_lines = "\n".join(
        f"• {date}: {count} photo{'s' if count != 1 else ''}"
        for date, count in sorted(dates_found.items())
    )
    share_list = ", ".join(share_names) if share_names else "none"

    payload = {
        "embeds": [
            {
                "title": "Foto — Job Complete",
                "color": 0x22C55E,
                "fields": [
                    {"name": "Total Photos", "value": str(total), "inline": True},
                    {"name": "Copied To", "value": share_list, "inline": False},
                    {"name": "Photos by Date", "value": date_lines or "—", "inline": False},
                ],
            }
        ]
    }
    _post(webhook_url, payload)


def notify_error(
    webhook_url: Optional[str],
    error_message: str,
):
    if not webhook_url:
        return
    payload = {
        "embeds": [
            {
                "title": "Foto — Job Failed",
                "color": 0xEF4444,
                "fields": [
                    {"name": "Error", "value": error_message[:1024], "inline": False},
                ],
            }
        ]
    }
    _post(webhook_url, payload)


def send_reset_code(webhook_url: Optional[str], code: str):
    if not webhook_url:
        return
    payload = {
        "embeds": [
            {
                "title": "Foto — Password Reset Code",
                "color": 0xF59E0B,
                "fields": [
                    {"name": "Reset Code", "value": f"**{code}**", "inline": True},
                    {"name": "Expires", "value": "15 minutes", "inline": True},
                ],
                "footer": {"text": "If you did not request this, ignore it."},
            }
        ]
    }
    _post(webhook_url, payload)
