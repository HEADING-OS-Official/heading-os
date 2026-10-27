#!/usr/bin/env python3
"""Token usage of every model call made through the local proxy, by day, model and caller.

Reads the ledger `call_model` appends to (the registry name `proxy-usage-ledger`,
see `scripts/utils/proxy_usage.py`) and reports it the way `agentsview usage
daily` reports Claude Code: a terminal table, JSON, or a self-contained static
HTML dashboard. Beside the tokens it shows what those tokens would cost at the
providers' API list prices, from the public pydantic/genai-prices table (the
source agentsview prices by). That is a reference figure, not the bill: the
proxy fronts flat subscriptions. See "Prices" below for the table, the private
alias map and what is left unpriced.

Usage:
    python scripts/proxy-usage.py daily                      # last 30 days, one row per day
    python scripts/proxy-usage.py daily --breakdown          # plus one sub-row per model
    python scripts/proxy-usage.py daily --by caller          # sub-rows per calling script
    python scripts/proxy-usage.py daily --since 7d
    python scripts/proxy-usage.py daily --since 2026-01-01 --until 2026-01-31
    python scripts/proxy-usage.py daily --all --json
    python scripts/proxy-usage.py daily --html               # write the dashboard, print its paths
    python scripts/proxy-usage.py dashboard                  # rebuild it only if the ledger moved
    python scripts/proxy-usage.py dashboard --force          # rebuild it regardless
    python scripts/proxy-usage.py daily --refresh-prices     # fetch the price table now

The dashboard is ONE file at a fixed address, `proxy-usage.html` under the
registry name `proxy-usage-reports`, replaced atomically and refreshing itself
in the browser every 5 minutes. `dashboard` is what the user timer
`proxy-usage-dashboard.timer` runs every 5 minutes: it reads a stamp embedded in
the current page (ledger size and mtime, the local day, the window, the page
format) and exits 0 without writing when nothing in it changed, so an idle
ledger costs one stat and one 512-byte read.

The page carries the whole ledger history, aggregated per day, model, caller
and provider, as a JSON block, and an inline script (no network, no external
reference) draws it the way agentsview's Usage view does: Cost | Tokens,
Smooth | Lines | Bars, grouping by model, caller or provider (the provider the
price alias map names; "unknown" without one), 7 / 30 / 90 days or all, model
and caller filters. The state lives in the URL fragment, e.g.
`proxy-usage.html#mode=cost&chart=bars&group=caller&period=7`, so it survives the
5-minute reload. The HEADING OS mark and favicon (registry names
`heading-os-logo`, `heading-os-favicon`) are inlined from the data repository at
render time; without them the header shows the text alone.

Both commands first refresh the local copy of the price table when it is older
than 24 hours (`--refresh-prices` forces it, `--prices-url` or
PROXY_USAGE_PRICES_URL points it elsewhere). A failed fetch keeps the last copy
and says so; it never fails the report.

Days are bucketed in HEADING_OS_TZ unless --timezone names another IANA zone.
Every request counts, retries included, because each one costs tokens. A request
that ended in an error with no response is a call with no token counts.
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import re
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path

import yaml
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.utils.atomic import atomic_write_text  # noqa: E402
from scripts.utils.colors import BOLD, GRAY, RESET, YELLOW  # noqa: E402
from scripts.utils.data_path import (  # noqa: E402
    DataPathError, data_path, data_write_dir, data_write_file)
from scripts.utils.proxy_usage import ledger_path, read_rows  # noqa: E402
from scripts.utils.user_agent import UserAgentConfigError, user_agent  # noqa: E402
from scripts.utils.workspace import get_default_tz_name  # noqa: E402

DEFAULT_WINDOW_DAYS = 30
DASHBOARD_FILE = "proxy-usage.html"
# Bump when the page layout changes, so the timer rebuilds a page an older
# renderer wrote even though the ledger did not move.
DASHBOARD_FORMAT = 4
REFRESH_SECONDS = 300
_STAMP_RE = re.compile(r"<!-- proxy-usage-stamp: (\{.*?\}) -->")
METRICS = ("calls", "retries", "errors", "no_usage", "input", "cached", "output", "reasoning",
           "total", "duration_ms", "cost_micro_usd", "cost_input_micro_usd",
           "cost_cached_micro_usd", "cost_output_micro_usd", "unpriced")

# ============================================================
# Aggregation
# ============================================================


def _empty() -> dict:
    return dict.fromkeys(METRICS, 0)


def _add(acc: dict, row: dict, cost: int | None, parts: dict | None = None) -> None:
    def n(key):
        value = row.get(key)
        return value if isinstance(value, int) else 0

    acc["calls"] += 1
    acc["retries"] += 1 if row.get("retry") else 0
    acc["errors"] += 1 if str(row.get("outcome", "")).startswith("error") else 0
    acc["no_usage"] += 1 if not isinstance(row.get("total_tokens"), int) else 0
    acc["input"] += n("prompt_tokens")
    acc["cached"] += min(n("cached_tokens"), n("prompt_tokens"))
    acc["output"] += n("completion_tokens")
    acc["reasoning"] += n("reasoning_tokens")
    acc["total"] += n("total_tokens") or (n("prompt_tokens") + n("completion_tokens"))
    acc["duration_ms"] += n("duration_ms")
    if cost is None:
        acc["unpriced"] += 1
    else:
        acc["cost_micro_usd"] += cost
        for part in ("input", "cached", "output"):
            acc[f"cost_{part}_micro_usd"] += (parts or {}).get(part, 0)


def model_of(row: dict) -> str:
    """The model that answered: `served` when the proxy said, else the one asked for."""
    return str(row.get("served") or row.get("model") or "unknown")


def parse_bound(value: str, tz: ZoneInfo) -> date:
    """`28d` (that many days back from today) or `YYYY-MM-DD`."""
    m = re.fullmatch(r"(\d+)d", value.strip())
    if m:
        return datetime.now(tz).date() - timedelta(days=int(m.group(1)))
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise SystemExit(f"not a date or a duration like 28d: {value!r}") from None


def aggregate(rows: list[dict], tz: ZoneInfo, since: date | None, until: date | None,
              book: PriceBook | None = None) -> dict:
    days: dict[date, dict] = defaultdict(_empty)
    day_model: dict[date, dict[str, dict]] = defaultdict(lambda: defaultdict(_empty))
    day_caller: dict[date, dict[str, dict]] = defaultdict(lambda: defaultdict(_empty))
    models: dict[str, dict] = defaultdict(_empty)
    callers: dict[str, dict] = defaultdict(_empty)
    cells: dict[tuple, dict] = defaultdict(_empty)
    total = _empty()
    unparsed = 0
    cache_since: date | None = None
    failed_models: set[str] = set()
    for row in rows:
        try:
            day = datetime.fromisoformat(row["ts"]).astimezone(tz).date()
        except (ValueError, TypeError):
            unparsed += 1
            continue
        if (since and day < since) or (until and day > until):
            continue
        model, caller = model_of(row), str(row.get("caller") or "unknown")
        cached = row.get("cached_tokens")
        if isinstance(cached, int) and not isinstance(cached, bool) \
                and (cache_since is None or day < cache_since):
            cache_since = day
        try:
            parts = book.cost_parts(row) if book else None
            cost = round(sum(parts.values())) if parts is not None else None
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            cost = parts = None
            if model not in failed_models:
                failed_models.add(model)
                print(f"{YELLOW}{model}: its price entry could not be evaluated "
                      f"({type(exc).__name__}: {exc}); its calls are counted unpriced{RESET}",
                      file=sys.stderr)
        provider = book.provider_of(row) if book else "unknown"
        rounded = {k: round(v) for k, v in parts.items()} if parts is not None else None
        for acc in (days[day], day_model[day][model], day_caller[day][caller],
                    models[model], callers[caller], total, cells[(day, model, caller, provider)]):
            _add(acc, row, cost, rounded)
    return {"days": dict(sorted(days.items())), "day_model": day_model, "day_caller": day_caller,
            "models": _ranked(models), "callers": _ranked(callers), "total": total,
            "cells": cells, "cache_since": cache_since, "unparsed": unparsed}


def _ranked(table: dict[str, dict]) -> dict[str, dict]:
    return dict(sorted(table.items(), key=lambda kv: (-kv[1]["total"], -kv[1]["calls"], kv[0])))


# ============================================================
# Prices
# ============================================================
#
# The table is pydantic/genai-prices' `data.json`, the file agentsview prices
# by, downloaded here (never read out of agentsview) into the registry name
# `proxy-usage-prices`, at most once a day, replaced only by a copy that passes
# `table_problem`. Which row a proxy model is priced as is NOT guessed from the
# table's match rules: the private registry name `proxy-usage-price-aliases`
# says so per model, because the proxy's names (`kimi-for-coding`, `k3`) match
# no rule, and a model it does not map is unpriced. The engine ships no alias.
#
# The contract, read from the downloaded file itself and from upstream
# `packages/python/genai_prices/types.py` on 2026-10-25 (M039):
#   top level   a list of providers: {id, name, models: [...], ...}
#   model       {id, name, match, prices, price_variants?, context_window?, ...}
#   prices      either one mapping, or a list of {prices, constraint?}, where
#               the LAST entry whose constraint holds wins; a constraint is
#               {start_date: YYYY-MM-DD} (request's UTC date >= it) or
#               {start_time, end_time} (UTC time of day, the interval may wrap
#               past midnight); an entry with no constraint always holds
#   a price     USD per million tokens under `input_mtok`, `output_mtok`,
#               `cache_read_mtok`, `cache_write_mtok`, `output_reasoning_mtok`
#               and others; either a number, or {base, tiers: [{start, price}]},
#               where the highest tier whose `start` the request's input tokens
#               EXCEED applies to ALL tokens of that unit, not just those above
#   variants    `price_variants` (batch, flex, priority tiers) are not used:
#               the proxy sends standard requests
# For OpenAI-shaped usage, which is what the ledger holds, the input count
# includes cached tokens, and completion tokens include reasoning tokens. So a
# row costs (prompt - cached) at input + cached at cache read (input when the
# model has no cache-read price) + completion at output, with the reasoning part
# at `output_reasoning_mtok` only where the model prices it separately. Cache
# writes are not reported by OpenAI-shaped usage, so none are priced.
# tokens x USD-per-million-tokens is micro-dollars, the unit every sum here keeps.

PRICES_URL = "https://raw.githubusercontent.com/pydantic/genai-prices/main/prices/new_data/v2/data.json"
PRICES_MAX_AGE_S = 24 * 3600
PRICES_RETRY_S = 3600
PRICES_TIMEOUT_S = 15
PRICES_MAX_BYTES = 32 * 1024 * 1024
PRICES_MIN_PROVIDERS = 10
NOT_THE_BILL = "API list price for these tokens, not the subscription bill"
PRICED_UNITS = ("input_mtok", "output_mtok", "cache_read_mtok", "output_reasoning_mtok")


def _price_problem(value: object) -> str | None:
    """Why one price value is neither a number nor {base, tiers: [{start, price}]}."""
    def number(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if number(value):
        return None
    if isinstance(value, dict) and number(value.get("base")):
        tiers = value.get("tiers", [])
        if isinstance(tiers, list) and all(
                isinstance(t, dict) and isinstance(t.get("start"), int)
                and not isinstance(t.get("start"), bool) and number(t.get("price"))
                for t in tiers):
            return None
    return f"price {value!r} is neither a number nor a tiered price"


def price_entry_problem(model: dict) -> str | None:
    """Why a model's `prices` cannot be evaluated by `PriceBook.cost`; None when it can.

    Checks every entry's shape, its constraint and the price units the cost
    reads, so a malformed entry is refused before it replaces the last good copy
    and is marked unpriced if it is already in one."""
    prices = model.get("prices")
    entries = prices if isinstance(prices, list) else [{"prices": prices}]
    if not entries:
        return "no price entries"
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("prices"), dict):
            return "a price entry that is not {prices: {...}}"
        constraint = entry.get("constraint")
        if constraint is not None:
            if not isinstance(constraint, dict):
                return f"constraint {constraint!r} is not a mapping"
            try:
                if "start_date" in constraint:
                    date.fromisoformat(constraint["start_date"])
                if "start_time" in constraint or "end_time" in constraint:
                    dtime.fromisoformat(constraint["start_time"])
                    dtime.fromisoformat(constraint["end_time"])
            except (KeyError, TypeError, ValueError) as exc:
                return f"constraint {constraint!r} unreadable ({type(exc).__name__}: {exc})"
        for unit in PRICED_UNITS:
            if unit in entry["prices"]:
                problem = _price_problem(entry["prices"][unit])
                if problem:
                    return f"{unit}: {problem}"
    return None


def load_aliases(path: Path) -> tuple[dict, str | None]:
    """The alias map's `aliases:` mapping and a problem line (None when it loaded)."""
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}, f"no alias map at {path}; every call is unpriced"
    except (OSError, yaml.YAMLError) as exc:
        return {}, f"{path} unreadable ({exc}); every call is unpriced"
    aliases = doc.get("aliases") if isinstance(doc, dict) else None
    if not isinstance(aliases, dict):
        return {}, f"{path} has no `aliases:` mapping; every call is unpriced"
    return aliases, None


