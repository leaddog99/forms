# Email — the whole setup, and how it was proven

Everything BCC needs to send mail: the account, the DNS, the code, the traps we hit, and
the commands to re-verify any of it. Written the day it was built (2026-07-30), from the
real values and the real results.

If you are self-hosting BCC, sections 1–3 are your runbook. Sections 4–6 are what the code
does. Section 7 is the list of things that looked right and were not — read it before
debugging anything.

---

## 1. The account

**SMTP2GO**, 175,000 messages/month on the current plan.

One vendor for everything, deliberately. SES is roughly an order of magnitude cheaper per
message and Postmark has a better transactional reputation, but the capacity here is
already paid for and dwarfs what this app sends — a second vendor would be a second bill
and a second integration to buy something we already own. Revisit only if 175k/mo is
actually reached.

**Two SMTP users, not one.** Create them under *Settings → SMTP Users*:

| User | Purpose |
|---|---|
| `bcc-transactional` | verification, password reset — anything a person is waiting for |
| `bcc-bulk` | digests, newsletters, follow alerts |

The separation is the point. Bulk mail attracts spam complaints; transactional mail has to
arrive anyway. Separate credentials are what let the provider keep those reputations apart,
and they are the seam along which a separate sending subdomain gets added later. A password
reset that lands in spam because a recipe digest annoyed people is an outage nobody thinks
to look for.

**Connection:** `mail.smtp2go.com`, port **465** (implicit SSL — encrypted from the first
byte, no STARTTLS downgrade window). Alternatives if 465 is ever blocked: 587 / 2525 /
8025 with STARTTLS, 8465 / 443 with SSL. This host is on Verizon Business with static IPs
and blocks nothing, so the non-standard ports are not needed.

**Sender domain** must be added under *Sending → Verified Senders* and show verified, or
every send fails with `550 From header sender domain not verified`. We verified the **root**
`bestcooksclub.com`.

---

## 2. DNS (Cloudflare, zone `bestcooksclub.com`)

### SMTP2GO's own records — CNAMEs, all **DNS only** (grey cloud, never proxied)

| Name | Content | Purpose |
|---|---|---|
| `em112864` | `return.smtp2go.net` | return path / bounce domain — also how SPF aligns |
| `s112864._domainkey` | `dkim.smtp2go.net` | DKIM signing key |
| `link` | `track.smtp2go.net` | click tracking (rewrites URLs — see §7) |

Orange-clouding any of these breaks all of it: Cloudflare would answer with its own IPs
instead of the CNAME target. This is the single most common way the setup fails.

### The two records we had to write by hand

**`_dmarc` — TXT**
```
v=DMARC1; p=none; rua=mailto:dmarc@bestcooksclub.com
```

**`@` (root) — TXT — the single SPF record**
```
v=spf1 include:spf.smtp2go.com include:_spf.mx.cloudflare.net ~all
```

Notes that cost time:

- The include is `spf.smtp2go.com` — **`.com`, not `.net`**. Read it out of
  `return.smtp2go.net`'s own SPF record rather than trusting documentation or memory.
- **A domain may have exactly one SPF record.** Two `v=spf1` TXT records is not "both work",
  it is a permanent error that breaks SPF for every sender. Adding a sender means editing
  *this* record, never publishing a second one.
- `~all` (softfail) is deliberate while we learn what else sends as us. Tighten to `-all`
  after the DMARC reports come back clean.
- Both includes together are **2 of the 10 permitted DNS lookups**, so there is headroom.

### Cloudflare Email Routing (inbound — how `dmarc@` reaches a human)

MX records `route1/2/3.mx.cloudflare.net` plus its SPF include (already merged above).
Routing rules: a catch-all and an explicit `dmarc@bestcooksclub.com`, both to
`john@johnlandry.com`.

`p=none` does nothing on its own — the aggregate reports *are* the point, and without a
working `rua` you are publishing a policy that observes nothing and gives you no basis for
ever tightening it.

---

## 3. `.env` and `system_config`

The split follows the rule set with the affiliate codes: **secrets in `.env`, per-instance
business config in the system record.**

`.env` — credentials:
```
SMTP_HOST=mail.smtp2go.com
SMTP_PORT=465
SMTP_TX_USER=
SMTP_TX_PASS=
SMTP_BULK_USER=
SMTP_BULK_PASS=
```

