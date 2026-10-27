"""Merged config loader: engine defaults + the per-user override layer.

The per-user layer lives in the data repository's `runtime:daemon-config/state`
(moved out of the engine checkout 2026-09-25, HOS-5 section 7). A tree with no
data repository has no user layer and runs on the defaults.
"""
import logging
from pathlib import Path
from typing import Any
import yaml

USER_CONFIG_NAME = "config.yaml"


def _config_dir() -> Path:
    """`runtime:daemon-config/state`; raises `DataPathError` with no data repository.

    Imported here: this package is loaded by daemons and CLIs alike, and only
    the config path needs the front door.
    """
    from scripts.utils.data_path import data_path

    return data_path("runtime:daemon-config/state")


DEFAULTS = {
    "version": 0,
    # Resolved through the operator seam in load_config() (see below); empty here
    # so no operator-identity literal lives in the engine defaults.
    "user_slug": "",
    "daemon": {},
}

logger = logging.getLogger(__name__)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def _load_layer(path: Path, label: str) -> dict:
    """Parse one config layer. A broken layer is SKIPPED, never fatal.

    `yaml.safe_load` errors used to propagate, so a per-user `config.yaml`
    saved mid-edit stopped the daemon from booting at all. A config
    override is the least important input here; refusing to start over one is
    the wrong trade. `read_text()` also used the locale default encoding, which
    misdecodes a UTF-8 file on a non-UTF-8 host.
    """
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        logger.warning("%s config layer at %s is unreadable; ignoring it and "
                       "keeping the layers below", label, path, exc_info=True)
        return {}
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        logger.warning("%s config layer at %s is a %s, not a mapping; ignoring it",
                       label, path, type(parsed).__name__)
        return {}
    return parsed


def load_config(workspace_root: Path) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    user = _user_path(workspace_root)
    if user is not None and user.exists():
        cfg = _deep_merge(cfg, _load_layer(user, "user"))
    # Resolve the operator's slug through the identity seam when no config layer
    # set it (operator.yaml / env; generic "operator" on a fresh clone).
    if not cfg.get("user_slug"):
        from scripts.utils.operator_identity import operator_slug
        cfg["user_slug"] = operator_slug()
    return cfg


def _user_path(workspace_root: Path) -> Path | None:
    """The per-user layer, or None on a tree with no data repository.

    The layer is in the data repository whatever root the caller names;
    `workspace_root` stays in the signature the callers use.
    """
    from scripts.utils.data_path import DataPathError

    try:
        return _config_dir() / USER_CONFIG_NAME
    except DataPathError as exc:
        logger.debug("config: no user layer on this tree: %s", exc)
        return None