def _alias_key(target: object) -> tuple | None:
    return (target.get("provider"), target.get("model")) if isinstance(target, dict) else None


def table_problem(table: object) -> str | None:
    """Why `table` is not a genai-prices table this parser can read; None when it is."""
    if not isinstance(table, list) or len(table) < PRICES_MIN_PROVIDERS:
        return f"expected a list of at least {PRICES_MIN_PROVIDERS} providers"
    for prov in table:
        if not isinstance(prov, dict) or not isinstance(prov.get("id"), str) \
                or not isinstance(prov.get("models"), list):
            return "a provider without a string id and a models list"
        for model in prov["models"]:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                return f"a model of {prov['id']} without a string id"
            prices = model.get("prices")
            entries = prices if isinstance(prices, list) else [{"prices": prices}]
            if not entries or not all(isinstance(e, dict) and isinstance(e.get("prices"), dict)
                                      for e in entries):
                return f"{prov['id']}/{model['id']}: prices are neither a mapping nor a list of them"
    return None


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another https URL. urllib's default handler
    follows https -> http, which would bring the table in without TLS."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise urllib.error.HTTPError(newurl, code, f"redirect to non-https {newurl} refused",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_attempt(path: Path) -> dict | None:
    """The last fetch attempt, {at, ok, failures, problem}; None when absent or unreadable."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"{YELLOW}{path} unreadable ({exc}); treated as no earlier attempt{RESET}",
              file=sys.stderr)
        return None
    ok = (isinstance(state, dict) and isinstance(state.get("at"), (int, float))
          and isinstance(state.get("ok"), bool) and isinstance(state.get("failures"), int))
    return state if ok else None


def _attempt_due(attempt: dict | None, copy_mtime: float | None, now: float) -> bool:
    """At most one attempt per 24 h; after a failure, one retry no sooner than 1 h."""
    if attempt is None:
        return copy_mtime is None or now - copy_mtime >= PRICES_MAX_AGE_S
    since = now - attempt["at"]
    if not attempt["ok"] and attempt["failures"] == 1:
        return since >= PRICES_RETRY_S
    return since >= PRICES_MAX_AGE_S


def _fetch_table(url: str) -> tuple[str | None, str | None]:
    """(the table's text, None) or (None, why not). Never raises for a network
    or content failure; refuses a non-https URL or redirect, and a table whose
    entries the alias map prices are malformed."""
    if urllib.parse.urlparse(url).scheme != "https":
        return None, f"refused {url!r}: not an https URL"
    opener = urllib.request.build_opener(_HttpsOnlyRedirect)
    try:
        req = urllib.request.Request(  # noqa: S310 - scheme checked above, redirects https only
            url, headers={"User-Agent": user_agent()})
        with opener.open(req, timeout=PRICES_TIMEOUT_S) as resp:
            body = resp.read(PRICES_MAX_BYTES + 1)
        if len(body) > PRICES_MAX_BYTES:
            raise ValueError(f"larger than {PRICES_MAX_BYTES:,} bytes")
        text = body.decode("utf-8")
        table = json.loads(text)
        invalid = table_problem(table)
        if invalid:
            raise ValueError(f"not a genai-prices table: {invalid}")
        rows = {(p["id"], m["id"]): m for p in table for m in p["models"]}
        aliases, _ = load_aliases(data_path("proxy-usage-price-aliases"))
        for name, target in aliases.items():
            key = _alias_key(target)
            problem = price_entry_problem(rows[key]) if key in rows else None
            if problem:
                raise ValueError(f"the row {name} is priced as, {key[0]}/{key[1]}: {problem}")
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code} from {url}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            UserAgentConfigError) as exc:
        return None, f"{type(exc).__name__}: {exc} ({url})"
    return text, None


def refresh_prices(url: str, force: bool = False) -> str | None:
    """Fetch the table into the local copy when an attempt is due, or `force`.

    An attempt is due when the last one (success or failure, recorded under the
    registry name `proxy-usage-prices-attempt`) is 24 h old, or 1 h old after a
    first failure: so at most one attempt a day plus one retry an hour after a
    failure, and an offline machine does not retry on every 5-minute run. With
    no attempt recorded, the copy's own age decides. A failed attempt keeps the
    last copy. Returns a line to show, or None when nothing was due and the last
    attempt succeeded. Never raises for a network or content failure."""
    target = data_path("proxy-usage-prices")
    attempt_file = data_path("proxy-usage-prices-attempt")
    try:
        mtime = target.stat().st_mtime
    except FileNotFoundError:
        mtime = None
    attempt = _read_attempt(attempt_file)
    now = time.time()
    if not force and not _attempt_due(attempt, mtime, now):
        if attempt is None or attempt["ok"]:
            return None
        when = datetime.fromtimestamp(attempt["at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        problem = f"last attempt {when} failed: {attempt.get('problem')}; not retried yet"
    else:
        text, problem = _fetch_table(url)
        failures = 0 if text is not None else (attempt["failures"] if attempt else 0) + 1
        data_write_file("proxy-usage-prices-attempt")
        atomic_write_text(attempt_file, json.dumps(
            {"at": now, "ok": text is not None, "failures": failures, "problem": problem}))
        if text is not None:
            data_write_file("proxy-usage-prices")
            atomic_write_text(target, text)
            return f"price table fetched: {len(text.encode()):,} bytes from {url} into {target}"
    if mtime is None:
        return f"price table NOT fetched ({problem}) and no earlier copy exists; every call is unpriced"
    when = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"price table NOT refreshed ({problem}); using the copy of {when} at {target}"


def _constraint_holds(constraint: dict, ts: datetime) -> bool:
    utc = ts.astimezone(timezone.utc)
    start = constraint.get("start_date")
    if start is not None and utc.date() < date.fromisoformat(start):
        return False
    if "start_time" in constraint and "end_time" in constraint:
        now = utc.timetz()
        a = dtime.fromisoformat(constraint["start_time"])
        b = dtime.fromisoformat(constraint["end_time"])
        a, b = (t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in (a, b))
        inside = (now >= a or now < b) if b < a else (a <= now < b)
        if not inside:
            return False
    return True


def _active_prices(model: dict, ts: datetime) -> dict:
    prices = model["prices"]
    if isinstance(prices, dict):
        return prices
    chosen: dict = {}
    for entry in prices:
        if _constraint_holds(entry.get("constraint") or {}, ts):
            chosen = entry["prices"]
    return chosen


def _rate(price: object, prompt_tokens: int) -> float | None:
    """USD per million tokens for this request, tiered by its input tokens."""
    if isinstance(price, bool):
        return None
    if isinstance(price, (int, float)):
        return float(price)
    if isinstance(price, dict) and isinstance(price.get("base"), (int, float)):
        tiers = price.get("tiers") or []
        if not isinstance(tiers, list) or not all(
                isinstance(t, dict) and isinstance(t.get("start"), int)
                and isinstance(t.get("price"), (int, float)) for t in tiers):
            return None
        rate = float(price["base"])
        for tier in sorted(tiers, key=lambda t: t["start"]):
            if prompt_tokens > tier["start"]:
                rate = float(tier["price"])
        return rate
    return None


class PriceBook:
    """The table and the alias map, loaded once per run."""

    def __init__(self, note: str | None = None):
        self.note = note
        self.problems: list[str] = []
        self.copy = data_path("proxy-usage-prices")
        self.aliases_file = data_path("proxy-usage-price-aliases")
        self.copy_date: str | None = None
        self.models: dict[str, dict | None] = {}
        table: list = []
        try:
            raw = json.loads(self.copy.read_text(encoding="utf-8"))
            invalid = table_problem(raw)
            if invalid:
                self.problems.append(f"{self.copy}: {invalid}; every call is unpriced")
            else:
                table = raw
                self.copy_date = datetime.fromtimestamp(
                    self.copy.stat().st_mtime, timezone.utc).date().isoformat()
        except FileNotFoundError:
            self.problems.append(f"no price table at {self.copy}; every call is unpriced")
        except (OSError, ValueError) as exc:
            self.problems.append(f"{self.copy} unreadable ({exc}); every call is unpriced")
        rows = {(p["id"], m["id"]): m for p in table for m in p["models"]}
        aliases, problem = load_aliases(self.aliases_file)
        self.aliases = aliases
        if problem:
            self.problems.append(problem)
        for name, target in aliases.items():
            if target is None:
                self.models[str(name)] = None
                continue
            key = _alias_key(target)
            invalid = price_entry_problem(rows[key]) if key in rows else None
            if invalid:
                self.models[str(name)] = None
                self.problems.append(f"alias {name} -> {key[0]}/{key[1]}: {invalid}; unpriced")
            elif key in rows:
                self.models[str(name)] = rows[key]
            else:
                self.models[str(name)] = None
                if table:
                    self.problems.append(f"alias {name} -> {target} is not a row of the table; unpriced")

    def model_for(self, row: dict) -> dict | None:
        for name in (row.get("model"), row.get("served")):
            if isinstance(name, str) and name in self.models:
                return self.models[name]
        return None

    def provider_of(self, row: dict) -> str:
        """The genai-prices provider the alias map names for this row; "unknown" when none."""
        for name in (row.get("model"), row.get("served")):
            if isinstance(name, str) and name in self.aliases:
                target = self.aliases[name]
                provider = target.get("provider") if isinstance(target, dict) else None
                return provider if isinstance(provider, str) and provider else "unknown"
        return "unknown"

    def cost_parts(self, row: dict) -> dict | None:
        """Unrounded micro-dollars of uncached input, cached input and output
        (reasoning included); None when the row is unpriced."""
        model = self.model_for(row)

        def n(key):
            value = row.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        prompt, completion = n("prompt_tokens"), n("completion_tokens")
        if model is None or not (prompt or completion):
            return None
        try:
            prices = _active_prices(model, datetime.fromisoformat(row["ts"]))
        except (ValueError, TypeError, KeyError):
            return None
        cached = min(n("cached_tokens"), prompt)
        reasoning = min(n("reasoning_tokens"), completion)
        input_rate = _rate(prices.get("input_mtok"), prompt)
        output_rate = _rate(prices.get("output_mtok"), prompt)
        if input_rate is None or (completion and output_rate is None):
            return None
        cache_rate = _rate(prices.get("cache_read_mtok"), prompt)
        reasoning_rate = _rate(prices.get("output_reasoning_mtok"), prompt)
        if reasoning and reasoning_rate is not None:
            output = reasoning * reasoning_rate + (completion - reasoning) * (output_rate or 0)
        else:
            output = completion * (output_rate or 0)
        return {"input": (prompt - cached) * input_rate,
                "cached": cached * (input_rate if cache_rate is None else cache_rate),
                "output": output}

    def state(self) -> dict:
        """What the page's prices were built from, for the dashboard stamp."""
        def mtime(path):
            try:
                return path.stat().st_mtime_ns
            except FileNotFoundError:
                return None
        return {"prices_mtime_ns": mtime(self.copy), "aliases_mtime_ns": mtime(self.aliases_file),
                "attempt_mtime_ns": mtime(data_path("proxy-usage-prices-attempt"))}

    def basis_lines(self) -> list[str]:
        """What the prices stand on, and what went wrong loading them."""
        out = [NOT_THE_BILL + f" (pydantic/genai-prices, copy of {self.copy_date or 'none'})"]
        if self.note:
            out.append(self.note)
        out.extend(self.problems)
        return out

    def lines(self, total: dict) -> list[str]:
        """The lines every view prints under its figures."""
        basis = self.basis_lines()
        return [basis[0], f"{total['unpriced']} call(s) unpriced: no price row mapped, or no token "
                          f"counts", *basis[1:]]


def usd(micro: int) -> str:
    dollars = micro / 1e6
    return f"${dollars:,.2f}" if dollars >= 1 else f"${dollars:.4f}"


def cost_cell(acc: dict) -> str:
    """The cost of a bucket, or a dash when none of its calls had a price."""
    return "-" if acc["calls"] and acc["unpriced"] == acc["calls"] else usd(acc["cost_micro_usd"])


# ============================================================
# Terminal and JSON
# ============================================================


def _print_table(header: list[str], body: list[list[str]], footer: list[str]) -> None:
    widths = [max(len(r[i]) for r in [header, footer, *body]) for i in range(len(header))]

    def line(cells):
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    rule = ["-" * len(h) for h in header]
    print(line(header))
    print(line(rule))
    for r in body:
        print(line(r))
    print(line(rule))
    print(line(footer))


def _cells(label: str, acc: dict, names: str) -> list[str]:
    return [label, str(acc["calls"]), str(acc["retries"]), str(acc["errors"]), str(acc["input"]),
            str(acc["output"]), str(acc["reasoning"]), str(acc["total"]),
            cost_cell(acc), names]


def print_daily(agg: dict, by: str | None) -> None:
    header = ["DATE", "CALLS", "RETRIES", "ERRORS", "INPUT", "OUTPUT", "REASONING", "TOTAL",
              "$ API LIST", "MODELS"]
    body: list[list[str]] = []
    for day, acc in agg["days"].items():
        per_model = _ranked(agg["day_model"][day])
        body.append(_cells(day.isoformat(), acc, ", ".join(per_model)))
        if by:
            sub = per_model if by == "model" else _ranked(agg["day_caller"][day])
            body.extend(_cells(f"  {name}", a, "") for name, a in sub.items())
    if not body:
        print(f"{GRAY}No proxy calls recorded in this window.{RESET}")
        return
    _print_table(header, body, _cells("TOTAL", agg["total"], ""))


def as_json(agg: dict, window: dict, book: PriceBook) -> dict:
    return {
        "window": window,
        "prices": {"basis": NOT_THE_BILL, "unit": "cost_micro_usd is integer micro-dollars",
                   "source": "pydantic/genai-prices", "copy": str(book.copy),
                   "copy_date": book.copy_date, "note": book.note, "problems": book.problems},
        "days": [{"date": d.isoformat(), **acc,
                  "models": _ranked(agg["day_model"][d]),
                  "callers": _ranked(agg["day_caller"][d])}
                 for d, acc in agg["days"].items()],
        "models": agg["models"],
        "callers": agg["callers"],
        "total": agg["total"],
    }


# ============================================================
# HTML dashboard
# ============================================================
# The page is one static file: the figures travel inside it as JSON, aggregated
# per day, model, caller and provider (never any text of a call; the ledger
# holds none), and an inline script draws the cards, the chart, the attribution
# and the tables from them, so the Cost/Tokens, chart, grouping, period and
# filter controls work with no server and no network. The look follows
# agentsview's Usage view in its dark theme.

# agentsview's dark categorical series, in its order; "Other" is grey.
PALETTE = ("#60a5fa", "#a78bfa", "#ef4444", "#fb7185", "#2dd4bf", "#fbbf24", "#4ade80",
           "#818cf8", "#34d399", "#fb923c")
OTHER_COLOR = "#8b93a8"
TOP_SERIES = 10
ICON_MAX_BYTES = 256 * 1024

CSS = """
:root{--bg:#0d0d12;--surface:#16161e;--hover:#1f1f2a;--inset:#111116;--border:#333345;--border2:#2a2a3a;
--text:#ececf1;--text2:#b4bcd0;--muted:#8b93a8;--accent:#60a5fa;
--sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
--mono:"JetBrains Mono","SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font:13px/1.4 var(--sans)}
.top{height:44px;display:flex;align-items:center;gap:16px;padding:0 14px;background:var(--surface);border-bottom:1px solid var(--border)}
.brand{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700;letter-spacing:.02em;white-space:nowrap}
.brand img{height:22px;width:auto;display:block}
.tabs{display:flex;gap:2px;background:var(--inset);border:1px solid var(--border2);border-radius:6px;padding:2px}
.tab{padding:3px 12px;border-radius:4px;font-size:13px;font-weight:500;color:var(--muted)}
.tab.on{background:var(--hover);color:var(--text);font-weight:600;box-shadow:0 1px 2px #00000040}
.top .right{margin-left:auto;color:var(--muted);font-size:12px}
.toolbar{display:flex;align-items:center;gap:10px;padding:9px 14px;border-bottom:1px solid var(--border)}
.seg{display:inline-flex;gap:1px;background:var(--inset);border:1px solid var(--border2);border-radius:6px;padding:2px}
.seg button{font:inherit;font-size:12px;font-weight:500;color:var(--muted);background:none;border:0;border-radius:4px;padding:4px 12px;cursor:pointer}
.seg button:hover{color:var(--text2)}
.seg button.on{background:var(--hover);color:var(--text);font-weight:600;box-shadow:0 1px 2px #00000040}
.pill{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text2);font-size:12px}
.pill.period{min-width:170px}
.pill select{appearance:none;-webkit-appearance:none;flex:1;background:transparent;border:0;color:inherit;font:inherit;padding:0 2px;cursor:pointer;outline:none}
.pill select option{background:var(--surface);color:var(--text)}
.ico{width:13px;height:13px;flex:none;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.updated{display:inline-flex;align-items:center;gap:8px;color:var(--text2);font-size:12px;margin-left:4px}
main{padding:14px;display:grid;gap:14px}
.cards{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 14px 12px;min-width:0;min-height:90px}
.card.hl{border:2px solid var(--accent);padding:13px 13px 11px}
.card .v{font-size:21px;font-weight:700;letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .l{color:var(--text2);font-size:12px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card .s{color:var(--muted);font-size:11px;margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.note{margin:0;color:var(--muted);font-size:11.5px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.panel h2{margin:0 0 10px;font-size:13px;font-weight:700}
.ph{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}
.ph h2{margin:0}
.ctl{display:flex;gap:10px}
.legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:11.5px;color:var(--text2);margin-bottom:8px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.chart{position:relative;height:280px}
.chart svg{display:block}
.axis{fill:var(--muted);font:10px var(--mono)}
.tip{position:absolute;pointer-events:none;background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:11.5px;min-width:190px;box-shadow:0 4px 16px #0006;z-index:2}
.tip .d{font-weight:600;margin-bottom:4px}
.tip .r{display:flex;gap:12px;justify-content:space-between;color:var(--text2)}
.tip .r b{font:500 11px var(--mono);color:var(--text)}
.tip i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.attr{display:grid;grid-template-columns:22px minmax(0,1fr) 3fr 110px;gap:7px 10px;align-items:center;font-size:12px}
.attr .rank{color:var(--muted);text-align:right;font:11px var(--mono)}
.attr .name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text2)}
.attr .bar{height:8px;border-radius:4px;background:var(--inset)}
.attr .bar span{display:block;height:100%;border-radius:4px}
.attr .val{text-align:right;font:11px var(--mono)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);padding:6px 8px}
td{border-bottom:1px solid var(--border2);padding:6px 8px}
td.n,th.n{text-align:right;font-family:var(--mono);font-size:11px}
.empty{color:var(--muted);margin:0}
footer{color:var(--muted);font-size:11px;padding:4px 14px 14px;display:flex;justify-content:space-between}
"""

JS = r"""
(function () {
  "use strict";
  var D = JSON.parse(document.getElementById("usage-data").textContent);
  var NS = document.getElementById("ns").namespaceURI;
  // Every object keyed by a ledger-derived name (a model, a caller, a provider, a day) or by a
  // URL-fragment word has no prototype, so a name such as "constructor" or "__proto__" is an
  // ordinary key and never an inherited property.
  function dict() { return Object.create(null); }
  var NF = D.fields.length, IX = dict();
  D.fields.forEach(function (f, i) { IX[f] = i; });
  var OPTS = Object.assign(dict(), {mode: ["tokens", "cost"], chart: ["smooth", "lines", "bars"],
              group: ["model", "caller", "provider"], period: ["7", "30", "90", "all"]});
  if (D.initial.period === "custom") OPTS.period.push("custom");
  var st = {mode: "tokens", chart: "smooth", group: "model", period: D.initial.period, model: "",
            caller: ""};
  var MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function dn(s) { var p = s.split("-"); return Date.UTC(+p[0], +p[1] - 1, +p[2]) / 864e5; }
  function ds(n) { return new Date(n * 864e5).toISOString().slice(0, 10); }
  function md(n) { var d = new Date(n * 864e5); return MON[d.getUTCMonth()] + " " + d.getUTCDate(); }
  D.rows.forEach(function (r) { r.d = dn(r[0]); });

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function svg(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }
  function tok(n, axis) {
    var u = [[1e9, "B"], [1e6, "M"], [1e3, "K"]];
    for (var i = 0; i < u.length; i++) {
      if (n >= u[i][0]) {
        var s = (n / u[i][0]).toFixed(1);
        return (axis ? s : s.replace(/\.0$/, "")) + u[i][1];
      }
    }
    return String(Math.round(n));
  }
  function usd(micro) {
    var d = micro / 1e6;
    if (d >= 100) return "$" + Math.round(d).toLocaleString("en-US");
    if (d >= 1) return "$" + d.toFixed(2);
    if (d > 0) return "$" + d.toFixed(4);
    return "$0.00";
  }
  function axisUsd(micro, step) {
    var places = Math.max(0, Math.min(6, -Math.floor(Math.log10(step / 1e6))));
    return micro >= 1e8 ? usd(micro) : "$" + (micro / 1e6).toFixed(Math.max(2, places));
  }
  function num(n) { return Math.round(n).toLocaleString("en-US"); }
  function cost() { return st.mode === "cost"; }
  function fmt(n) { return cost() ? usd(n) : tok(n); }

  function zero() { var a = []; for (var i = 0; i < NF; i++) a.push(0); return a; }
  function addTo(a, r) { for (var i = 0; i < NF; i++) a[i] += r[4 + i]; }
  function g(a, f) { return a[IX[f]]; }
  function metricOf(a) { return cost() ? g(a, "cost_micro_usd") : g(a, "total"); }
  function rowMetric(r) { return r[4 + (cost() ? IX.cost_micro_usd : IX.total)]; }
  function unpricedAll(a) { return g(a, "calls") > 0 && g(a, "unpriced") === g(a, "calls"); }
  function keyOf(r) {
    if (st.group === "caller") return D.callers[r[2]];
    if (st.group === "provider") return D.providers[r[3]];
    return D.models[r[1]];
  }
  function byDesc(sums) {
    return Object.keys(sums).sort(function (a, b) {
      return sums[b] - sums[a] || (a < b ? -1 : a > b ? 1 : 0);
    });
  }

  function readHash() {
    location.hash.replace(/^#/, "").split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      if (i < 0) return;
      var k = decodeURIComponent(kv.slice(0, i).replace(/\+/g, " "));
      var v = decodeURIComponent(kv.slice(i + 1).replace(/\+/g, " "));
      if (OPTS[k] && OPTS[k].indexOf(v) >= 0) st[k] = v;
      else if (k === "model" && D.models.indexOf(v) >= 0) st.model = v;
      else if (k === "caller" && D.callers.indexOf(v) >= 0) st.caller = v;
    });
  }
  function writeHash() {
    var parts = [];
    Object.keys(st).forEach(function (k) {
      if (st[k] !== "") parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(st[k]));
    });
    location.replace("#" + parts.join("&"));
  }

  function view() {
    // 7/30/90 days end on the day the page was built; "all" spans the data, a row a wrong
    // clock dated in the future included, and never stops before today.
    var today = dn(D.today), end, start;
    if (st.period === "custom") { start = dn(D.initial.from); end = dn(D.initial.to); }
    else if (st.period === "all") { end = Math.max(dn(D.end), today); start = Math.min(dn(D.start), end); }
    else { end = today; start = end - Number(st.period) + 1; }
    var rows = D.rows.filter(function (r) {
      return r.d >= start && r.d <= end &&
        (!st.model || D.models[r[1]] === st.model) &&
        (!st.caller || D.callers[r[2]] === st.caller);
    });
    return {start: start, end: end, n: end - start + 1, rows: rows};
  }

  // ---- cards -------------------------------------------------------------
  function cards(v, t, byDay) {
    var box = document.getElementById("cards");
    var none = cost() && unpricedAll(t);
    function c(x) { return none ? "-" : usd(x); }
    var peak = null;
    Object.keys(byDay).forEach(function (d) {
      var m = metricOf(byDay[d]);
      if (peak === null || m > peak.m) peak = {d: Number(d), m: m};
    });
    var models = dict(), callers = dict();
    v.rows.forEach(function (r) { models[r[1]] = 1; callers[r[2]] = 1; });
    var input = g(t, "input"), cached = g(t, "cached"), reasoning = g(t, "reasoning");
    var list = [
      cost() ? [c(g(t, "cost_micro_usd")), "Total Cost", g(t, "unpriced") + " call(s) unpriced", true]
             : [tok(g(t, "total")), "Total Tokens", num(g(t, "calls")) + " calls", true],
      [cost() ? c(g(t, "cost_input_micro_usd") + g(t, "cost_cached_micro_usd")) : tok(input), "Total Input", ""],
      [cost() ? c(g(t, "cost_input_micro_usd")) : tok(input - cached), "Uncached Input",
       "+" + (cost() ? c(g(t, "cost_cached_micro_usd")) : tok(cached)) + " cached"],
      cost() ? [c(g(t, "cost_output_micro_usd")), "Output Cost", ""]
             : [tok(g(t, "output")), "Output Tokens", reasoning ? tok(reasoning) + " reasoning" : ""],
      [cost() ? c(metricOf(t) / v.n) : tok(metricOf(t) / v.n), "Daily Burn", "avg/day"],
      [peak ? (cost() ? c(peak.m) : tok(peak.m)) : "0", "Peak Day", peak ? ds(peak.d) : ""],
      [(input ? (100 * cached / input).toFixed(1) : "0.0") + "%", "Cache Hit",
       D.cache_since ? "data since " + D.cache_since : "no cache data yet"],
      [String(Object.keys(callers).length), "Callers", ""],
      [String(Object.keys(models).length), "Models", ""],
      [String(Object.keys(byDay).length), "Active Days", "of " + v.n]
    ];
    box.replaceChildren();
    list.forEach(function (it) {
      var card = el("div", it[3] ? "card hl" : "card");
      var vv = el("div", "v", it[0]);
      vv.title = it[0];
      card.appendChild(vv);
      card.appendChild(el("div", "l", it[1]));
      if (it[2]) card.appendChild(el("div", "s", it[2]));
      box.appendChild(card);
    });
  }

  function notes(t) {
    var box = document.getElementById("notes");
    var lines = D.notes.concat([
      g(t, "unpriced") + " call(s) in this view unpriced: no price row mapped, or no token counts",
      D.cache_since ? "Cached-token counts are recorded from " + D.cache_since + "; rows before that count 0 cached."
                    : "No ledger row carries a cached-token count yet; every row counts 0 cached."]);
    box.replaceChildren();
    lines.forEach(function (s, i) {
      if (i) box.appendChild(el("br"));
      box.appendChild(document.createTextNode(s));
    });
  }

  // ---- chart -------------------------------------------------------------
  function tangents(xs, ys) {
    var n = xs.length, t = new Array(n), s = [], i;
    for (i = 0; i < n - 1; i++) s.push((ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]));
    if (n === 2) return [s[0], s[0]];
    for (i = 1; i < n - 1; i++) {
      var h0 = xs[i] - xs[i - 1], h1 = xs[i + 1] - xs[i], s0 = s[i - 1], s1 = s[i];
      var p = (s0 * h1 + s1 * h0) / (h0 + h1);
      t[i] = (Math.sign(s0) + Math.sign(s1)) * Math.min(Math.abs(s0), Math.abs(s1), 0.5 * Math.abs(p)) || 0;
    }
    t[0] = (3 * s[0] - t[1]) / 2;
    t[n - 1] = (3 * s[n - 2] - t[n - 2]) / 2;
    return t;
  }
  function f1(x) { return x.toFixed(1); }
  function piece(xs, ys, t, a, b) {
    if (!t) return "L" + f1(xs[b]) + "," + f1(ys[b]);
    var dx = (xs[b] - xs[a]) / 3;
    return "C" + f1(xs[a] + dx) + "," + f1(ys[a] + dx * t[a]) + " " +
      f1(xs[b] - dx) + "," + f1(ys[b] - dx * t[b]) + " " + f1(xs[b]) + "," + f1(ys[b]);
  }
  function areaPath(xs, top, bot, smooth) {
    var n = xs.length, i, tt = smooth ? tangents(xs, top) : null, tb = smooth ? tangents(xs, bot) : null;
    var d = "M" + f1(xs[0]) + "," + f1(top[0]);
    for (i = 0; i < n - 1; i++) d += piece(xs, top, tt, i, i + 1);
    d += "L" + f1(xs[n - 1]) + "," + f1(bot[n - 1]);
    for (i = n - 1; i > 0; i--) d += piece(xs, bot, tb, i, i - 1);
    return d + "Z";
  }
  function ticks(max) {
    if (!(max > 0)) return [0];
    var raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), e = raw / p;
    var step = (e > 5 ? 10 : e > 2 ? 5 : e > 1 ? 2 : 1) * p, out = [];
    for (var i = 0; i <= Math.ceil(max / step - 1e-9); i++) out.push(i * step);
    return out;
  }
  function series(v) {
    var sums = dict();
    v.rows.forEach(function (r) { var k = keyOf(r); sums[k] = (sums[k] || 0) + rowMetric(r); });
    var keys = byDesc(sums), list = [], at = dict();
    keys.slice(0, D.top_series).forEach(function (k, i) {
      at[k] = list.length;
      list.push({name: k, color: D.palette[i % D.palette.length]});
    });
    if (keys.length > D.top_series) list.push({name: "Other", color: D.other_color});
    return {list: list, index: function (r) {
      var k = keyOf(r);
      return k in at ? at[k] : list.length - 1;
    }};
  }

  function chart(v) {
    var box = document.getElementById("chart-box"), tip = document.getElementById("tip");
    document.getElementById("chart-title").textContent = (cost() ? "Cost" : "Tokens") + " Over Time";
    var S = series(v), n = v.n, i, j;
    var vals = S.list.map(function () { var a = []; for (var k = 0; k < n; k++) a.push(0); return a; });
    v.rows.forEach(function (r) { vals[S.index(r)][r.d - v.start] += rowMetric(r); });

    var lg = document.getElementById("legend");
    lg.replaceChildren();
    S.list.forEach(function (s) {
      var it = el("span"), sw = el("i");
      sw.style.background = s.color;
      it.appendChild(sw);
      it.appendChild(document.createTextNode(s.name));
      lg.appendChild(it);
    });

    var cum = [], prev = [];
    for (i = 0; i < n; i++) prev.push(0);
    vals.forEach(function (a) {
      var c = a.map(function (x, k) { return x + prev[k]; });
      cum.push(c);
      prev = c;
    });
    var max = 0;
    prev.forEach(function (x) { if (x > max) max = x; });
    var W = Math.max(box.clientWidth, 320), H = box.clientHeight || 280;
    var L = 58, R = 12, T = 10, B = 24, pw = W - L - R, ph = H - T - B;
    var tk = ticks(max), ymax = tk[tk.length - 1] || 1;
    var bars = st.chart === "bars", slot = pw / n;
    function y(x) { return T + ph * (1 - x / ymax); }
    function x(k) { return (bars || n === 1) ? L + slot * (k + 0.5) : L + pw * k / (n - 1); }

    var root = svg("svg", {width: W, height: H, role: "img",
                           "aria-label": (cost() ? "cost" : "tokens") + " per day by " + st.group});
    var defs = svg("defs"), clip = svg("clipPath", {id: "plot-clip"});
    clip.appendChild(svg("rect", {x: L, y: T, width: pw, height: ph}));
    defs.appendChild(clip);
    root.appendChild(defs);
    tk.forEach(function (t) {
      root.appendChild(svg("line", {x1: L, x2: W - R, y1: f1(y(t)), y2: f1(y(t)),
                                    stroke: "#333345", "stroke-dasharray": "1 3"}));
      var lab = svg("text", {"class": "axis", x: L - 8, y: f1(y(t) + 3), "text-anchor": "end"});
      lab.textContent = t === 0 ? (cost() ? "$0.00" : "0") : (cost() ? axisUsd(t, tk[1]) : tok(t, true));
      root.appendChild(lab);
    });
    var plot = svg("g", {"clip-path": "url(#plot-clip)"});
    if (bars) {
      var bw = Math.max(1, Math.min(slot * 0.72, 36));
      for (i = 0; i < n; i++) {
        for (j = 0; j < S.list.length; j++) {
          var lo = j ? cum[j - 1][i] : 0, hi = cum[j][i];
          if (hi <= lo) continue;
          plot.appendChild(svg("rect", {x: f1(x(i) - bw / 2), y: f1(y(hi)), width: f1(bw),
                                        height: f1(y(lo) - y(hi)), fill: S.list[j].color}));
        }
      }
    } else {
      var xs = [];
      for (i = 0; i < n; i++) xs.push(x(i));
      if (n === 1) xs = [L, L + pw];
      for (j = 0; j < S.list.length; j++) {
        var top = cum[j].map(y), bot = (j ? cum[j - 1] : cum[j].map(function () { return 0; })).map(y);
        if (n === 1) { top = [top[0], top[0]]; bot = [bot[0], bot[0]]; }
        plot.appendChild(svg("path", {d: areaPath(xs, top, bot, st.chart === "smooth"),
                                      fill: S.list[j].color, "fill-opacity": "0.9"}));
      }
    }
    root.appendChild(plot);
    var every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(pw / 110)))), at = [];
    for (i = 0; i < n; i += every) at.push(i);
    if (at[at.length - 1] !== n - 1) {
      if (n - 1 - at[at.length - 1] < every * 0.6 && at.length > 1) at.pop();
      at.push(n - 1);
    }
    at.forEach(function (k) {
      var anchor = x(k) > W - R - 24 ? "end" : x(k) < L + 24 && n > 1 ? "start" : "middle";
      var xl = svg("text", {"class": "axis", x: f1(x(k)), y: H - 6, "text-anchor": anchor});
      xl.textContent = md(v.start + k);
      root.appendChild(xl);
    });
    var guide = svg("line", {y1: T, y2: T + ph, stroke: "#8b93a8", "stroke-dasharray": "2 3",
                             visibility: "hidden"});
    root.appendChild(guide);
    var hit = svg("rect", {x: L, y: T, width: pw, height: ph, fill: "transparent"});
    root.appendChild(hit);
    hit.addEventListener("mousemove", function (ev) {
      var px = ev.clientX - root.getBoundingClientRect().left;
      var k = (bars || n === 1) ? Math.floor((px - L) / slot) : Math.round((px - L) / pw * (n - 1));
      k = Math.max(0, Math.min(n - 1, k));
      guide.setAttribute("x1", f1(x(k)));
      guide.setAttribute("x2", f1(x(k)));
      guide.setAttribute("visibility", "visible");
      tip.replaceChildren();
      tip.appendChild(el("div", "d", ds(v.start + k)));
      var tr = el("div", "r");
      tr.appendChild(el("span", null, "Total"));
      tr.appendChild(el("b", null, fmt(cum.length ? cum[cum.length - 1][k] : 0)));
      tip.appendChild(tr);
      for (var s = S.list.length - 1; s >= 0; s--) {
        if (!vals[s][k]) continue;
        var row = el("div", "r"), name = el("span"), sw = el("i");
        sw.style.background = S.list[s].color;
        name.appendChild(sw);
        name.appendChild(document.createTextNode(S.list[s].name));
        row.appendChild(name);
        row.appendChild(el("b", null, fmt(vals[s][k])));
        tip.appendChild(row);
      }
      tip.hidden = false;
      var left = x(k) + 14;
      if (left + tip.offsetWidth > W) left = x(k) - 14 - tip.offsetWidth;
      tip.style.left = Math.max(0, left) + "px";
      tip.style.top = T + "px";
    });
    hit.addEventListener("mouseleave", function () {
      tip.hidden = true;
      guide.setAttribute("visibility", "hidden");
    });
    box.replaceChildren(root, tip);
    tip.hidden = true;
  }

  // ---- attribution and tables ---------------------------------------------
  function attribution(v) {
    [["model", 1, D.models, "Model"], ["caller", 2, D.callers, "Caller"]].forEach(function (spec) {
      var sums = dict(), acc = dict(), grand = 0;
      v.rows.forEach(function (r) {
        var k = spec[2][r[spec[1]]], m = rowMetric(r);
        sums[k] = (sums[k] || 0) + m;
        if (!acc[k]) acc[k] = zero();
        addTo(acc[k], r);
        grand += m;
      });
      document.getElementById("attr-" + spec[0] + "-title").textContent =
        (cost() ? "Cost" : "Token") + " Attribution · " + spec[3];
      var box = document.getElementById("attr-" + spec[0]);
      box.replaceChildren();
      var keys = byDesc(sums).slice(0, 12);
      if (!keys.length) { box.appendChild(el("p", "empty", "No calls in this view.")); return; }
      var top = sums[keys[0]] || 1, grid = el("div", "attr");
      keys.forEach(function (k, i) {
        grid.appendChild(el("div", "rank", String(i + 1)));
        var name = el("div", "name", k);
        name.title = k;
        grid.appendChild(name);
        var bar = el("div", "bar"), fill = el("span");
        fill.style.width = (100 * sums[k] / top).toFixed(1) + "%";
        fill.style.background = D.palette[i % D.palette.length];
        bar.appendChild(fill);
        grid.appendChild(bar);
        grid.appendChild(el("div", "val", (cost() && unpricedAll(acc[k]) ? "-" : fmt(sums[k])) + " · " +
          (grand ? Math.round(100 * sums[k] / grand) : 0) + "%"));
      });
      box.appendChild(grid);
    });
  }

  function tables(v) {
    var cols = ["Calls", "Retries", "Errors", "Input", "Cached", "Output", "Reasoning", "Total",
                "$ API list", "Unpriced", "Avg s"];
    [["models", 1, D.models], ["callers", 2, D.callers]].forEach(function (spec) {
      var acc = dict();
      v.rows.forEach(function (r) {
        var k = spec[2][r[spec[1]]];
        if (!acc[k]) acc[k] = zero();
        addTo(acc[k], r);
      });
      var sums = dict();
      Object.keys(acc).forEach(function (k) { sums[k] = metricOf(acc[k]); });
      var table = document.getElementById("t-" + spec[0]);
      table.replaceChildren();
      var head = el("tr");
      head.appendChild(el("th", null, "Name"));
      cols.forEach(function (c) { head.appendChild(el("th", "n", c)); });
      var thead = el("thead");
      thead.appendChild(head);
      var tbody = el("tbody");
      byDesc(sums).forEach(function (k) {
        var a = acc[k], tr = el("tr");
        tr.appendChild(el("td", null, k));
        [num(g(a, "calls")), num(g(a, "retries")), num(g(a, "errors")), num(g(a, "input")),
         num(g(a, "cached")), num(g(a, "output")), num(g(a, "reasoning")), num(g(a, "total")),
         unpricedAll(a) ? "-" : usd(g(a, "cost_micro_usd")), num(g(a, "unpriced")),
         (g(a, "calls") ? g(a, "duration_ms") / g(a, "calls") / 1000 : 0).toFixed(1)
        ].forEach(function (c) { tr.appendChild(el("td", "n", c)); });
        tbody.appendChild(tr);
      });
      table.appendChild(thead);
      table.appendChild(tbody);
    });
  }

  function footer(t, byDay) {
    var s = num(g(t, "calls")) + " calls · " + Object.keys(byDay).length + " active day(s)";
    if (g(t, "no_usage")) s += " · " + num(g(t, "no_usage")) +
      " request(s) carried no token counts (errors before a response)";
    document.getElementById("foot").textContent = s;
  }

  function updated() {
    var mins = Math.round((Date.now() - Date.parse(D.generated)) / 60000), rel;
    if (!(mins >= 1)) rel = "just now";
    else if (mins < 60) rel = mins + " min ago";
    else if (mins < 2880) rel = Math.round(mins / 60) + " h ago";
    else rel = Math.round(mins / 1440) + " days ago";
    document.getElementById("updated").textContent = "Updated " + rel + " · " + D.generated_label;
  }

  // ---- controls -----------------------------------------------------------
  function sync() {
    Array.prototype.forEach.call(document.querySelectorAll(".seg[data-key]"), function (seg) {
      Array.prototype.forEach.call(seg.querySelectorAll("button"), function (b) {
        b.classList.toggle("on", b.getAttribute("data-v") === st[seg.getAttribute("data-key")]);
      });
    });
    document.getElementById("period").value = st.period;
    document.getElementById("f-model").value = st.model;
    document.getElementById("f-caller").value = st.caller;
  }

  function render() {
    sync();
    var v = view(), t = zero(), byDay = dict();
    v.rows.forEach(function (r) {
      addTo(t, r);
      if (!byDay[r.d]) byDay[r.d] = zero();
      addTo(byDay[r.d], r);
    });
    cards(v, t, byDay);
    notes(t);
    chart(v);
    attribution(v);
    tables(v);
    footer(t, byDay);
  }

  function fill(id, label, names) {
    var sel = document.getElementById(id);
    var all = el("option", null, label + ": All");
    all.value = "";
    sel.appendChild(all);
    names.forEach(function (n) {
      var o = el("option", null, label + ": " + n);
      o.value = n;
      sel.appendChild(o);
    });
  }

  function init() {
    if (D.initial.period === "custom") {
      var custom = el("option", null, D.initial.from + " → " + D.initial.to);
      custom.value = "custom";
      document.getElementById("period").appendChild(custom);
    }
    fill("f-model", "Model", D.models);
    fill("f-caller", "Caller", D.callers);
    readHash();
    Array.prototype.forEach.call(document.querySelectorAll(".seg[data-key]"), function (seg) {
      seg.addEventListener("click", function (ev) {
        var b = ev.target.closest("button");
        if (!b) return;
        st[seg.getAttribute("data-key")] = b.getAttribute("data-v");
        writeHash();
        render();
      });
    });
    [["period", "period"], ["f-model", "model"], ["f-caller", "caller"]].forEach(function (p) {
      document.getElementById(p[0]).addEventListener("change", function (ev) {
        st[p[1]] = ev.target.value;
        writeHash();
        render();
      });
    });
    var pending = 0;
    window.addEventListener("resize", function () {
      cancelAnimationFrame(pending);
      pending = requestAnimationFrame(function () { chart(view()); });
    });
    updated();
    setInterval(updated, 30000);
    render();
  }

  init();
})();
"""

ICONS = {
    "calendar": '<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/>',
    "filter": '<path d="M3 4h18l-7 8v6l-4 2v-8z"/>',
    "chevron": '<path d="M6 9l6 6 6-6"/>',
    "refresh": '<path d="M21 12a9 9 0 0 1-15.5 6.3L3 16M3 12a9 9 0 0 1 15.5-6.3L21 8M21 3v5h-5M3 21v-5h5"/>',
}


def _icon(name: str, ident: str = "") -> str:
    id_attr = f' id="{ident}"' if ident else ""
    return f'<svg{id_attr} class="ico" viewBox="0 0 24 24" aria-hidden="true">{ICONS[name]}</svg>'


def _seg(key: str, opts: tuple) -> str:
    """A segmented control; the script marks the chosen button."""
    buttons = "".join(f'<button type="button" data-v="{v}">{_esc(label)}</button>' for v, label in opts)
    return f'<div class="seg" data-key="{key}">{buttons}</div>'


def _esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def _script_json(obj: object) -> str:
    """JSON that cannot end its <script> block: <, > and & written as \\u escapes."""
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e"))


def page_period(since: date | None, until: date, today: date, every_day: bool,
                first: date) -> dict:
    """The period the page opens on, from the command's window: 7, 30 or 90 days when the
    window ends today and is within a day of one of them, "all" for --all, otherwise a custom
    range. `dashboard` passes no window and opens on 30 days."""
    if until == today:
        if every_day:
            return {"period": "all"}
        if since is None:
            return {"period": str(DEFAULT_WINDOW_DAYS)}
        days = (today - since).days + 1
        for n in (7, 30, 90):
            if abs(days - n) <= 1:
                return {"period": str(n)}
    return {"period": "custom", "from": (since or first).isoformat(),
            "to": until.isoformat()}


def page_data(agg: dict, since: date, until: date, tz_name: str, generated: datetime,
              book: PriceBook, initial: dict) -> dict:
    """What the page's script draws from: one row per day x model x caller x provider."""
    keys = sorted(agg["cells"])
    names = [sorted({k[i] for k in keys}) for i in (1, 2, 3)]
    index = [{n: i for i, n in enumerate(ns)} for ns in names]
    rows = [[k[0].isoformat(), index[0][k[1]], index[1][k[2]], index[2][k[3]],
             *(agg["cells"][k][f] for f in METRICS)] for k in keys]
    return {"fields": list(METRICS), "rows": rows,
            "models": names[0], "callers": names[1], "providers": names[2],
            "start": since.isoformat(), "end": until.isoformat(), "timezone": tz_name,
            "today": generated.date().isoformat(),
            "generated": generated.isoformat(timespec="seconds"),
            "generated_label": generated.strftime("%Y-%m-%d %H:%M %Z"),
            "cache_since": agg["cache_since"].isoformat() if agg["cache_since"] else None,
            "notes": book.basis_lines(),
            "initial": initial,
            "palette": list(PALETTE), "other_color": OTHER_COLOR, "top_series": TOP_SERIES}


def render_html(data: dict, stamp: dict, icons: dict[str, str]) -> str:
    logo = icons.get("heading-os-logo")
    favicon = icons.get("heading-os-favicon")
    periods = "".join(f'<option value="{v}">{label}</option>' for v, label in
                      (("7", "Last 7 days"), ("30", "Last 30 days"), ("90", "Last 90 days"),
                       ("all", "All time")))
    return f"""<!doctype html>
<!-- proxy-usage-stamp: {json.dumps(stamp, sort_keys=True)} -->
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_SECONDS}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proxy Usage · HEADING OS</title>
{f'<link rel="icon" type="image/svg+xml" href="{favicon}">' if favicon else ""}
<style>{CSS}</style></head>
<body>
<header class="top">
<span class="brand">{f'<img src="{logo}" alt="" width="13" height="22">' if logo else ""}<span>HEADING OS</span></span>
<nav class="tabs"><span class="tab on">Proxy Usage</span></nav>
<span class="right">{_esc(data["timezone"])}</span>
</header>
<div class="toolbar">
{_seg("mode", (("cost", "Cost"), ("tokens", "Tokens")))}
<label class="pill period">{_icon("calendar", "ns")}<select id="period" aria-label="Period">{periods}</select>{_icon("chevron")}</label>
<label class="pill">{_icon("filter")}<select id="f-model" aria-label="Model filter"></select>{_icon("chevron")}</label>
<label class="pill">{_icon("filter")}<select id="f-caller" aria-label="Caller filter"></select>{_icon("chevron")}</label>
<span class="updated">{_icon("refresh")}<span id="updated"></span></span>
</div>
<main>
<section class="cards" id="cards"></section>
<p class="note" id="notes"></p>
<section class="panel">
<div class="ph"><h2 id="chart-title">Tokens Over Time</h2><div class="ctl">
{_seg("chart", (("smooth", "Smooth"), ("lines", "Lines"), ("bars", "Bars")))}
{_seg("group", (("model", "Model"), ("caller", "Caller"), ("provider", "Provider")))}
</div></div>
<div class="legend" id="legend"></div>
<div class="chart" id="chart-box"><div class="tip" id="tip" hidden></div></div>
</section>
<section class="grid2">
<div class="panel"><h2 id="attr-model-title">Token Attribution · Model</h2><div id="attr-model"></div></div>
<div class="panel"><h2 id="attr-caller-title">Token Attribution · Caller</h2><div id="attr-caller"></div></div>
</section>
<div class="panel"><h2>Models</h2><table id="t-models"></table></div>
<div class="panel"><h2>Callers</h2><table id="t-callers"></table></div>
<noscript><p class="note">This page draws its figures with JavaScript, which is off.
`scripts/proxy-usage.py daily` prints the same figures in a terminal.</p></noscript>
</main>
<footer><span id="foot"></span><span>{_esc(NOT_THE_BILL)}; the proxy fronts flat subscriptions</span></footer>
<script type="application/json" id="usage-data">{_script_json(data)}</script>
<script>{JS}</script>
</body></html>
"""


def ledger_state() -> dict:
    """The ledger's size and mtime, taken BEFORE it is read, so a row appended
    while the page is being built moves the next stamp and is never skipped.
    A missing ledger is a state too: an empty dashboard is built once and then
    left alone."""
    try:
        st = ledger_path().stat()
        return {"ledger_size": st.st_size, "ledger_mtime_ns": st.st_mtime_ns}
    except FileNotFoundError:
        return {"ledger_size": None, "ledger_mtime_ns": None}


def ledger_stamp(state: dict, window: dict, today: date) -> dict:
    """What the page was built from."""
    return {"format": DASHBOARD_FORMAT, **state, "day": today.isoformat(), **window}


def page_stamp(target: Path) -> dict | None:
    """The stamp embedded in the current page; None when there is no readable one."""
    try:
        with target.open("rb") as fh:
            head = fh.read(512).decode("utf-8", "replace")
    except FileNotFoundError:
        return None
    m = _STAMP_RE.search(head)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def write_dashboard(agg: dict, since: date, until: date, tz_name: str, tz: ZoneInfo,
                    stamp: dict, book: PriceBook, initial: dict) -> Path:
    """Replace the one dashboard file atomically (same-directory temp, then rename).

    The HEADING OS mark and favicon are read from the data repository here and
    inlined as data: URIs; one that is missing or not what it should be is left
    out with a note, and the header shows the text alone."""
    icons: dict[str, str] = {}
    for name, where, mime, magic in (
            ("heading-os-logo", lambda: data_path("heading-os-logo"), "image/png",
             (b"\x89PNG\r\n\x1a\n",)),
            ("heading-os-favicon", lambda: data_path("heading-os-favicon"), "image/svg+xml",
             (b"<svg", b"<?xml"))):
        try:
            raw = where().read_bytes()
        except (DataPathError, OSError) as exc:
            print(f"{YELLOW}{name} not inlined ({type(exc).__name__}); the dashboard header "
                  f"shows text only{RESET}", file=sys.stderr)
            continue
        if len(raw) > ICON_MAX_BYTES or not raw.lstrip().startswith(magic):
            print(f"{YELLOW}{name} not inlined: not a {mime} file under {ICON_MAX_BYTES:,} bytes; "
                  f"the dashboard header shows text only{RESET}", file=sys.stderr)
            continue
        icons[name] = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    target = data_write_dir("proxy-usage-reports") / DASHBOARD_FILE
    data = page_data(agg, since, until, tz_name, datetime.now(tz), book, initial)
    atomic_write_text(target, render_html(data, stamp, icons))
    return target


def windows_path(path: Path) -> str | None:
    if not shutil.which("wslpath"):
        return None
    done = subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True,
                          check=False)
    return done.stdout.strip() or None


