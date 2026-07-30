"""
pdu_poll — APC Switched Rack PDU polling library.

Public API:
    PDU_POLL_INIT()       — authenticate and open a session
    PDU_POLL_POWER(n)     — return real power of PDU n as a float (kW)
    PDU_POLL_FINALIZE()   — close the session and clear state
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import requests
from requests.exceptions import RequestException

# Site-specific. The PDU's address and read-only web credentials are supplied by
# the environment and ~/.secrets, the same convention the InfluxDB token and the
# BMC password already use -- a rack address does not belong in the source.
_BASE_URL = os.environ.get("PDU_URL", "")
_USERNAME = os.environ.get("PDU_USER", "readonly")
_PASSWORD_PATH = Path(
    os.environ.get("PDU_PASSWORD_FILE", Path.home() / ".secrets" / "pdu_password.txt")
)


def _password() -> str:
    try:
        return _PASSWORD_PATH.read_text().strip()
    except FileNotFoundError:
        raise RuntimeError(
            f"PDU password file not found at {_PASSWORD_PATH}. Create it with the "
            f"PDU's read-only web password, e.g.:\n"
            f"  mkdir -p {_PASSWORD_PATH.parent} && "
            f"echo '<password>' > {_PASSWORD_PATH} && chmod 600 {_PASSWORD_PATH}"
        ) from None

_POWER_RE = re.compile(
    r'class="dataName">\s*Power\s*</div>\s*<div class="dataValue">'
    r'\s*<span[^>]*>([\d.]+)\s*kW<',
    re.DOTALL,
)

# Module-level session state
_session: requests.Session | None = None
_auth_base: str | None = None


def PDU_POLL_INIT() -> None:
    """Authenticate with the PDU and open a persistent HTTP session."""
    global _session, _auth_base

    _session = requests.Session()
    _session.headers["User-Agent"] = "PDU-Poller/1.0"
    _auth_base = _login(_session)


def PDU_POLL_POWER(n: int) -> float:
    """
    Return the real power consumption of PDU n in kW.
    Raises RuntimeError if not initialised, the session has expired,
    or the value cannot be parsed from the page.
    """
    if _session is None or _auth_base is None:
        raise RuntimeError("Call PDU_POLL_INIT() before PDU_POLL_POWER()")

    url = f"{_auth_base}devstat.htm?pdu={n}"
    try:
        r = _session.get(url, timeout=10)
        r.raise_for_status()
    except RequestException as exc:
        raise RuntimeError(f"Network error fetching PDU {n}: {exc}") from exc

    if not _is_logged_in(r.text):
        raise RuntimeError("Session has expired — call PDU_POLL_INIT() again")

    m = _POWER_RE.search(r.text)
    if not m:
        raise RuntimeError(f"Could not parse Power value from devstat.htm?pdu={n}")

    return float(m.group(1))


def PDU_POLL_POWER_DEBUG(n: int) -> tuple[float, float]:
    """
    Like PDU_POLL_POWER(n) but also returns the HTTP round-trip time in ms.
    Returns (power_kw, elapsed_ms).
    """
    if _session is None or _auth_base is None:
        raise RuntimeError("Call PDU_POLL_INIT() before PDU_POLL_POWER_DEBUG()")

    url = f"{_auth_base}devstat.htm?pdu={n}"
    try:
        t0 = time.perf_counter()
        r = _session.get(url, timeout=10)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
    except RequestException as exc:
        raise RuntimeError(f"Network error fetching PDU {n}: {exc}") from exc

    if not _is_logged_in(r.text):
        raise RuntimeError("Session has expired — call PDU_POLL_INIT() again")

    m = _POWER_RE.search(r.text)
    if not m:
        raise RuntimeError(f"Could not parse Power value from devstat.htm?pdu={n}")

    return float(m.group(1)), elapsed_ms


def PDU_POLL_FINALIZE() -> None:
    """Close the HTTP session and reset module state."""
    global _session, _auth_base

    if _session is not None:
        _session.close()
    _session = None
    _auth_base = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _login(session: requests.Session) -> str:
    """Return the authenticated base URL (e.g. http://.../NMC/<token>/)."""
    if not _BASE_URL:
        raise RuntimeError("PDU_URL is unset -- point it at the PDU, e.g. "
                           "PDU_URL=http://10.0.0.5")
    r = session.get(_BASE_URL + "/", allow_redirects=True, timeout=10)
    r.raise_for_status()

    match = re.search(r'action="(/NMC/[^"]+/Forms/login1)"', r.text)
    if not match:
        raise RuntimeError("Could not locate login form action in logon page")

    login_action = match.group(1)
    payload = {
        "prefLanguage": "00000000",
        "login_username": _USERNAME,
        "login_password": _password(),
        "submit": "Log On",
    }
    r = session.post(_BASE_URL + login_action, data=payload,
                     allow_redirects=True, timeout=10)
    r.raise_for_status()

    auth_base = r.url.rsplit("/", 1)[0]
    if not auth_base.startswith(_BASE_URL + "/NMC/"):
        raise RuntimeError(f"Login failed; landed on: {r.url}")

    return auth_base.rstrip("/") + "/"


def _is_logged_in(html: str) -> bool:
    return "Log On" not in html or "logout.htm" in html
