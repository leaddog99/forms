# Warranty claim — submission text (MARLEY_SVR / HP Envy TE01-4xxx)

Ready-to-paste text for the Best Buy service request, and the same evidence reframed for
the Intel claim that follows it. Technical backing and the full crash history live in
[host-stability-and-watchdog.md](host-stability-and-watchdog.md).

**Be straight about the coverage dates.** Best Buy Protection lapsed 2025-11-21 and the
documented crash history starts 2026-04-14. Saying so up front costs nothing — they will
see the dates anyway — and everything else in the submission is verifiable, which is
what makes the argument worth reading. Do not imply an in-window failure.

---

## The facts (verified — reuse in every submission)

| | |
|---|---|
| Order | `BBY01-806817898714` — purchased **2023-11-21**, picked up 11-22, Framingham MA |
| Product | HP Envy Desktop, Model **`7H0U6AA#ABA`**, Best Buy SKU `6532244`, $749.99 |
| Serial | **`2MO334319K`** |
| Processor | **Intel Core i7-13700** (13th Gen "Raptor Lake", locked non-K) |
| BIOS | HP **F.45** (2025-10-14), Intel microcode **0x12F** |
| Protection | Monthly Best Buy Protection, up to 24 mo., SKU `6420466` — lapsed **2025-11-21** |
| Membership | My Best Buy Total, ~$200/yr, **active and continuously paid since purchase** |

**Fault:** 14 unexpected shutdowns since 2026-04-14, now roughly every 5 days, always at
idle. Windows recorded bugcheck **`0x101 CLOCK_WATCHDOG_TIMEOUT`** (a CPU core ceasing to
respond to interrupts) on **2026-06-12** and **2026-07-29**. On 2026-07-29 the machine
crashed at 01:28 and could not restart itself — it stayed dead ~12 hours until powered on
by hand.

**The decisive point:** HP BIOS was updated F.40 → F.45 on **2026-06-23** specifically to
apply Intel's latest microcode (0x12F) for this defect. **Four more crashes followed**
(6/25, 7/10, 7/24, 7/29). Intel's own guidance is that the microcode halts *further*
degradation but cannot reverse damage already done — so a chip that still fails after it
is physically degraded and not fixable in software.

**No overclocking is possible:** locked non-K processor, locked OEM prebuilt, stock HP
firmware throughout.

---

## 1. Best Buy — short form (fits a web service-request box)

> Order BBY01-806817898714 (purchased 11/21/2023) — HP Envy Desktop, Model 7H0U6AA#ABA,
> serial 2MO334319K.
>
> The Intel Core i7-13700 in this PC is failing from the manufacturer-acknowledged "Vmin
> Shift Instability" defect affecting Intel 13th/14th Gen desktop processors — the defect
> Intel extended its CPU warranty from 3 to 5 years to cover.
>
> The machine has had 14 unexpected shutdowns since April 2026, now about every 5 days,
> always while idle. Windows recorded bugcheck 0x101 CLOCK_WATCHDOG_TIMEOUT — a CPU core
> ceasing to respond — on 6/12/2026 and 7/29/2026. On 7/29 it crashed at 1:28 AM and
> could not restart itself; it sat dead for 12 hours.
>
> I applied HP's BIOS F.45 on 6/23/2026, which carries Intel's latest 0x12F microcode fix
> for this exact defect. It did not help — four more crashes followed. Per Intel's own
> guidance that means the processor has already physically degraded and cannot be fixed
> in software. The PC is a locked non-K OEM system on stock firmware, so there is no
> overclocking involved.
>
> I understand my Best Buy Protection (up to 24 months) lapsed on 11/21/2025, and my
> documented crash log begins April 2026. I raise it because this is a progressive latent
> defect — the degradation accumulates over the life of the chip and only becomes
> symptomatic once it crosses a threshold, so the damage was building during the covered
> period. I have been a continuously paying My Best Buy Total member since the purchase.
>
> I'd like to know what options are available — service, goodwill, or trade-in credit.
> I can provide the full Windows event log history on request.

## 2. Best Buy — long form (email / chat / escalation)

