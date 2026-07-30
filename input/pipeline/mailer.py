"""mailer.py — the ONE place this app sends email from.

Every message leaves through `send_mail()`. That is deliberate, and it is the
same shape as `serp_search()`: when the SERP vendor changed (SerpApi → Scale
SERP) it was a config edit at one chokepoint rather than a hunt through call
sites, and mail vendors get swapped for exactly the same reasons — price,
deliverability, an account problem on a Sunday.

TWO STREAMS, TWO CREDENTIALS
----------------------------
`stream='transactional'` — verification, password reset, anything a person is
sitting there waiting for. Sent immediately.

`stream='bulk'` — digests, newsletters, follow alerts. Sent from the jobs
runner so it is logged, cancellable and rate-limited.

They authenticate as DIFFERENT SMTP users on purpose. Bulk mail attracts spam
complaints; transactional mail must still arrive when it does. Separate
credentials are what let the provider (and eventually a separate sending
subdomain) keep those reputations apart. A password-reset that lands in spam
because a recipe digest annoyed people is an outage nobody thinks to look for.

BLOCKING
--------
Everything here is synchronous and talks to a remote server over TLS. An SMTP
handshake is far slower than the ~420ms Whisper transcribe that used to freeze
all 173 endpoints from `/cook/listen`. **Callers in the request path MUST use
`run_in_threadpool`.** Bulk callers are already in a job subprocess and can call
straight through.

CONFIG SPLIT
------------
`.env`  — host, port, and the two username/password pairs. Credentials.
`system_config` (category "Mail") — from-addresses, display name, the kill
switch, the daily cap. Not secrets, per-instance business config, and the
curator's to edit in the admin UI (memory/project_portable_package).
"""
from __future__ import annotations

import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Optional

try:                                          # optional: the DB may not be up yet
    from input.pipeline.system_config import get_setting
except Exception:                             # pragma: no cover - bootstrap path
    def get_setting(key, default=None, **kw):  # type: ignore
        return default

TRANSACTIONAL = "transactional"
BULK = "bulk"

# Deliberately permissive: a syntax gate, not a deliverability oracle. The only
# addresses that truly validate are the ones that accept mail. `email-validator`
# goes here if we ever need unicode/IDN normalisation.
_ADDR_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class MailNotConfigured(RuntimeError):
    """Raised only for programmer error (an unknown stream). Ordinary send
    failures come back as a result dict — a caller mid-signup needs to tell the
    user something, not catch an exception."""


def _creds(stream: str) -> tuple[str, str]:
    if stream == TRANSACTIONAL:
        return (os.environ.get("SMTP_TX_USER", "").strip(),
                os.environ.get("SMTP_TX_PASS", "").strip())
    if stream == BULK:
        return (os.environ.get("SMTP_BULK_USER", "").strip(),
                os.environ.get("SMTP_BULK_PASS", "").strip())
    raise MailNotConfigured(f"unknown mail stream {stream!r}")


def _from_address(stream: str) -> str:
    key = "mail_from_bulk" if stream == BULK else "mail_from_transactional"
    return str(get_setting(key, "") or "").strip()


def is_configured(stream: str = TRANSACTIONAL) -> bool:
    """True when this stream could actually send. Cheap enough to gate a UI on."""
    user, password = _creds(stream)
    return bool(os.environ.get("SMTP_HOST") and user and password
                and _from_address(stream))


def valid_address(addr: Optional[str]) -> bool:
    return bool(addr and _ADDR_RE.match(addr.strip()))


def send_mail(
    to: str,
    subject: str,
    body: str,
    *,
    html: Optional[str] = None,
    stream: str = TRANSACTIONAL,
    reply_to: Optional[str] = None,
    unsubscribe_url: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Send one message. Returns {ok, error, message_id, stream} — never raises
    for a delivery failure.

    `unsubscribe_url` is REQUIRED for bulk in practice: Gmail and Yahoo demand
    one-click unsubscribe from bulk senders, and mail without it gets filtered
    however clean the DNS is. Transactional mail must NOT carry it — an
    unsubscribe link on a password reset is how someone opts out of being able
    to log in.
    """
    result = {"ok": False, "error": None, "message_id": None, "stream": stream}

    if not get_setting("mail_enabled", True):
        result["error"] = "Mail is disabled in system settings (mail_enabled)."
        return result
    if not valid_address(to):
        result["error"] = f"Not a valid address: {to!r}"
        return result

    user, password = _creds(stream)
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "465") or 465)
    sender = _from_address(stream)
    if not (host and user and password):
        result["error"] = f"SMTP credentials missing for the {stream} stream (.env)."
        return result
    if not valid_address(sender):
        key = "mail_from_bulk" if stream == BULK else "mail_from_transactional"
        result["error"] = f"No valid from-address configured ({key})."
        return result

    display = str(get_setting("mail_from_name", "") or "").strip()
    domain = sender.split("@", 1)[1]

    msg = EmailMessage()
    msg["From"] = f"{display} <{sender}>" if display else sender
    msg["To"] = to.strip()
    msg["Subject"] = subject
    # UTC with an explicit offset, like every other timestamp in this system.
    msg["Date"] = formatdate(usegmt=True)
    # Message-ID on OUR domain, not the relay's — it is what threads replies and
    # what a bounce refers back to.
    msg["Message-ID"] = make_msgid(domain=domain)
    if reply_to:
        msg["Reply-To"] = reply_to
    if unsubscribe_url and stream == BULK:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        if port in (465, 8465, 443):
            # Implicit TLS — encrypted from the first byte, no STARTTLS window.
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.ehlo()
                s.login(user, password)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        # The most likely first-run failure, and the least obvious: SMTP2GO's
        # SMTP *username* is not necessarily the label shown in its dashboard.
        result["error"] = f"SMTP auth rejected for the {stream} user: {e}"
        return result
    except Exception as e:                                    # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        return result

    result["ok"] = True
    result["message_id"] = msg["Message-ID"]
    print(f"[MAIL] sent {stream} -> {to} ({subject!r}) id={msg['Message-ID']}")
    return result


if __name__ == "__main__":                                    # test send
    import argparse
    from dotenv import load_dotenv
    from pathlib import Path

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    ap = argparse.ArgumentParser(description="Send one test message.")
    ap.add_argument("--to", required=True)
    ap.add_argument("--stream", default=TRANSACTIONAL, choices=[TRANSACTIONAL, BULK])
    ap.add_argument("--subject", default="BCC mail test")
    a = ap.parse_args()
    print(f"configured({a.stream}):", is_configured(a.stream))
    r = send_mail(a.to, a.subject,
                  "If you are reading this, BCC can send mail.\n\n"
                  "Sent through SMTP2GO from the BCC host.\n",
                  stream=a.stream)
    print(r)
