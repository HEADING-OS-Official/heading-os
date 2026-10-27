"""Operator identity seam - one place a HEADING OS instance sets who runs it.

The engine ships generic defaults (name "Operator", slug "operator") so a fresh
public clone is operator-agnostic. A real deployment supplies its own identity in
one place; every load-bearing default in the codebase resolves through here.

Resolution precedence (highest wins):
    1. environment  HEADING_OS_OPERATOR_{NAME,SLUG,GITHUB_ORG,VOICE_REFERENCE,
                                         EMAIL,CORPORATE_EMAIL_DOMAIN}
    2. data overlay `operator.yaml` under `data-config` (get_data_config_dir())
    3. engine-local config/operator.yaml   (gitignored; for a data-less clone)
    4. the shipped example scripts/operator.example.yaml (generic defaults)

Never raises, and that is now true. Every read/parse error, and every failure to
RESOLVE tier 2 at all, degrades to the next tier down and finally to the generic
dict at tier 4. Composes the existing
scripts.utils.workspace.resolve_config_with_example() helper for the
overlay->example decision and layers the engine-local + env tiers on top.

The documented sentinel. When nothing above tier 4 could be read, `get_operator()`
returns a copy of `_GENERIC`: name "Operator", slug "operator", github_org "",
email "", corporate_email_domain "". `operator_email_domain()` and
`corporate_email_domain()` therefore answer `""` and `operator_slug()` answers
`"operator"`. A caller that needs a REAL value must test the empty string; it must
not assume the value is real merely because no exception arrived.

Why this is the read that degrades rather than the read that refuses: callers
bind it at MODULE scope, so a raise from tier 2 (`DataRootError` when
`HEADING_OS_DATA` names no directory) killed them at import, before argparse,
until 2026-08-30. Nothing here writes, so falling to the lower tiers costs
nothing; the fall is announced once per process on stderr.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

OPERATOR_FILENAME = "operator.yaml"

# Shipped engine example (generic identity). scripts/operator.example.yaml.
_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "operator.example.yaml"

# The neutral identity a fresh clone resolves to.
_GENERIC: dict[str, str] = {
    "name": "Operator",
    "slug": "operator",
    "github_org": "",
    "voice_reference": "voice-guide",
    "email": "",
    "corporate_email_domain": "",
    "admin_email": "",
    # What the company is, in one phrase ("Acme Ltd, a logistics company"), and
    # the context block a council consult opens with. Both are instance data and
    # stay empty on a fresh clone, where the prompts that read them fall back to
    # generic wording.
    "company": "",
    "advisor_context": "",
    # The company as branded output prints it: its name ("Acme Ltd"), its short
    # form for file names ("ACME"), its site ("acme.example"). Empty on a fresh
    # clone, where footers, cover lines and file stems leave the company out.
    "company_name": "",
    "company_short": "",
    "company_site": "",
    # The one-line tagline a branded letterhead prints under the name. Empty
    # on a fresh clone, where the letterhead carries no tagline line.
    "company_tagline": "",
}

# field -> environment variable (highest-precedence tier).
_ENV_KEYS: dict[str, str] = {
    "name": "HEADING_OS_OPERATOR_NAME",
    "slug": "HEADING_OS_OPERATOR_SLUG",
    "github_org": "HEADING_OS_OPERATOR_GITHUB_ORG",
    "voice_reference": "HEADING_OS_OPERATOR_VOICE_REFERENCE",
    "email": "HEADING_OS_OPERATOR_EMAIL",
    "corporate_email_domain": "HEADING_OS_OPERATOR_CORPORATE_EMAIL_DOMAIN",
    "admin_email": "HEADING_OS_OPERATOR_ADMIN_EMAIL",
}


def _resolve_file() -> tuple[Path | None, bool]:
    """Return (path, is_real). is_real is False when the path is the generic example.

    Composes resolve_config_with_example() for the overlay->example decision, then
    inserts the engine-local config/operator.yaml tier between them: if the overlay
    file is absent (helper fell back to the example) but an engine-local file
    exists, prefer it.

    A tier 2 that cannot even be RESOLVED (no usable data root, corrupt workspace
    identity) is treated exactly like a tier 2 that is absent: skip it and carry
    on down. That is what makes the module docstring's "never raises" true. The
    reason is stated there and the fall is announced on stderr, never taken in
    silence.
    """
    from scripts.utils.data_path import DataPathError
    from scripts.utils.paths import DataRootError
    from scripts.utils.workspace import resolve_config_with_example, get_workspace_root

    try:
        resolved = resolve_config_with_example(OPERATOR_FILENAME, _EXAMPLE_PATH)
    # `DataPathError` since 2026-09-25: the helper reaches `data_path()` now
    # that HOS-5 section 6 deleted the seam, and the front door refuses a
    # damaged or incomplete registry where the seam fell back in silence.
    except (DataRootError, DataPathError, ValueError, OSError) as exc:
        # ValueError kept from a resolver removed 2026-09-28; whether anything
        # else on the chain still raises it was not re-checked.
        print(f"[operator-identity] the private data overlay could not be "
              f"resolved ({type(exc).__name__}: {exc}); reading the operator "
              f"identity from the engine-local config/operator.yaml or the "
              f"shipped example instead. github_org may be empty.",
              file=sys.stderr)
        resolved = _EXAMPLE_PATH
    if resolved == _EXAMPLE_PATH:
        engine_local = get_workspace_root() / "config" / OPERATOR_FILENAME
        if engine_local.exists():
            return engine_local, True
        return (_EXAMPLE_PATH if _EXAMPLE_PATH.exists() else None), False
    return resolved, True


def _load() -> tuple[dict, bool]:
    """Return (operator_dict, configured). configured is True when a real
    operator.yaml (overlay or engine-local) or any env var supplied a value."""
    import yaml

    data = dict(_GENERIC)
    configured = False

    path, is_real = _resolve_file()
    if path is not None and path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            # UnicodeDecodeError is raised inside read_text, before the parser,
            # and is not a YAMLError: a UTF-16 operator.yaml raised out of
            # get_operator() at import until 2026-09-01.
            loaded = {}
        if isinstance(loaded, dict):
            for key in _GENERIC:
                val = loaded.get(key)
                if val not in (None, ""):
                    data[key] = str(val)
                    if is_real:
                        configured = True

    for key, env_name in _ENV_KEYS.items():
        val = os.environ.get(env_name)
        if val not in (None, ""):
            data[key] = val
            configured = True

    return data, configured


@lru_cache(maxsize=1)
def _cached() -> tuple[dict, bool]:
    return _load()


def get_operator() -> dict:
    """Resolved operator identity dict (name/slug/github_org/voice_reference/email).

    Never raises. Returns the documented sentinel -- a copy of _GENERIC, so
    github_org "" and slug "operator" -- on a fresh clone AND whenever the data
    overlay cannot be resolved or read (see the module docstring). Cached; call
    _reset_cache() in tests after mutating env or files.
    """
    return dict(_cached()[0])


def operator_slug() -> str:
    """Operator short handle. 'operator' on an unconfigured clone."""
    return get_operator()["slug"]


def operator_email_domain() -> str:
    """The domain half of the operator's email, or '' when unconfigured.

    Added 2026-08-23, when `scripts/gal-export.py` had a tenant domain compiled
    in. The domain is instance identity like the slug, and it belongs here.
    """
    email = (get_operator().get("email") or "").strip()
    _, _, domain = email.partition("@")
    return domain.lower()


def corporate_email_domain() -> str:
    """The instance's CORPORATE mail domain, bare, or '' when unconfigured.

    Added 2026-09-01. Three engine scripts had a tenant mail domain compiled in
    and each used it to decide BEHAVIOUR, not branding: `INTERNAL_DOMAIN` in
    `scripts/email-intelligence.py` classified a conversation internal or
    external, `scripts/utils/crm.py` flagged a contact holding a company
    mailbox but not typed tribe, and `scripts/crm-health.py` printed the
    result. On any other deployment all three were dead code that never fired.

    Deliberately NOT `operator_email_domain()`. That answers "where does the
    operator's own mail live", and the personal and company domains are
    routinely different.

    Bare, with no leading '@', because the callers prepend their own. A caller
    that builds a match out of this MUST test the empty string first: an unset
    domain turns `f"@{d}" in address` into `"@" in address`, which is true of
    every address ever written, so the guard that warned about nobody would
    warn about everybody.
    """
    return (get_operator().get("corporate_email_domain") or "").strip().lstrip("@").lower()


def admin_email() -> str:
    """The inbox that reaches whoever ADMINISTERS this fleet. '' when unset.

    Added 2026-09-01, on the operator's decision, after a team bot
    was found offering one tenant's mailbox as a literal "reach a human" line.

    ## Why this is not `email`, and not `corporate_email_domain`

    `email` is "whoever runs THIS clone"; this field is whoever administers it.
    They may coincide, and that coincidence is what hides a mix-up.

    `corporate_email_domain` is a DOMAIN and answers a membership question. This
    is a whole ADDRESS and answers a routing question. Neither derives from the
    other: there is no rule that says the admin's mailbox sits on the corporate
    domain, and inventing a local part to bolt onto it would be a guess.

    ## Callers

    Fall back to `email` when this is empty, then refuse. An empty address in an
    outbound message is worse than no address at all, because the reader tries
    it. The team bot omits its whole "reach a human" line rather
    than ship a blank one.
    """
    return (get_operator().get("admin_email") or "").strip()


def voice_reference_path() -> Path | None:
    """The voice reference file `voice_reference` names, or None when unresolvable.

    HOS-234 part 5: the field holds a registry name (`voice-guide`), resolved
    through `config_names`. The old form, a path relative to the data root, is
    still read and said on stderr as deprecated. Unlike the identity fields this
    one refuses a name the registry does not place, loudly: a voice guide read
    from the wrong file is prose in the wrong voice, and nothing would say so.
    Not cached, so it follows the data root of the moment it is asked.
    """
    from scripts.utils.data_path import config_names, deprecated_config_path
    from scripts.utils.paths import get_data_root

    value = get_operator()["voice_reference"]
    field = "operator.yaml voice_reference"
    named = config_names(value, field=field)
    if named is None:
        deprecated_config_path(field, value)
        return get_data_root() / value
    if len(named) != 1:
        raise ValueError(f"{field}: {value!r} names {len(named)} files; it must name one")
    return named[0][1]


def company_brand() -> tuple[str, str, str]:
    """(company_name, company_short, company_site) for branded output; each '' when unset."""
    who = get_operator()
    return who["company_name"], who["company_short"], who["company_site"]


def _reset_cache() -> None:
    """Clear the identity cache. Intended for tests; not for production use."""
    _cached.cache_clear()