> **Re: Order BBY01-806817898714 — HP Envy Desktop, serial 2MO334319K**
>
> I'm writing about a hardware failure on a PC I bought from your Framingham, MA store on
> November 21, 2023. I've been a My Best Buy Total member continuously since then.
>
> **The failure.** The system shuts down without warning, always while idle, and has done
> so 14 times since April 2026 — currently about every five days. On July 29 it crashed at
> 1:28 AM and could not restart itself; it sat dead until I powered it on by hand twelve
> hours later. Windows recorded the crash as bugcheck 0x101 CLOCK_WATCHDOG_TIMEOUT, which
> means a processor core stopped responding to interrupts.
>
> **The cause is a known manufacturing defect.** This machine uses an Intel Core i7-13700,
> a 13th Gen "Raptor Lake" processor. Intel has publicly acknowledged that these
> processors suffer from "Vmin Shift Instability" — progressive physical degradation that
> causes exactly this failure pattern, including crashes at idle and light load. Intel
> extended the warranty on affected processors from three years to five specifically
> because of it.
>
> **I already tried the manufacturer's fix.** On June 23, 2026 I updated the BIOS to HP's
> F.45, which delivers Intel's latest microcode revision (0x12F) for this defect. Four
> more crashes followed — June 25, July 10, July 24 and July 29. Intel's own guidance is
> that this microcode halts further degradation but cannot reverse damage already done, so
> a processor that continues to fail afterward has already degraded physically and cannot
> be repaired in software.
>
> **This is not user-induced.** The i7-13700 is a locked, non-overclockable processor in a
> locked OEM system running stock HP firmware. There is no way for me to have overclocked
> or over-volted it. Recorded CPU temperatures have been normal throughout (max 64 °C).
>
> **On coverage.** I'm aware the Monthly Best Buy Protection on this order ran up to 24
> months and lapsed on November 21, 2025, and that my documented crash history begins in
> April 2026. I'm raising it anyway because of how this particular defect works: the
> degradation is cumulative across the life of the chip and only produces visible symptoms
> once it passes a threshold. The damage was accumulating throughout the covered period —
> what changed afterward was only that it became severe enough to crash the machine. This
> is a latent manufacturing defect, not wear or misuse.
>
> I've paid roughly $200 a year for My Best Buy Total continuously since this purchase and
> I'd like to keep the relationship. I'm asking what you can do — a service evaluation,
> goodwill assistance, or trade-in credit toward a replacement would all be welcome.
>
> I can supply the complete Windows System event log, the bugcheck records, and the BIOS
> update history. Please let me know what would be useful.
>
> John Landry

## 3. Phone / in-store — say it in 30 seconds

> "I bought an HP Envy desktop here in November 2023, order BBY01-806817898714. The Intel
> processor in it has the known 13th-generation defect — the one Intel extended warranties
> to five years over. It crashes every few days now. I already installed the BIOS update
> with Intel's fix and it didn't help, which per Intel means the chip is physically
> degraded. I know my protection ran out in November 2025. I've been a Total member paying
> $200 a year the whole time and I'd like to know what options you have for me."

Then stop talking and let them respond. If the first person can't help, ask politely for a
supervisor — front-line staff usually have no discretion past the expiry date.

---

## 4. Evidence to have ready

- **This order page** (screenshot already saved) — purchase date, order number, serial.
- **Crash history** — run `kernel_power_check.bat` in the project root; it lists every
  Kernel-Power Event 41 and tags each one pre- or post-BIOS-flash.
- **The two bugchecks** — export from Windows Event Viewer, System log, Event ID 1001,
  dated 2026-06-12 and 2026-07-29, both `0x101`.
- **BIOS version** — F.45, visible in `msinfo32`.
- **MemTest86 pass** if you run one (see the RAM note in the main doc — the machine has
  64 GB aftermarket against 16 GB as sold, and that will be raised as an alternative
  cause if you don't get ahead of it).

## 5. Expectations

Best Buy is a **long shot** — the protection is eight months expired and there is no
in-window failure to point to. It costs one submission and it's worth trying, because the
membership relationship is real and goodwill discretion does exist. But treat any outcome
as upside, not the plan.

**The plan is Intel** (§7 of the main doc), open until **2028-11-21**. Everything in §1–§4
above transfers directly; only the ask changes — Intel replaces the processor rather than
servicing the system. Route it HP first, get the refusal in writing, then escalate to
Intel Customer Support citing their published remediation clause for customers
"unsuccessful in prior RMAs."

**A Best Buy denial is not wasted.** It is a second documented refusal to attach to the
Intel escalation.
