#!/usr/bin/env python3
"""The accounts, and every address a name takes once per account.

An account is one directory under the registry name `accounts-root`, and its
directory name is the slug. A registry name spelled with `{account}`
(`account-proposals`, `account-threads`, ...) is ONE account's address of that
kind, so a reader that needs every account's threads cannot ask for the name
alone: it asks this module, which lists the slugs and resolves the name once per
slug. HOS-234 part 10, 2026-10-13: the C3 move spreads files of one kind over
many account directories, which a single repoint cannot carry.

A directory whose name starts with `_` or `.` is not an account: `_index/`
holds the account-wide indexes. Nor is a name that is not a single path segment
the resolver would accept as a parameter value.

    from scripts.utils.accounts import account_slugs, each_account
    for slug, home in each_account("account-threads"):
        ...

    python scripts/data-path.py accounts                  # the slugs
    python scripts/data-path.py each account-threads      # one address per account

A name with no parameter answers its one address, so a group may mix the two
shapes and a reader walks it with `addresses()` without asking which is which.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.utils.data_path import (  # noqa: E402
    PARAM_VALUE_RE,
    ParameterMismatch,
    data_path,
    load_registry,
)

PARAM = "account"


def accounts_root(data_root: Path | None = None) -> Path:
    """Where the account directories are: the registry name `accounts-root`."""
    return data_path("accounts-root", data_root=data_root)


def is_account_slug(name: str) -> bool:
    """A directory name that is an account: not `_`/`.`-prefixed, one segment."""
    return (not name.startswith(("_", ".")) and bool(PARAM_VALUE_RE.match(name)))


def account_slugs(data_root: Path | None = None) -> list[str]:
    """Every account's slug, sorted. Empty when the accounts root is not on disk."""
    root = accounts_root(data_root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir()
                  if p.is_dir() and not p.is_symlink() and is_account_slug(p.name))


def name_params(name: str, data_root: Path | None = None) -> tuple[str, ...]:
    """The parameters a registry name takes; `()` for a `record:`/`runtime:` name."""
    entry = load_registry(data_root).get(name)
    return tuple(entry["params"]) if entry else ()


def each_account(name: str, data_root: Path | None = None, *,
                 existing: bool = True) -> list[tuple[str, Path]]:
    """`(slug, address)` of an `{account}` name for every account, slug order.

    `existing` keeps only the addresses on disk. A name that takes any other
    parameter, or none, refuses: it is not account-scoped, and answering it
    once would let a caller believe it had walked every account.
    """
    params = name_params(name, data_root)
    if params != (PARAM,):
        raise ParameterMismatch(
            f"{name}: takes {list(params) or 'no parameters'}; only a name spelled "
            f"with exactly {{{PARAM}}} is walked once per account")
    out = []
    for slug in account_slugs(data_root):
        home = data_path(name, data_root=data_root, account=slug)  # leak-guard: ok (the per-account resolver: `name` is a registry name its caller spelled, and the guard above refuses any but an `{account}` name)
        if not existing or home.exists():
            out.append((slug, home))
    return out


def addresses(name: str, data_root: Path | None = None, *,
              existing: bool = False) -> list[Path]:
    """Every address of a name: one per account for an `{account}` name, else its one.

    A name taking a parameter other than `{account}` refuses, as `data_path`
    would, because there is no list of its values to walk.
    """
    if name_params(name, data_root) == (PARAM,):
        return [home for _, home in each_account(name, data_root, existing=existing)]
    one = data_path(name, data_root=data_root)  # leak-guard: ok (a registry group member or a caller-spelled registry name, resolved as data_homes.resolve does)
    return [one] if (not existing or one.exists()) else []


def account_of(path: Path, data_root: Path | None = None) -> str | None:
    """The account a path lies under, or None when it lies under none."""
    root = accounts_root(data_root).resolve()
    try:
        rel = Path(path).resolve().relative_to(root)
    except ValueError:
        return None
    head = rel.parts[0] if rel.parts else ""
    return head if is_account_slug(head) else None


__all__ = ["PARAM", "account_of", "account_slugs", "accounts_root", "addresses",
           "each_account", "is_account_slug", "name_params"]
