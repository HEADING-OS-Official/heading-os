#!/usr/bin/env python3
"""Blind model comparison for the LLM of the Hindsight memory server, as one tool.

    hindsight-model-bench.py replay --name SLUG --model M1 --model M2 [--extra M='{"k": v}'] [--strip-schema M]
                                    [--samples FILE] [--host H] [--python P] [--key-file F] [--dry-run]
    hindsight-model-bench.py fetch   --run RUN            # resume a replay whose local side died
    hindsight-model-bench.py packet  --run RUN [--models A B] [--seed N] [--samples FILE]
    hindsight-model-bench.py judge   --run RUN --judge kimi|claude
    hindsight-model-bench.py unblind --run RUN [--out DIR]
    hindsight-model-bench.py report  --run RUN [--samples FILE] [--results FILE] [--out DIR]
    hindsight-model-bench.py serve   CONFIG               # runs ON the memory server; replay starts it

What it replaces: the hand-built comparisons of 2026-10-14, -16 and -17, each a copy of the
same runner with the sample set pasted into it, a packet builder and a judge script. The
method is theirs, unchanged: real requests the server already made (a sample set) are
replayed unchanged to each model through the server's own proxy, with the strict
json_schema the server sends; the outputs are judged blind, A/B shuffled per case with a
fixed seed, by Kimi K3 and by a Claude subagent; the key is applied only afterwards.

RUN is a directory. A bare name is a run under the registry name `hindsight-model-bench`
(`<YYYY-MM-DD>-<slug>`, created by `replay`); anything else is taken as a path, so an older
hand-built folder can be unblinded and reported read-only with `--out` elsewhere. Files in a
run: `run.json` (what replay sent and where), `results.jsonl` (one line per model per
sample: sample, kind, model, secs, usage, output, valid, n, err), `judge-packet.md`,
`judge-key.json`, `judge-instructions.md`, `extraction-system-prompt.md`,
`verdicts-<judge>.jsonl`, `unblind.md`, `report.md`.

Samples: a JSON file `{"samples": [{"id", "kind": "retain"|"consolidation", "ctx",
"temperature", "messages": [...]}], "retain_schema": {...}}`. The default is `samples.json`
at the registry name `hindsight-model-bench`. The engine ships no sample. A set is captured
from the server's LLM trace listing for the bank (each trace item carries the `input`
messages, the `response_schema` and the operation); the first set was cut from that listing
by hand, retain extractions and consolidation batches, the retain schema taken from a retain
trace. Capture is not built here.

replay runs NOTHING on this machine but ssh. It copies this file, the samples and a config
to `~/hindsight-model-bench/<run>/` on the server, starts `serve` there detached
(`setsid nohup`), then polls with short ssh calls and copies `results.jsonl` back. A dropped
link costs nothing (on 2026-10-17 one long ssh session died with exit 255 while the remote
run finished); `fetch` picks the run up again from `run.json`. `serve` imports
`hindsight_api` under the server's own interpreter to build the consolidation schema exactly
as the server does, reads the proxy key from a file on the server and never prints it.
Requests alternate model order per sample. `--extra M=JSON` merges request fields for one
model (`chat_template_kwargs` `enable_thinking: false` turns reasoning off for a llama.cpp
model); `--strip-schema M` passes the retain schema through Hindsight's own
`_strip_ref_siblings` and `_strip_numeric_bounds` for a server that rejects them.

Settings, flag first, then the gitignored `.env`: `HINDSIGHT_BENCH_HOST` (ssh alias of the
memory server), `HINDSIGHT_BENCH_PYTHON` (the interpreter that has `hindsight_api`),
`HINDSIGHT_BENCH_KEY_FILE` (the proxy key file ON the server). The proxy URL defaults to the
server's loopback CLIProxyAPI.

It changes no model, configuration, daemon or timer on the server; `serve` only reads the
proxy. The Kimi judge goes through `scripts/utils/proxy_transport.call_model`; the Claude
judge is a subagent, and `judge --judge claude` prints its brief rather than calling it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROXY_URL = "http://127.0.0.1:8317/v1/chat/completions"
REMOTE_BASE = "hindsight-model-bench"
KINDS = ("retain", "consolidation")
CASE_HEADER = re.compile(r"^=== (case\d+) \((\w+)\) ===$", re.M)
CYRILLIC = re.compile("[\u0400-\u04ff]")
LATIN = re.compile(r"[A-Za-z]")
CYRILLIC_INPUT = 0.25  # share of letters from which a document counts as Cyrillic-script

JUDGE_INSTRUCTIONS = """\
You are a blind judge comparing two models that serve as the LLM of a memory server (Hindsight). The packet holds {n} cases. Each case gives the INPUT the server sent and two answers, OUTPUT A and OUTPUT B, from two different models; which model is which is hidden and shuffled per case. Cases marked (retain) are fact extraction from one document; their shared system prompt (the extraction rules) is in extraction-system-prompt.md. Cases marked (consolidation) carry their own system prompt inside the INPUT.