`system_config`, category **Mail** (editable in the System admin page):

| Key | Default | Notes |
|---|---|---|
| `mail_enabled` | `true` | master kill switch — first thing to flip if something loops |
| `mail_from_transactional` | `noreply@bestcooksclub.com` | must be at a verified domain |
| `mail_from_bulk` | `digest@bestcooksclub.com` | kept separate from transactional |
| `mail_from_name` | `Best Cooks Club` | display name beside the address |
| `mail_daily_cap` | `5000` | runaway guard, not a quota |
| `customer_base_url` | `https://bestcooksclub.com` | **not** `public_base_url` — see §5 |

> A leaked SMTP credential is worse than a typical key leak: it sends as your domain with
> **valid DKIM**, so a phishing message passes every check your real mail passes. Use
> generated passwords, different per user. Note `backup_db.py` copies `.env` to ADAM in
> plaintext by design, so they land there too.

---

## 4. The code

**`input/pipeline/mailer.py`** — `send_mail()` is the only place mail leaves this app. Same
shape as `serp_search()`: when the SERP vendor changed it was a config edit at one
chokepoint rather than a hunt through call sites, and mail vendors get swapped for the same
reasons.

```python
send_mail(to, subject, body, *, html=None, stream=TRANSACTIONAL,
          reply_to=None, unsubscribe_url=None, timeout=30) -> dict
```

- Returns `{ok, error, message_id, stream}` and **never raises** on a delivery failure — a
  caller mid-signup has to tell the user something, not catch an exception.
- `Message-ID` is minted on **our** domain, not the relay's: it threads replies and is what
  a bounce refers back to.
- `List-Unsubscribe` + `List-Unsubscribe-Post` attach to **bulk only**. Gmail and Yahoo
  require one-click unsubscribe from bulk senders, and an unsubscribe link on a password
  reset is how someone opts out of being able to log in.
- **Blocking.** Everything here is synchronous TLS I/O. Request-path callers **must** use
  `run_in_threadpool` — an SMTP handshake is far slower than the ~420 ms Whisper transcribe
  that used to freeze all 173 endpoints from `/cook/listen`.

**`input/pipeline/mail_messages.py`** — the words, kept out of the endpoints. Copy changes
far more often than logic and a wording tweak should never risk the auth path. Plain text is
written first and always: it is what a screen reader, a text-only client and a spam filter
read, and a message whose HTML is its only content looks exactly like the ones trying to
hide something. No remote images, fonts or CSS — clients block them by default, so nothing
load-bearing may depend on loading.

Test send, any time:
```
python -m input.pipeline.mailer --to you@example.com --stream transactional
```

---

## 5. Email verification

`users.email_verified` / `email_verified_at`, auto-migrated. **Nothing enforces it yet** —
six real accounts predate mail entirely, and a flag day that locks them out of a public site
is worse than an unproven address. Record first, enforce later, one surface at a time.

**The token is not a session token, and that distinction is load-bearing.**
`mint_user_token` returns a 30-day credential; mailing one hands a logged-in account to
anyone who reads the mailbox, a forwarded copy, a proxy log or a `Referer` header. So
emailed links use `auth.mint_purpose_token(purpose, user_id, bound, ttl)`, signed over a
different payload that binds three things:

| Bound value | Why |
|---|---|
| `purpose` | a verify token cannot be replayed as a reset token |
| `user_id` | inside the signature, so it cannot be replayed as another account |
| `bound` | re-read from the DB at redemption — for verification, the current email |

Binding the address means a token minted for an old address stops working the moment the
address changes. For a password reset the bound value would be the current password hash,
which makes the link die as soon as the password is used or changed — no revocation table.

Endpoints:

| Path | Notes |
|---|---|
| `POST /auth/send-verification` | self or `manage_users`; per-IP throttled — an endpoint that sends mail is a way to use us to spam a third party |
| `GET /auth/verify?token=…` | returns a **page**, not JSON: it is clicked from a mail client. Malformed, expired and unknown-user all give the *same* message, so a stranger cannot learn whether a token was ever real |

`POST /auth/signup` sends the confirmation but **does not fail if mail is down** — the
account exists and the person is already signed in; a mail outage should cost them a
confirmation they can request again, not the account they just made. The response carries
`verification_sent` so the UI can be honest either way.

