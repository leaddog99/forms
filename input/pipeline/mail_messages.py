"""mail_messages.py — the actual words we send, kept out of the endpoints.

Endpoints decide WHO gets mail and WHEN. This decides what it says. Separated
because copy changes far more often than logic, and because a wording tweak
should never risk the auth path.

Every message here is TRANSACTIONAL: a person asked for it and is waiting. None
of them carry List-Unsubscribe — see mailer.py for why that matters.

Each builder returns (subject, text, html). Plain text first and always: it is
what a screen reader, a text-only client and a spam filter read, and a message
whose HTML is its only content looks exactly like the ones that are trying to
hide something.
"""
from __future__ import annotations

from html import escape

try:
    from input.pipeline.system_config import get_setting
except Exception:                                  # pragma: no cover - bootstrap
    def get_setting(key, default=None, **kw):      # type: ignore
        return default


def _brand() -> str:
    return str(get_setting("mail_from_name", "Best Cooks Club") or "Best Cooks Club").strip()


def customer_url(path: str) -> str:
    """Absolute URL on the CUSTOMER host for a path in an email.

    Deliberately not `public_base_url` — that is the admin host. A verification
    link built from it would 404 for the person it was sent to, because the host
    gate serves the customer allowlist on the other hostname."""
    base = str(get_setting("customer_base_url", "") or "").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


# A single button, inlined. No external CSS, no images, no web fonts: remote
# assets are blocked by default in most clients, so anything load-bearing has to
# survive not loading. The plain-text part carries the same URL in full.
def _wrap(heading: str, body_html: str, button: tuple[str, str] | None = None) -> str:
    btn = ""
    if button:
        label, href = button
        btn = (
            f'<p style="margin:26px 0"><a href="{escape(href)}" '
            'style="background:#b8602a;color:#fff;text-decoration:none;padding:12px 22px;'
            'border-radius:8px;font-weight:600;display:inline-block">'
            f'{escape(label)}</a></p>'
            f'<p style="font-size:.85em;color:#6b5b4f;line-height:1.5">'
            'If the button does not work, paste this into your browser:<br>'
            f'<span style="word-break:break-all">{escape(href)}</span></p>'
        )
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'font-size:16px;line-height:1.55;color:#2a211b;max-width:34rem;margin:0 auto;padding:8px">'
        f'<h1 style="font-size:1.35rem;margin:0 0 14px">{escape(heading)}</h1>'
        f'{body_html}{btn}'
        '<hr style="border:none;border-top:1px solid #e6dccf;margin:28px 0 12px">'
        f'<p style="font-size:.8em;color:#8a7f72;margin:0">{escape(_brand())}</p>'
        '</div>'
    )


def verification(name: str, token: str) -> tuple[str, str, str]:
    """Confirm-your-address mail. The link is a GET because it is clicked from a
    mail client, which cannot POST."""
    brand = _brand()
    link = customer_url(f"/auth/verify?token={token}")
    first = (name or "").strip().split(" ")[0] or "there"

    subject = f"Confirm your email for {brand}"
    text = (
        f"Hi {first},\n\n"
        f"Confirm this address to finish setting up your {brand} account:\n\n"
        f"{link}\n\n"
        "The link works for 24 hours. If it expires, sign in and ask for a new one.\n\n"
        "If you didn't create this account, ignore this message — nothing was set up\n"
        "in your name, and the address will not be used again.\n"
    )
    html = _wrap(
        f"Confirm your email",
        f"<p>Hi {escape(first)},</p>"
        f"<p>Confirm this address to finish setting up your {escape(brand)} account.</p>"
        "<p style='font-size:.9em;color:#6b5b4f'>The link works for 24 hours. "
        "If you didn’t create this account, ignore this message — nothing was set up "
        "in your name.</p>",
        button=("Confirm my email", link),
    )
    return subject, text, html