Judge quality only, not length. For each case decide which answer would make the better long-term memory, weighing in this order:
1. Faithfulness: every fact is supported by the input. An invented or distorted fact (wrong name, wrong number, wrong date, wrong attribution, a proposal stated as a decision) is the worst error.
2. Coverage: the significant facts of the input (decisions with reasons, commitments, dated numbers, statuses with identifiers, people and roles) are present; trivia does not count.
3. Rules of the prompt: same language and script as the input, never translated; dates converted only when unambiguous; fields used as the schema intends.
4. For consolidation: no information lost in an update, merges only of observations that truly say the same thing, no duplicate creates, ids copied exactly.

Never invent a fact about the input yourself: quote it when you cite an error. Say only what you can see in the packet; when you cannot tell, say so.

Output exactly {n} lines of JSON, one per case, and nothing else:
{{"case": "case1", "winner": "A" | "B" | "tie", "margin": "clear" | "narrow", "errors_A": "<short, with quotes>", "errors_B": "<short, with quotes>", "reason": "<one sentence>"}}
"""

# ============================================================
# Shared
# ============================================================


def _engine() -> None:
    """Engine imports are deferred: `serve` runs on the server, where the engine is absent."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def bench_home() -> Path:
    _engine()
    from scripts.utils.data_path import data_path
    return data_path("hindsight-model-bench")


def write_text(path: Path, text: str) -> None:
    _engine()
    from scripts.utils.atomic import atomic_write_text
    atomic_write_text(path, text)


def setting(flag: str | None, name: str) -> str:
    if flag:
        return flag
    _engine()
    from scripts.utils.api import load_api_key
    value = load_api_key(name, required=False)
    if not value:
        sys.exit(f"{name} is not set: pass it as a flag or put it in .env")
    return value


def resolve_run(arg: str, *, create: bool = False) -> Path:
    """A path when it looks like one or exists, otherwise a run under the bench home."""
    p = Path(arg).expanduser()
    if os.sep in arg or p.exists():
        run = p.resolve()
    else:
        home = bench_home().resolve()
        run = (home / arg).resolve()
        if run.parent != home:
            sys.exit(f"refusing run name {arg!r}: it leaves {home}")
    if create:
        run.mkdir(parents=True, exist_ok=True)
    elif not run.is_dir():
        sys.exit(f"no such run directory: {run}")
    return run


def load_run_meta(run: Path) -> dict:
    f = run / "run.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def samples_file(arg: str | None, meta: dict) -> Path:
    if arg:
        return Path(arg).expanduser().resolve()
    if meta.get("samples"):
        return Path(meta["samples"])
    return bench_home() / "samples.json"