# ============================================================
# CLI
# ============================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily", help="tokens and calls per day")
    daily.add_argument("--since", help="start of window: a duration like 28d, or YYYY-MM-DD")
    daily.add_argument("--until", help="end of window, inclusive: a duration or YYYY-MM-DD")
    daily.add_argument("--all", action="store_true", help="all history (overrides the 30-day default)")
    daily.add_argument("--breakdown", action="store_true", help="one sub-row per model")
    daily.add_argument("--by", choices=("model", "caller"), help="sub-rows per model or per caller")
    daily.add_argument("--timezone", help="IANA zone for day buckets (default: HEADING_OS_TZ)")
    out = daily.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="emit JSON")
    out.add_argument("--html", action="store_true", help="write the dashboard with this window")
    dash = sub.add_parser("dashboard", help="rebuild the dashboard (all history, shown at 30 days "
                                            "by default) if the ledger changed")
    dash.add_argument("--force", action="store_true", help="rebuild even when nothing changed")
    dash.add_argument("--timezone", help="IANA zone for day buckets (default: HEADING_OS_TZ)")
    for p in (daily, dash):
        p.add_argument("--refresh-prices", action="store_true",
                       help="fetch the price table now, not only when the copy is a day old")
        p.add_argument("--prices-url", default=os.environ.get("PROXY_USAGE_PRICES_URL", PRICES_URL),
                       help="where the price table is fetched from (an https URL)")
    args = parser.parse_args()

    tz_name = args.timezone or get_default_tz_name()
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"unknown timezone: {tz_name!r}") from None
    today = datetime.now(tz).date()
    is_dash = args.command == "dashboard"
    until = parse_bound(args.until, tz) if not is_dash and args.until else today
    if is_dash or args.all:
        since = None
    else:
        since = (parse_bound(args.since, tz) if not is_dash and args.since
                 else today - timedelta(days=DEFAULT_WINDOW_DAYS - 1))

    note = refresh_prices(args.prices_url, force=args.refresh_prices)
    if note and is_dash:
        print(f"{YELLOW if 'NOT' in note else GRAY}{note}{RESET}", file=sys.stderr)
    book = PriceBook(note)
    state = {**ledger_state(), **book.state()}
    stamp_window = {"since": since.isoformat() if since else "all", "until": until.isoformat(),
                    "timezone": tz_name}
    if is_dash and not args.force:
        target = data_write_dir("proxy-usage-reports") / DASHBOARD_FILE
        if page_stamp(target) == ledger_stamp(state, stamp_window, today):
            return 0

    rows, skipped = read_rows()
    # The page always carries the whole history; the window only picks the period it opens on.
    page = is_dash or args.html
    agg = aggregate(rows, tz, None if page else since, None if page else until, book)
    left_out = skipped + agg["unparsed"]
    if left_out:
        print(f"{YELLOW}{left_out} unreadable ledger line(s) in {ledger_path()} were left out "
              f"of every figure below.{RESET}", file=sys.stderr)
    if page:
        first = min(agg["days"], default=today)
        initial = page_period(since, until, today, not is_dash and args.all, first)
        target = write_dashboard(agg, first, max([today, *agg["days"]]),
                                 tz_name, tz, ledger_stamp(state, stamp_window, today), book,
                                 initial)
        print(f"{BOLD}Dashboard:{RESET} {target}")
        win = windows_path(target)
        if win:
            print(f"{BOLD}Windows:{RESET}   {win}")
        return 0
    if since is None:
        since = min(agg["days"], default=until)
    window = {"since": since.isoformat(), "until": until.isoformat(), "timezone": tz_name}

    if not is_dash and args.json:
        print(json.dumps(as_json(agg, window, book), indent=2))
        return 0
    print(f"{GRAY}{window['since']} to {window['until']} ({tz_name}) · ledger {ledger_path()}{RESET}")
    print_daily(agg, args.by or ("model" if args.breakdown else None))
    for line in book.lines(agg["total"]):
        print(f"{GRAY}{line}{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
