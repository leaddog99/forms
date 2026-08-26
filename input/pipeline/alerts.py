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


def alert_curator(subject: str, body: str) -> None:
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
        mailer.send_mail(to, f"[BCC alert] {subject}", body)
        print(f"!! alert emailed to {to}")
    except Exception as e:  # noqa: BLE001 — alerting must never break the run
        print(f"!! alert email failed ({type(e).__name__}: {e}) — banner stands")
