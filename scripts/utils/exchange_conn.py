"""The one place an exchangelib `Account` is built for the Exchange scripts.

`exchange-task.py`, `gal-export.py`, `send-email.py` and `sync-exchange.py` each
carried their own copy of the same five steps: credentials, the workspace
User-Agent, a `Configuration`, and an `Account` with `autodiscover=False` and
delegate access. They are here once, so a change to how this workspace reaches
Exchange lands in every caller at once. Each script keeps what is genuinely its
own: how it reads `.env`, what it prints, and what it touches first.

**Building an `Account` makes no network call.** MEASURED 2026-09-28 on
exchangelib 5.6.0 with every socket entry point replaced by one that raises:
`Account(..., autodiscover=False)` completed with zero connection attempts, and
the first folder access made the first one. So a failure to reach the server or
to authenticate surfaces at the caller's first real request, never here. That is
why no retry lives in this module: `send-email.py` wrapped this constructor in a
three-attempt loop that could only ever repeat a local error, and it was not
copied into the other three. A retry that means something belongs around the
first request, where `send-email.py` already has one (`_is_safe_to_resend`).
"""
from __future__ import annotations

from typing import Any

from scripts.utils.user_agent import user_agent


def build_account(config: dict, *, version: Any = None):
    """Return an exchangelib `Account` for `config`. No network I/O.

    `config` carries `EXCHANGE_SERVER`, `EXCHANGE_EMAIL`, `EXCHANGE_USERNAME` and
    `EXCHANGE_PASSWORD`, the shape every caller's `load_config()` returns.
    `version` is passed to `Configuration` as given; `None` is exchangelib's own
    default, so a caller that passes nothing gets exactly what omitting it gave.

    Exits 1 with an actionable message if the `email` extra is not installed,
    through `optdeps.require`, as each copy did before.
    """
    from scripts.utils.optdeps import require
    exchangelib = require("exchangelib", extra="email")
    credentials = exchangelib.Credentials(
        username=config["EXCHANGE_USERNAME"],
        password=config["EXCHANGE_PASSWORD"],
    )
    # exchangelib sends its own agent unless told otherwise; the workspace one
    # lives in config/user-agent.txt (scripts/utils/user_agent.py).
    from exchangelib.protocol import BaseProtocol
    BaseProtocol.USERAGENT = user_agent()
    exchange_config = exchangelib.Configuration(
        server=config["EXCHANGE_SERVER"],
        credentials=credentials,
        version=version,
    )
    return exchangelib.Account(
        primary_smtp_address=config["EXCHANGE_EMAIL"],
        config=exchange_config,
        autodiscover=False,
        access_type=exchangelib.DELEGATE,
    )