def load_samples(path: Path) -> dict:
    if not path.is_file():
        sys.exit(f"no sample set at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("samples"):
        sys.exit(f"{path} holds no samples")
    return data


def samples_changed(path: Path, meta: dict) -> bool:
    """True when the run recorded a samples hash and `path` no longer has it.

    replay keeps no copy of the samples, only their path and hash, so a set
    edited after the replay would pair the outputs with inputs the models never
    saw. A hand-built run with no recorded hash is not checked.
    """
    want = meta.get("samples_sha256")
    return bool(want) and hashlib.sha256(path.read_bytes()).hexdigest() != want


def load_results(path: Path) -> list[dict]:
    if not path.is_file():
        sys.exit(f"no results at {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ============================================================
# serve: on the memory server
# ============================================================


def cmd_serve(a: argparse.Namespace) -> int:
    here = Path(a.config).resolve().parent
    done, status = 0, "ok"
    # Everything after this line is inside the try, startup included: an import
    # or key-file failure must still write `done`, or the poller waits forever.
    try:
        cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
        from hindsight_api.engine.consolidation.consolidator import _ConsolidationBatchResponse
        from hindsight_api.engine.structured_output import strict_json_schema
        data = json.loads((here / "samples.json").read_text(encoding="utf-8"))
        key = Path(cfg["key_file"]).read_text(encoding="utf-8").strip()
        consolidation_schema = strict_json_schema(_ConsolidationBatchResponse)
        retain_schema = data["retain_schema"]
        stripped = None
        if cfg.get("strip_schema"):
            from hindsight_api.engine.structured_output import _strip_numeric_bounds, _strip_ref_siblings
            stripped = _strip_numeric_bounds(_strip_ref_siblings(retain_schema))
        models, extra = cfg["models"], cfg.get("extra", {})
        with open(here / "results.jsonl", "w", encoding="utf-8") as out:
            for n, s in enumerate(data["samples"]):
                for m in (models if n % 2 == 0 else models[::-1]):
                    if s["kind"] == "retain":
                        schema = stripped if m in cfg.get("strip_schema", []) else retain_schema
                    else:
                        schema = consolidation_schema
                    body = {"model": m, "messages": s["messages"], "temperature": s["temperature"],
                            **extra.get(m, {}),
                            "response_format": {"type": "json_schema", "json_schema":
                                                {"name": "r", "strict": True, "schema": schema}}}
                    out.write(json.dumps(replay_one(cfg, key, s, m, body), ensure_ascii=False) + "\n")
                    out.flush()
                    done += 1
    except BaseException as exc:
        status = f"failed: {type(exc).__name__}: {str(exc)[:200]}"
        raise
    finally:
        (here / "done").write_text(f"{status} {done}\n", encoding="utf-8")
    return 0


def replay_one(cfg: dict, key: str, s: dict, model: str, body: dict) -> dict:
    rec = {"sample": s["id"], "kind": s["kind"], "model": model}
    user_agent = cfg["user_agent"]  # the workspace value, resolved by replay: the engine is absent here
    req = urllib.request.Request(  # noqa: S310 - the server's loopback proxy
        cfg.get("proxy_url", PROXY_URL), data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": user_agent})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg.get("timeout", 600)) as r:  # noqa: S310 - the server's loopback proxy
            d = json.load(r)
        rec["secs"] = round(time.time() - t0, 1)
        content = d["choices"][0]["message"]["content"]
        rec["usage"], rec["output"] = d.get("usage"), content
        try:
            o = json.loads(content)
            rec["valid"] = True
            rec["n"] = (len(o.get("facts", [])) if s["kind"] == "retain"
                        else {k: len(v) for k, v in o.items() if isinstance(v, list)})
        except (ValueError, AttributeError) as exc:
            rec["valid"], rec["err"] = False, str(exc)[:200]
    except urllib.error.HTTPError as exc:
        rec["secs"], rec["valid"] = round(time.time() - t0, 1), False
        rec["err"] = f"HTTP {exc.code} " + exc.read()[:300].decode("utf8", "replace")
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        rec["secs"], rec["valid"] = round(time.time() - t0, 1), False
        rec["err"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return rec


# ============================================================
# replay / fetch: this machine, over ssh
# ============================================================


def ssh(host: str, command: str, *, data: bytes | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", host, command],
                          input=data, capture_output=True, timeout=timeout, check=False)


def parse_extra(items: list[str], models: list[str]) -> dict:
    extra = {}
    for item in items:
        name, sep, raw = item.partition("=")
        if not sep or name not in models:
            sys.exit(f"--extra {item!r}: expected MODEL=JSON for one of {models}")
        value = json.loads(raw)
        if not isinstance(value, dict):
            sys.exit(f"--extra {item!r}: the JSON must be an object")
        extra[name] = value
    return extra


def cmd_replay(a: argparse.Namespace) -> int:
    _engine()
    from scripts.utils.user_agent import user_agent
    from scripts.utils.workspace import get_default_tz
    models = a.model
    if len(set(models)) != len(models):
        sys.exit("a model is named twice")
    for m in a.strip_schema:
        if m not in models:
            sys.exit(f"--strip-schema {m}: not one of {models}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", a.name):
        sys.exit("--name: lowercase letters, digits and hyphens")
    spath = samples_file(a.samples, {})
    samples = load_samples(spath)
    now = datetime.now(get_default_tz())
    run_id = f"{now:%Y-%m-%d}-{a.name}"
    meta = {
        "run": run_id, "created": now.isoformat(timespec="seconds"),
        "host": setting(a.host, "HINDSIGHT_BENCH_HOST"),
        "python": setting(a.python, "HINDSIGHT_BENCH_PYTHON"),
        "remote_dir": f"{REMOTE_BASE}/{run_id}",
        "models": models, "extra": parse_extra(a.extra, models), "strip_schema": a.strip_schema,
        "samples": str(spath), "samples_sha256": hashlib.sha256(spath.read_bytes()).hexdigest(),
        "n_samples": len(samples["samples"]), "timeout": a.timeout,
    }
    config = {"models": models, "extra": meta["extra"], "strip_schema": a.strip_schema,
              "key_file": setting(a.key_file, "HINDSIGHT_BENCH_KEY_FILE"), "timeout": a.timeout,
              "user_agent": user_agent()}
    rd, py = shlex.quote(meta["remote_dir"]), shlex.quote(meta["python"])
    # `pid` lets the poller tell a serve that died before writing `done` (a
    # missing interpreter, a kill) from one still running.
    # The braces keep `&` from backgrounding the `cd` with the rest: `pid` is
    # written in the run directory, by the shell that did the `cd`.
    launch = (f"cd {rd} && rm -f done results.jsonl pid && {{ setsid nohup {py} bench.py serve config.json"
              " > serve.out 2> serve.err < /dev/null & echo $! > pid; } && echo started")
    if a.dry_run:
        print(json.dumps({"run.json": meta, "config.json": config}, indent=2, ensure_ascii=False))
        print(f"\nwould upload bench.py, samples.json, config.json to {meta['host']}:{meta['remote_dir']}")
        print(f"would run on {meta['host']}: {launch}")
        return 0
    run = resolve_run(run_id, create=True)
    if (run / "results.jsonl").exists():
        sys.exit(f"{run} already holds results; pick another --name")
    # The local check alone misses a run started whose results were never
    # fetched: the launch would delete them on the server. Ask the server.
    r = ssh(meta["host"], f"test -e {rd}/results.jsonl -o -e {rd}/done -o -e {rd}/pid && echo taken; true")
    if r.returncode:
        sys.exit(f"could not check {meta['host']}:{meta['remote_dir']} (ssh exit {r.returncode}): "
                 f"{r.stderr.decode()[-300:]}")
    if b"taken" in r.stdout:
        sys.exit(f"{meta['host']}:{meta['remote_dir']} already holds a run; "
                 f"fetch it with: {Path(__file__).name} fetch --run {run_id}, or pick another --name")
    write_text(run / "run.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    for name, payload in (("bench.py", Path(__file__).read_bytes()), ("samples.json", spath.read_bytes()),
                          ("config.json", json.dumps(config).encode())):
        r = ssh(meta["host"], f"mkdir -p {rd} && cat > {rd}/{name}", data=payload, timeout=300)
        if r.returncode:
            sys.exit(f"upload of {name} failed (ssh exit {r.returncode}): {r.stderr.decode()[-300:]}")
    r = ssh(meta["host"], launch)
    if r.returncode or b"started" not in r.stdout:
        sys.exit(f"could not start serve (ssh exit {r.returncode}): {r.stderr.decode()[-300:]}")
    print(f"started {len(models)} model(s) x {meta['n_samples']} sample(s) on {meta['host']}; run {run}")
    return wait_and_fetch(run, meta, a.poll)


def cmd_fetch(a: argparse.Namespace) -> int:
    run = resolve_run(a.run)
    meta = load_run_meta(run)
    if not meta.get("host"):
        sys.exit(f"{run}/run.json names no host: not a run replay started")
    return wait_and_fetch(run, meta, a.poll)


def wait_and_fetch(run: Path, meta: dict, poll: int) -> int:
    rd = shlex.quote(meta["remote_dir"])
    expected = meta["n_samples"] * len(meta["models"])
    failures, status = 0, ""
    while True:
        try:
            # Liveness is read BEFORE `done`: a serve that wrote `done` and then
            # exited is seen with its `done`, never as dead without one.
            r = ssh(meta["host"], f"cd {rd} && (wc -l < results.jsonl 2>/dev/null || echo 0); "
                    "if [ ! -f pid ]; then echo '?'; elif kill -0 \"$(cat pid)\" 2>/dev/null; then echo 1; "
                    "else echo 0; fi; cat done 2>/dev/null; true")
        except subprocess.TimeoutExpired:
            r = None
        if r is None or r.returncode:
            failures += 1
            print(f"poll failed ({failures}); the remote run is unaffected", file=sys.stderr)
            if failures >= 20:
                sys.exit(f"gave up polling; resume with: {Path(__file__).name} fetch --run {run}")
        else:
            failures = 0
            lines = r.stdout.decode().split("\n") + ["", ""]
            count = int(lines[0].strip() or 0)
            alive = lines[1].strip()
            status = "\n".join(lines[2:]).strip()
            print(f"{count}/{expected} {status}".rstrip(), flush=True)
            if status:
                break
            if alive == "0":
                err = ssh(meta["host"], f"tail -c 2000 {rd}/serve.err 2>/dev/null; true")
                print(err.stdout.decode("utf-8", "replace"), file=sys.stderr)
                sys.exit(f"serve on {meta['host']} exited without writing done; its serve.err is above")
        time.sleep(poll)
    for name in ("results.jsonl", "serve.err"):
        try:
            r = ssh(meta["host"], f"cat {rd}/{name}", timeout=600)
        except subprocess.TimeoutExpired:
            sys.exit(f"fetch of {name} timed out; retry: fetch --run {run}")
        if r.returncode:
            sys.exit(f"fetch of {name} failed (ssh exit {r.returncode}); retry: fetch --run {run}")
        if r.stdout or name == "results.jsonl":
            write_text(run / name, r.stdout.decode("utf-8"))
    got = len(load_results(run / "results.jsonl"))
    print(f"fetched {got}/{expected} record(s) into {run / 'results.jsonl'}; remote status: {status}")
    return 0 if status.startswith("ok") and got == expected else 1


# ============================================================
# packet / judge
# ============================================================


def cmd_packet(a: argparse.Namespace) -> int:
    run = resolve_run(a.run)
    meta = load_run_meta(run)
    models = a.models or meta.get("models")
    if not models or len(models) != 2:
        sys.exit("a packet compares exactly two models: pass --models A B")
    if a.seed is not None:
        seed = a.seed
    else:
        _engine()
        from scripts.utils.workspace import get_default_tz
        seed = int((meta.get("created") or datetime.now(get_default_tz()).isoformat())[:10].replace("-", ""))
    spath = samples_file(a.samples, meta)
    data = load_samples(spath)
    if samples_changed(spath, meta):
        sys.exit(f"{spath} is not the sample set this run replayed (sha256 differs from run.json); "
                 "pass --samples with the original set")
    results: dict[str, dict] = {}
    for r in load_results(run / "results.jsonl"):
        results.setdefault(r["sample"], {})[r["model"]] = r
    prompts = {s["messages"][0]["content"] for s in data["samples"]
               if s["kind"] == "retain" and s["messages"][0]["role"] == "system"}
    if len(prompts) > 1:
        sys.exit("the retain samples carry different system prompts; this packet format shares one")
    rng, key, out = random.Random(seed), {}, []  # noqa: S311 - a reproducible shuffle, not a secret
    for i, s in enumerate([s for s in data["samples"] if s["id"] in results], 1):
        order = list(models)
        rng.shuffle(order)
        key[f"case{i}"] = {"A": order[0], "B": order[1]}
        out.append(f"=== case{i} ({s['kind']}) ===\n--- INPUT ---")
        for m in s["messages"]:
            if m["role"] == "system" and s["kind"] == "retain":
                continue
            out.append(f"[{m['role']}]\n{m['content']}")
        for label, m in zip("AB", order, strict=True):
            r = results[s["id"]].get(m, {})
            out.append(f"--- OUTPUT {label} ---\n{r.get('output') or 'INVALID: ' + str(r.get('err'))}")
    old_key = run / "judge-key.json"
    stale = sorted(f.name for f in run.glob("verdicts-*"))
    if stale and (not old_key.is_file() or json.loads(old_key.read_text(encoding="utf-8")) != key):
        sys.exit(f"{run} holds verdicts for the current key ({', '.join(stale)}); this packet would assign "
                 "A and B differently and unblind would credit the wrong model. Move them out of the run "
                 "first, or repeat the packet with the seed in run.json")
    write_text(run / "judge-packet.md", "\n".join(out))
    write_text(run / "judge-key.json", json.dumps(key))
    write_text(run / "judge-instructions.md", JUDGE_INSTRUCTIONS.format(n=len(key)))
    if prompts:
        write_text(run / "extraction-system-prompt.md", prompts.pop())
    meta["packet"] = {"models": models, "seed": seed, "cases": len(key)}
    write_text(run / "run.json", json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(key)} cases, seed {seed}, in {run / 'judge-packet.md'}")
    return 0


def parse_verdicts(text: str) -> list[dict]:
    found = []
    for line in text.splitlines():
        line = line.strip().strip("`").strip()
        if not line.startswith("{"):
            continue
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict) and "case" in v and "winner" in v:
            found.append(v)
    return found


def cmd_judge(a: argparse.Namespace) -> int:
    run = resolve_run(a.run)
    need = ["judge-instructions.md", "judge-packet.md", "judge-key.json"]
    missing = [n for n in need if not (run / n).is_file()]
    if missing:
        sys.exit(f"{run} lacks {missing}; run packet first")
    n = len(json.loads((run / "judge-key.json").read_text(encoding="utf-8")))
    extraction = run / "extraction-system-prompt.md"
    if a.judge == "claude":
        print(f"""Brief for a Claude subagent judge. Paste it into the Agent tool as it stands.

You are the blind judge of a model comparison. Read, in full, {run / 'judge-instructions.md'}, then {extraction if extraction.is_file() else '(no retain cases)'} and {run / 'judge-packet.md'}. Do not open judge-key.json or results.jsonl in that directory, and do not try to find out which model wrote which output: the comparison is only worth something blind.
Nothing is invented: every error you name is quoted from the packet, and when you cannot tell, say so. Judge each of the {n} cases on its own.
Write exactly {n} JSON lines, in the format the instructions give, to {run / 'verdicts-claude.jsonl'} and nothing else into that file. Then reply with one line: how many verdicts you wrote.
Then: {Path(__file__).name} unblind --run {run}""")
        return 0
    _engine()
    from scripts.utils.proxy_transport import call_model
    parts = [(run / "judge-instructions.md").read_text(encoding="utf-8")]
    if extraction.is_file():
        parts.append("=== extraction-system-prompt.md ===\n" + extraction.read_text(encoding="utf-8"))
    parts += ["=== PACKET ===\n" + (run / "judge-packet.md").read_text(encoding="utf-8"),
              f"Now output the {n} JSON lines."]
    raw = call_model("k3", "\n\n".join(parts), temperature=0.2, max_tokens=16384, timeout=900,
                     reasoning_effort="high")
    write_text(run / "verdicts-kimi.raw", raw)
    verdicts = parse_verdicts(raw)
    write_text(run / "verdicts-kimi.jsonl", "".join(json.dumps(v, ensure_ascii=False) + "\n" for v in verdicts))
    print(f"kimi: {len(verdicts)}/{n} verdicts in {run / 'verdicts-kimi.jsonl'}")
    return 0 if len(verdicts) == n else 1


# ============================================================
# unblind / report
# ============================================================


def unblind(run: Path) -> tuple[list[str], dict]:
    """The unblinded tally as text lines and as data; empty when no verdict file exists."""
    key = json.loads((run / "judge-key.json").read_text(encoding="utf-8"))
    kinds = dict(CASE_HEADER.findall((run / "judge-packet.md").read_text(encoding="utf-8")))
    models = sorted({m for pair in key.values() for m in pair.values()})
    judges = {}
    for f in sorted(run.glob("verdicts-*.jsonl")):
        judges[f.stem.removeprefix("verdicts-")] = {
            v["case"]: v for v in parse_verdicts(f.read_text(encoding="utf-8"))}
    if not judges:
        return [], {}
    per_case: dict[str, dict] = {}
    tally: dict[str, dict] = {}
    for judge, verdicts in judges.items():
        t = tally[judge] = {"all": {}, **{k: {} for k in KINDS}}
        for case in key:
            v = verdicts.get(case)
            w = v.get("winner") if v else None
            label = key[case][w] if w in ("A", "B") else ("tie" if w == "tie" else ("missing" if v is None else "invalid"))
            per_case.setdefault(case, {})[judge] = (label, (v or {}).get("margin", ""))
            for bucket in ("all", kinds.get(case, "?")):
                t.setdefault(bucket, {})
                t[bucket][label] = t[bucket].get(label, 0) + 1

    def counts(c: dict) -> str:
        order = sorted(models, key=lambda m: (-c.get(m, 0), m)) + ["tie"]
        extra = [k for k in c if k not in order]
        return ", ".join(f"{k} {c.get(k, 0)}" for k in order + extra)

    lines = ["## Blind judges, unblinded", ""]
    for judge, t in tally.items():
        lines.append(f"- {judge} ({len(judges[judge])} verdicts of {len(key)}): {counts(t['all'])}")
        for k in KINDS:
            if t.get(k):
                lines.append(f"  - {k}: {counts(t[k])}")
    lines += ["", "Per case (" + " | ".join(judges) + "):"]
    disagree = []
    for case in key:
        cells = per_case[case]
        lines.append(f"- {case} ({kinds.get(case, '?')}): " + " | ".join(
            f"{lab} ({mar})" if mar else lab for lab, mar in (cells[j] for j in judges)))
        if len({cells[j][0] for j in judges}) > 1:
            disagree.append(f"{case} (" + ", ".join(f"{j} {cells[j][0]}" for j in judges) + ")")
    if len(judges) > 1:
        lines += ["", "Disagreements between judges: " + ("; ".join(disagree) if disagree else "none")]
    return lines, {"tally": tally, "per_case": per_case, "disagreements": disagree}


def cmd_unblind(a: argparse.Namespace) -> int:
    run = resolve_run(a.run)
    lines, data = unblind(run)
    if not lines:
        sys.exit(f"no verdicts-*.jsonl in {run}")
    out = Path(a.out).expanduser().resolve() if a.out else run
    write_text(out / "unblind.md", "\n".join(lines) + "\n")
    write_text(out / "unblind.json", json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print("\n".join(lines))
    print(f"\nwritten: {out / 'unblind.md'}")
    return 0


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def script_share(text: str) -> tuple[int, float]:
    cyr, lat = len(CYRILLIC.findall(text)), len(LATIN.findall(text))
    return cyr, (cyr / (cyr + lat) if cyr + lat else 0.0)


def output_text(r: dict) -> str:
    raw = r.get("output") or ""
    try:
        return "\n".join(_strings(json.loads(raw)))
    except ValueError:
        return raw


def cmd_report(a: argparse.Namespace) -> int:
    run = resolve_run(a.run)
    meta = load_run_meta(run)
    results = load_results(Path(a.results).expanduser().resolve() if a.results else run / "results.jsonl")
    models = list(dict.fromkeys(meta.get("models") or [r["model"] for r in results]))
    lines = [f"# Hindsight model bench: {run.name}", ""]
    if meta.get("created"):
        lines.append(f"Replayed {meta['created']} on {meta.get('host', '?')}; samples {meta.get('samples')} (sha256 {meta.get('samples_sha256', '?')[:12]}).")
    lines += ["Seconds per call are an isolated replay, not production queueing.", "", "## Speed and validity", ""]
    for m in models:
        for k in KINDS:
            rs = [r for r in results if r["model"] == m and r["kind"] == k]
            if not rs:
                continue
            ok = [r for r in rs if r.get("valid")]
            secs = [r["secs"] for r in ok if r.get("secs") is not None]
            line = f"- {m}, {k}: {len(ok)}/{len(rs)} valid"
            if secs:
                line += f"; median {statistics.median(secs):g} s, max {max(secs):g} s"
            if k == "retain" and ok:
                facts = [r["n"] for r in ok if isinstance(r.get("n"), int)]
                toks = [r["usage"]["completion_tokens"] for r in ok if (r.get("usage") or {}).get("completion_tokens")]
                if facts:
                    line += f"; facts per document {statistics.mean(facts):.1f}"
                if toks:
                    line += f"; mean output tokens {statistics.mean(toks):.0f}"
            lines.append(line)
    lines += ["", "## Language, by script (retain)", ""]
    spath = samples_file(a.samples, meta)
    if not spath.is_file():
        lines.append(f"NOT EVALUATED: no sample set at {spath}; pass --samples.")
    elif samples_changed(spath, meta):
        lines.append(f"NOT EVALUATED: {spath} is not the sample set this run replayed (sha256 differs "
                     "from run.json); pass --samples with the original set.")
    else:
        samples = load_samples(spath)["samples"]
        index = {r_id: i for i, r_id in enumerate(dict.fromkeys(
            s["id"] for s in samples if any(r["sample"] == s["id"] for r in results)), 1)}
        lines.append(f"A document counts as Cyrillic-script when at least {CYRILLIC_INPUT:.0%} of its letters are Cyrillic; "
                     "an output is flagged LOST when its Cyrillic share is under half the input's.")
        for s in samples:
            if s["kind"] != "retain" or s["id"] not in index:
                continue
            cyr, share = script_share(s["messages"][-1]["content"])
            if share < CYRILLIC_INPUT:
                continue
            cells = []
            for m in models:
                r = next((r for r in results if r["sample"] == s["id"] and r["model"] == m), None)
                if r is None:
                    continue
                oc, osh = script_share(output_text(r))
                cells.append(f"{m} {oc} ({osh:.0%}){' LOST' if osh < share / 2 else ''}")
            lines.append(f"- case{index[s['id']]}: input {cyr} Cyrillic ({share:.0%}); output " + "; ".join(cells))
    lines.append("")
    if (run / "judge-key.json").is_file() and (run / "judge-packet.md").is_file():
        ulines, _ = unblind(run)
        lines += ulines or ["## Blind judges", "", "NOT EVALUATED: no verdicts-*.jsonl yet."]
    else:
        lines += ["## Blind judges", "", "NOT EVALUATED: no packet yet."]
    lines += ["", "Not checked by this report: the real-world truth of any fact; behaviour under production load; reflect."]
    out = Path(a.out).expanduser().resolve() if a.out else run
    write_text(out / "report.md", "\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwritten: {out / 'report.md'}")
    return 0


# ============================================================
# CLI
# ============================================================


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("replay", help="replay the sample set to N models on the memory server")
    r.add_argument("--name", required=True, help="slug; the run is <YYYY-MM-DD>-<slug>")
    r.add_argument("--model", action="append", required=True)
    r.add_argument("--extra", action="append", default=[], metavar="MODEL=JSON")
    r.add_argument("--strip-schema", action="append", default=[], metavar="MODEL")
    r.add_argument("--samples")
    r.add_argument("--host")
    r.add_argument("--python")
    r.add_argument("--key-file")
    r.add_argument("--timeout", type=int, default=600, help="seconds per request")
    r.add_argument("--poll", type=int, default=30)
    r.add_argument("--dry-run", action="store_true", help="print what would be sent; no ssh")

    f = sub.add_parser("fetch", help="wait for a started replay and copy its results back")
    f.add_argument("--run", required=True)
    f.add_argument("--poll", type=int, default=30)

    k = sub.add_parser("packet", help="blind A/B packet and key")
    k.add_argument("--run", required=True)
    k.add_argument("--models", nargs=2)
    k.add_argument("--seed", type=int)
    k.add_argument("--samples")

    j = sub.add_parser("judge", help="Kimi K3 judge, or the brief for a Claude subagent")
    j.add_argument("--run", required=True)
    j.add_argument("--judge", choices=("kimi", "claude"), required=True)

    u = sub.add_parser("unblind", help="apply the key to every verdicts-*.jsonl")
    u.add_argument("--run", required=True)
    u.add_argument("--out")

    t = sub.add_parser("report", help="speed, validity, facts, language and judge tally")
    t.add_argument("--run", required=True)
    t.add_argument("--samples")
    t.add_argument("--results")
    t.add_argument("--out")

    s = sub.add_parser("serve", help="(memory server) run the replay described by CONFIG")
    s.add_argument("config")

    a = p.parse_args(argv)
    return {"replay": cmd_replay, "fetch": cmd_fetch, "packet": cmd_packet, "judge": cmd_judge,
            "unblind": cmd_unblind, "report": cmd_report, "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
