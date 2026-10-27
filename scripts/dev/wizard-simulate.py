#!/usr/bin/env python3
"""wizard-simulate.py -- dev harness to replay canned wizard answers.

canned.yaml format:
    answers:
      company_short_name: "Acme"
      ceo_voice:
        value: "Short sentences."
        draft: "Expanded voice brief..."
        draft_approved: true
      core_values: ["Trust", "Speed"]
    skipped: [calendar_policy]

Refuses to run against workspaces tagged type: "ceo-master" - no override.

Tests: none since the 2026-09-21 test-suite split. The refusal against a
`ceo-master` workspace is unchanged and now rests on the code below being read
before it is edited.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--answers", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args(argv)

    # Safety: refuse to run against the CEO master workspace.
    # The `--force-ceo-master` flag below bypasses apply-script detection, but
    # we do NOT want this dev harness to ever touch a real ceo-master workspace
    # by accident. No override flag is offered.
    #
    # The guard reads `.workspace-identity.json` from WHATEVER `--workspace`
    # points at, so a copied ceo-master identity is refused too. To exercise a
    # master-like layout, copy the workspace and DELETE the identity file, which
    # the apply script reads as a public clone (`ceo-master` is the only type it
    # accepts since 2026-09-25).
    identity = args.workspace / ".workspace-identity.json"
    if identity.exists():
        try:
            data = json.loads(identity.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                # The same shape check this file already applies three times to
                # the canned-answers file, applied to the SAFETY-CRITICAL one.
                # `json.loads` answers with any JSON value, so `[]` reached
                # `.get` as an AttributeError past the JSONDecodeError handler.
                # The refusal happened to hold - by crashing - but a merely
                # corrupt non-master workspace could not be run at all, and the
                # clean refusal path was unreachable.
                print(f"ERROR: {identity} is valid JSON but not an object; "
                      f"cannot tell whether this is a ceo-master workspace.",
                      file=sys.stderr)
                return 2
            if data.get("type") == "ceo-master":
                print(
                    f"REFUSED: --workspace {args.workspace} is a CEO master workspace. "
                    f"This harness never runs against ceo-master. Copy to a tmpdir first.",
                    file=sys.stderr,
                )
                return 2
        except json.JSONDecodeError:
            print(f"ERROR: malformed .workspace-identity.json in {args.workspace}",
                  file=sys.stderr)
            return 2

    # Checked, like the identity file and the apply script around it. A typo'd
    # path used to give a raw FileNotFoundError traceback instead of the clean
    # `ERROR: ...` this file uses everywhere else.
    if not args.answers.is_file():
        print(f"ERROR: answers file not found: {args.answers}", file=sys.stderr)
        return 2
    try:
        canned = yaml.safe_load(args.answers.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        # `UnicodeDecodeError`: the decode in `read_text` fails before YAML runs
        # and is neither an OSError nor a YAMLError (MEASURED 2026-09-01).
        print(f"ERROR: could not read {args.answers}: {exc}", file=sys.stderr)
        return 2
    # Shape checks, not `or {}`: a top-level list or a bare-string `skipped:`
    # would pass an `or` and fail later (one subprocess per character).
    if canned is None:
        canned = {}
    if not isinstance(canned, dict):
        print(f"ERROR: {args.answers} is a {type(canned).__name__}, not a mapping "
              f"with `answers:` / `skipped:` keys.", file=sys.stderr)
        return 2

    answers = canned.get("answers") or {}
    if not isinstance(answers, dict):
        print(f"ERROR: `answers:` is a {type(answers).__name__}, not a mapping "
              f"of question id to value.", file=sys.stderr)
        return 2
    skipped = canned.get("skipped") or []
    if not isinstance(skipped, list):
        print(f"ERROR: `skipped:` is a {type(skipped).__name__}, not a list of "
              f"question ids.", file=sys.stderr)
        return 2
    # The ELEMENTS too: an unquoted numeric YAML key (`answers: {7: "Acme"}`)
    # would reach `subprocess.run` as an int and raise TypeError.
    bad_ids = [qid for qid in list(answers) + list(skipped)
               if not isinstance(qid, str)]
    if bad_ids:
        shown = ", ".join(f"{qid!r} ({type(qid).__name__})" for qid in bad_ids)
        print(f"ERROR: question ids must be strings; {args.answers} carries "
              f"{shown}. Quote them in the YAML.", file=sys.stderr)
        return 2
    # Resolve apply-wizard-answers.py relative to this harness's location.
    # If the harness is ever moved out of scripts/dev/, fail fast with a clear error.
    apply_script = Path(__file__).resolve().parent.parent / "apply-wizard-answers.py"
    if not apply_script.exists():
        print(f"ERROR: apply script not found at {apply_script}. "
              f"This harness assumes it lives at scripts/dev/wizard-simulate.py.",
              file=sys.stderr)
        return 2

    for qid, value in answers.items():
        if isinstance(value, dict):
            payload = value
        else:
            payload = {"value": value}
        result = subprocess.run(
            [sys.executable, str(apply_script),
             "--question", qid, "--value-from-stdin", "--force-ceo-master"],
            cwd=args.workspace, input=json.dumps(payload),
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"FAILED on {qid}: {result.stderr}", file=sys.stderr)
            return result.returncode
        print(f"OK  {qid}: {result.stdout.strip()}")

    for qid in skipped:
        result = subprocess.run(
            [sys.executable, str(apply_script), "--skip", qid, "--force-ceo-master"],
            cwd=args.workspace, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"FAILED on skip {qid}: {result.stderr}", file=sys.stderr)
            return result.returncode
        print(f"SKIP {qid}")

    # Checked like every call above, so a failed `--status` cannot exit 0.
    status = subprocess.run(
        [sys.executable, str(apply_script), "--status", "--force-ceo-master"],
        cwd=args.workspace, capture_output=True, text=True,
    )
    if status.returncode != 0:
        print(f"FAILED on --status (exit {status.returncode}): "
              f"{status.stderr.strip() or '(no stderr)'}", file=sys.stderr)
        return status.returncode
    print(f"STATUS: {status.stdout}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
