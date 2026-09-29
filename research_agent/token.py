"""Bearer-token loading for the research agent's own HTTP calls.

The app gives each configured research-agent credential its own token (private config
v2 ``agents``, AGENTS.md/API-CONTRACT.md); this module only reads *this* agent's token
from wherever the operator put it, so it can be sent as ``Authorization: Bearer
<token>``. It never prints or logs the value, and callers must not either — pass it
straight to ``context.fetch_context`` / ``submit.submit_report`` and nowhere else.

Two sources, file preferred over environment (the app's own ``.env`` precedence,
AGENTS.md: "present .env takes precedence and fails closed if invalid"):

* ``RESEARCH_AGENT_TOKEN_FILE``: a path to a file holding just the token, outside the
  repository, as the app's own runtime credentials are kept outside it. It must be a regular
  file owned by this user and readable by nobody else (mode 0600, as
  ``scripts/cloud_secrets.py`` writes it); anything else is refused, never read. A stray
  trailing newline is stripped.
* ``RESEARCH_AGENT_TOKEN``: the token itself, for a session that already has it in a
  process environment it controls.

Neither is committed, and this module reads them at call time only — nothing is cached
at import time, so a missing token fails when it is actually needed, not on import.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

TOKEN_FILE_ENV = "RESEARCH_AGENT_TOKEN_FILE"
TOKEN_ENV = "RESEARCH_AGENT_TOKEN"


class TokenUnavailable(Exception):
    """No usable token was configured. The message never contains a token value."""


def load_token(*, env=None):
    """The agent's bearer token, file first, then the environment variable.

    ``env`` is an injectable mapping (tests use a plain ``dict``; real runs omit it and
    read ``os.environ``). Raises ``TokenUnavailable`` with a code-like, secret-free
    message when neither source yields a non-empty token.
    """
    env = os.environ if env is None else env
    path = env.get(TOKEN_FILE_ENV)
    if path:
        try:
            fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise TokenUnavailable(
                f"RESEARCH_AGENT_TOKEN_FILE_UNREADABLE: could not read {TOKEN_FILE_ENV}"
            ) from None
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            os.close(fd)
            raise TokenUnavailable(
                f"RESEARCH_AGENT_TOKEN_FILE_NOT_PRIVATE: {TOKEN_FILE_ENV} must be a regular "
                "file of this user with mode 0600 (chmod 600 <file>)"
            )
        with os.fdopen(fd, "rb") as stream:
            try:
                text = stream.read(65536).decode("utf-8")
            except (OSError, UnicodeDecodeError):
                raise TokenUnavailable(
                    f"RESEARCH_AGENT_TOKEN_FILE_UNREADABLE: could not read {TOKEN_FILE_ENV}"
                ) from None
        token = text.strip()
        if not token:
            raise TokenUnavailable(f"RESEARCH_AGENT_TOKEN_FILE_EMPTY: {TOKEN_FILE_ENV} is blank")
        return token
    value = env.get(TOKEN_ENV)
    if value and value.strip():
        return value.strip()
    raise TokenUnavailable(
        f"RESEARCH_AGENT_TOKEN_MISSING: set {TOKEN_FILE_ENV} (preferred, a path to a "
        f"mode-0600 file holding the token) or {TOKEN_ENV} (the token itself)"
    )
