"""Curator alerting — the loud channel for things a log line can't carry.

Born 2026-08-26: a SERP page exhausted its retries and quietly cost a run 5
candidates; the only witness was a mid-log line nobody reads in real time
("i don't see any message that says we lost 5 entries" — curator). This is
the one place that turns an operational loss into (a) an UNMISSABLE log
banner and (b) an email to the curator, best-effort, via the existing
transactional mailer.

Recipient comes from system_config `alert_email` (curator-editable; empty =
banner only, no mail). Always prints; never raises — an alert failure must
not break the run it is reporting on.
"""
from __future__ import annotations


from html import escape as _esc

# ---- HTML for the digests (curator 2026-11-24: "almost impossible to read...
# make the output html and columnar"). Inline styles and plain tables only: a
# mail client drops <style> blocks and most layout CSS. The text body stays as
# the log banner and as the fallback part of the message.
_TD = "padding:7px 12px 7px 0;border-bottom:1px solid #e5e7eb;vertical-align:top"
_TH = ("padding:7px 12px 7px 0;border-bottom:2px solid #9ca3af;text-align:left;"
       "font-size:12px;color:#4b5563;text-transform:uppercase;letter-spacing:.03em")


class Raw(str):
    """A table cell that is already HTML (a link, a pill). Everything else is escaped."""


def pill(text: str, color: str = "#b45309") -> Raw:
    return Raw(f'<span style="display:inline-block;padding:1px 7px;border-radius:9px;'
               f'background:{color};color:#fff;font-size:11px;white-space:nowrap">{_esc(text)}</span>')


def html_table(headers: list, rows: list, *, right: tuple = ()) -> str:
    """One table. `right` = indexes of columns to right-align (numbers)."""
    def cell(v, i, tag, style):
        al = ";text-align:right" if i in right else ""
        body = v if isinstance(v, Raw) else _esc("" if v is None else str(v))
        return f'<{tag} style="{style}{al}">{body}</{tag}>'
    head = "".join(cell(h, i, "th", _TH) for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(cell(v, i, "td", _TD) for i, v in enumerate(r)) + "</tr>"
                   for r in rows)
    return (f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
            f'width:100%;font-size:14px;line-height:1.4"><tr>{head}</tr>{body}</table>')


def html_page(title: str, intro: str, sections: list, footer: str = "") -> str:
    """`sections` = [(heading, note, table_html)]. intro/note/footer are plain text."""
    out = [f'<div style="font-family:Arial,Helvetica,sans-serif;color:#111827;max-width:960px">',
           f'<h2 style="margin:0 0 6px;font-size:19px">{_esc(title)}</h2>',
           f'<p style="margin:0 0 14px;font-size:14px;line-height:1.5;color:#374151">{_esc(intro)}</p>']
    for heading, note, table in sections:
        out.append(f'<h3 style="margin:22px 0 4px;font-size:15px">{_esc(heading)}</h3>')
        if note:
            out.append(f'<p style="margin:0 0 8px;font-size:13px;line-height:1.5;color:#4b5563">{_esc(note)}</p>')
        out.append(table)
    if footer:
        out.append(f'<p style="margin:20px 0 0;font-size:13px;line-height:1.5;color:#4b5563">{_esc(footer)}</p>')
    out.append("</div>")
    return "".join(out)


def alert_curator(subject: str, body: str, html: str | None = None) -> None:
    banner = "!" * 74
    print(f"\n{banner}\n!! ALERT: {subject}\n" +
          "\n".join("!! " + ln for ln in body.splitlines()) +
          f"\n{banner}\n")
    try:
        from input.pipeline.system_config import get_setting
        to = str(get_setting("alert_email", "") or "").strip()
        if not to:
            print("!! (no alert_email configured in System — banner only)")
            return
        from input.pipeline import mailer
        if not mailer.is_configured():
            print("!! (mailer not configured — banner only)")
            return
        mailer.send_mail(to, f"[BCC alert] {subject}", body, html=html)
        print(f"!! alert emailed to {to}")
    except Exception as e:  # noqa: BLE001 — alerting must never break the run
        print(f"!! alert email failed ({type(e).__name__}: {e}) — banner stands")
