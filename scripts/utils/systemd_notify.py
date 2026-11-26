"""systemd notify: one datagram to `NOTIFY_SOCKET`, for daemons run as `Type=notify` units.

Shared since 2026-11-22 (HOS-408), so every daemon run as a `Type=notify`
unit sends READY=1 and WATCHDOG=1 through one copy.
"""
from __future__ import annotations

import logging
import os
import socket

_warned: set[str] = set()


def sd_notify(message: str, logger: logging.Logger | None = None) -> bool:
    """Send `message` to systemd's NOTIFY_SOCKET; False when unset or on any OSError, never raises.

    A failure is logged once per socket address through `logger`, never again.
    """
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode("utf-8"), address)
    except OSError as exc:
        if address not in _warned:
            (logger or logging.getLogger(__name__)).warning(
                "sd_notify to %s failed: %s", address, exc)
            _warned.add(address)
        return False
    return True