**Both paths are allowlisted in `host_gate.py`.** Anything appearing in customer email must
be, or the mail is fine and the link is dead on arrival.

---

## 6. Proven, with results

| Test | Result |
|---|---|
| transactional → Gmail | **inbox**, first-ever send from a cold domain |
| bulk → Gmail | **inbox**, `List-Unsubscribe` present |
| `dmarc@bestcooksclub.com` → Cloudflare Email Routing → Outlook | **inbox** |
| From header | `Best Cooks Club <noreply@bestcooksclub.com>` |

The Outlook result is the strongest signal. It arrived **forwarded**, which relays from
Cloudflare's IP rather than SMTP2GO's, so **SPF necessarily fails** at the destination.
Microsoft accepting it means it authenticated on the **DKIM** leg — confirming the DKIM
alignment that could not be read from a header, and confirming the setup survives forwarding,
which is exactly what breaks when a domain has SPF but no aligned DKIM.

Re-verify DNS at any time:
```bash
nslookup -type=TXT _dmarc.bestcooksclub.com 8.8.8.8
nslookup -type=TXT bestcooksclub.com 8.8.8.8              # exactly ONE v=spf1
nslookup -type=TXT s112864._domainkey.bestcooksclub.com 8.8.8.8
nslookup -type=MX  bestcooksclub.com 8.8.8.8
```

---

## 7. Traps — things that looked right and were not

**A DMARC record without `v=DMARC1;` is not a weak policy, it is no policy.** The record
here read exactly `"p=none"`. It showed as a present, correct-looking row in Cloudflare;
receivers discard the whole record. Every DMARC record must begin with the version tag.

**Pasting into Cloudflare's Content field eats edge characters.** Three records in this zone
have lost one: the original `_dmarc` lost its leading `v=DMARC1;`, then on re-entry DMARC
lost its leading `v` and SPF lost the final `l` of `~all` (`~al` is a syntax error, which
makes the record *worse* than absent). Cloudflare pre-fills the field on edit, so pasting
into a partial selection leaves fragments. **`Ctrl+A` first, then verify the first and last
characters after saving.**

**Cloudflare Email Routing needs its own SPF include.** Its rules can show **Active** while
the service itself is **Disabled** and DNS reads *Not configured* — mail to a routed address
then vanishes with no bounce anywhere obvious. Because only one SPF record is allowed, the
SMTP2GO-only record we published first occupied the slot and starved it. Both includes go in
one record.

**Do not accept Cloudflare's "add records automatically" for SPF.** It publishes its own
`v=spf1` record, giving the domain two — which breaks SPF for both senders.

**`public_base_url` is the *admin* host** (`recipes.tbotb.com`). A link built from it 404s
for the customer it was sent to, because `host_gate` serves the admin surface on one
hostname and the customer surface on the other. Customer email uses `customer_base_url`.

**A silent zero is the failure mode to design against.** A send that fails must say so at
every layer — the endpoint 502s, the UI prints the reason. "Sent" when nothing left the
building leaves someone waiting for a message that will never arrive.

---

## 8. Not built yet

- **Password reset** — same rails, `bound` = the current password hash.
- **Enforcement** — nothing currently requires a verified address. Needs a grandfathering
  decision for the accounts that predate mail.
- **Follow / subscription tables** for the alert side. Alerts ("a dish or publisher I follow
  has a new recipe") are event-driven and preference-filtered and must come from BCC, the
  only thing that knows what you follow. Newsletters and digests are curated campaigns and
  could go to **Listmonk** later — self-hosted, free, sends through this same SMTP2GO plan —
  rather than hand-rolling a list manager. One relay, two producers.
- **Bulk sending as a job**, so a 40k digest is logged, cancellable and rate-limited, and
  never queues ahead of a password reset.
- **Sending subdomains** (`mail.` / `news.`). The root domain is verified, so both streams
  currently share one reputation. Splitting is cheap now and expensive after 100k newsletters
  have built a complaint history the transactional stream would inherit.
- **Suppression handling** — respect bounces before enqueueing, or repeated sends to dead
  addresses will get the sender blocked.
- **Click-tracking check.** `link.bestcooksclub.com` rewrites URLs in outgoing mail. Confirm
  Amazon `tag` and `ascsubtag` survive it before the first digest, or newsletter sales pay
  nobody (see `memory/project_buy_links_revenue`).
