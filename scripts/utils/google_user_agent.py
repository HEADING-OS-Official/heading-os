"""Make the Google client libraries send the workspace User-Agent.

The value is `scripts/utils/user_agent.py`'s; this module only reaches the three
HTTP paths the Google libraries use, none of which takes a User-Agent setting:

- the API calls `googleapiclient` makes, and the token refresh `AuthorizedHttp`
  performs on a 401, both over one `httplib2.Http`, which by default sends
  "Python-httplib2/<version>" and to which the API client appends its own
  "(gzip)" suffix;
- the explicit token refresh, `google.auth.transport.requests.Request`, which
  sends "python-requests/<version>";
- the OAuth consent flow's token exchange, a `requests_oauthlib` session.

Operator rule 2026-09-27: one User-Agent everywhere the workspace reaches the
outside world. So the httplib2 object used here replaces whatever User-Agent a
caller or library set with the workspace value, in any case spelling, rather
than prepending to it the way `googleapiclient.http.set_user_agent` does.

The Google libraries are imported inside each function, so importing this module
costs nothing for code that never talks to Google.
"""
from __future__ import annotations

from scripts.utils.user_agent import user_agent


def _force(headers: dict | None) -> dict:
    forced = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
    forced["user-agent"] = user_agent()
    return forced


def google_http(credentials):
    """An authorised httplib2 transport for `build(..., http=...)`, sending only our User-Agent."""
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp

    class _Http(httplib2.Http):
        def request(self, uri, method="GET", body=None, headers=None, *args, **kwargs):
            return super().request(uri, method, body, _force(headers), *args, **kwargs)

    http = _Http()
    # A resumable upload answers each chunk with 308 "Resume Incomplete" and no
    # Location header; httplib2 counts 308 as a redirect and raises
    # RedirectMissingLocation. `googleapiclient.http.build_http()` removes it the
    # same way. MEASURED 2026-10-07: the first live Drive upload of the Hindsight
    # snapshot failed on exactly this.
    http.redirect_codes = http.redirect_codes - {308}
    return AuthorizedHttp(credentials, http=http)


def google_refresh_request():
    """A `google.auth` transport request for `credentials.refresh(...)`."""
    import requests
    from google.auth.transport.requests import Request

    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    return Request(session)


def google_flow_user_agent(flow):
    """Set the workspace User-Agent on an OAuth flow's token-exchange session; returns the flow."""
    flow.oauth2session.headers["User-Agent"] = user_agent()
    return flow
