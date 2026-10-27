"""Image loading utilities."""

import base64
from pathlib import Path

from .data_path import data_path


def load_logo_base64(logo_path: Path = None) -> str:
    """Load an image and return as base64 data URI.

    The default is the registry name `email-signature-logo`, so the address
    follows any move of it. Built as `get_datastore_dir() / "brand" / ...` it
    raised `LegacyHomeVacated` the day the HEADING OS 2 move took the records,
    and a hand-built tail under the kind's home would miss the C3 move the same
    way; every caller here loads a logo into a document it is about to send or
    publish.

    Args:
        logo_path: Path to image file. Defaults to standard logo location.

    Returns:
        Base64 data URI string, or empty string if file not found.
    """
    if logo_path is None:
        # By its registry name (C3, 2026-10-13), the file the signature embeds.
        logo_path = data_path("email-signature-logo")
    if logo_path.exists():
        data = logo_path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        ext = logo_path.suffix.lstrip(".").lower()
        mime = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "svg": "image/svg+xml",
        }.get(ext, "image/png")
        return f"data:{mime};base64,{b64}"
    return ""
