---
name: "#usafa-chamber / ACM2202 calibration"
dateCreated: 2026-08-25
dateModified: 2026-08-25
container: cdocker
---
# Calibrating from the dashboard — design note

The chamber's VNA is a Copper Mountain A2202-Fx and the automatic calibration
module on hand is CMT's **ACM2202** — a USB-powered, USB-controlled electronic
2-port cal kit. The question is what "run a calibration from the dashboard"
should mean.

Written with **no hardware in reach**. Everything below marked *unverified* is a
reading of CMT's published command index, not something this project has seen
answer. The point of the design is that Wednesday's bench session reduces to
*run the probe, correct one dataclass* — not *write the feature*.

# What is already true

`SENS<ch>:CORR:STAT?` is now queried on both entry points, reported before the
tower turns, and written into every run's `meta.json` as `correction_state`. So
the system can already say **whether** a run was calibrated. It cannot yet say
**what** calibration, **when**, or **at what sweep** — and it cannot produce one.

# The shape of the problem

A calibration is not a software operation with a hardware side effect. It is a
**physical procedure with a person in the middle of it**.

To calibrate the chamber's measurement plane, someone has to walk into the
chamber, unmate the transmit horn and the AUT from the cable ends, mate the ACM
across those two ends, walk back out, and shut the door. Only then can the
sweep run. Then it all has to be undone.

That single fact drives most of what follows:

- **A calibration cannot be a button that just goes.** It is a wizard with an
  explicit *the module is connected* confirmation, and an explicit *put the
  antennas back* step at the end. Phaser's `#calibration-modal` is the right
  furniture; the content is ours.
- **The reference plane is a decision, not a detail.** Cal at the instrument
  front panel leaves the chamber cables inside the DUT — every dB of cable loss
  and every ripple from a bad connector lands in the pattern. Cal at the cable
  ends inside the chamber removes them, which is the whole point. The wizard
  should say which plane it believes it is calibrating, and record it, because
  nothing in the data distinguishes the two afterwards.
- **The tower must not move during a cal.** Not because the cal cares, but
  because a cable being wound up while somebody has their hands on the
  connectors is the one failure mode here with a person in it.

# Exclusivity, and the honest abort

Calibration is a second worker competing for the same single-threaded, stateful
instruments. It gets the same treatment as a scan: one worker, `_require_idle()`
in both directions — no cal while scanning, no scan while calibrating.

The abort story is **not** the same as a scan's, and pretending otherwise would
be the worst thing in this design.

A scan is a loop: the worker checks `self._cancel` between angles, so cancelling
lands within about 100 ms. An AutoCal is **one SCPI command that blocks for as
long as it takes** — `SENS<ch>:CORR:COLL:ECAL:SOLT2 1,2` runs the whole
short/open/load/thru sequence inside the instrument. There is no place to poll.
While it is in flight, the cancel flag can be set but nothing can act on it, and
closing the socket underneath a running calibration is how an instrument gets
left in a half-collected state.

So:

> **Cancel means "stop after this step".** The button says so. The worker sets
> the flag, lets the in-flight command return or time out, and then clears the
> collection buffer so nothing half-collected is left applied.

Two supporting requirements:

1. **A long, explicit VISA timeout for the cal call only**, restored afterwards.
   The default 120 s is generous for a sweep and tight for a 22 GHz two-port
   cal; a timeout mid-cal is exactly the state we do not want to reach.
2. **`SENS<ch>:CORR:COLL:CLE` on every exit path** — success, failure, cancel.
   The same shape as the axis-stop invariant in `finally`, and for the same
   reason: the failure that matters is the one that leaves hardware in a state
   nobody is watching.

# Cal and sweep are one object

Configuring the sweep is what invalidates a calibration. That is already why
`_run_scan` reads `CORR:STAT?` *after* `configure()` rather than before.

It follows that a calibration is only meaningful **paired with the sweep it was
taken at**. A cal at 2–3 GHz / 101 points says nothing useful about a run at
5–6 GHz, and a VNA quietly interpolating across a changed span is a plausible-
looking answer of unknown quality — the same bug class as the split position
reply.

The design: the cal wizard **configures the sweep from the current form values
first**, then calibrates, then records those values. After that the dashboard
can answer two questions it cannot answer today:

- *Is the calibration stale?* — age against `taken_at`.
- *Does the calibration match this run?* — compare the form's span, points, IF
  bandwidth and power against `cal.json`, and say so in the banner if they
  differ. Not a block. A statement.

# What gets recorded

`runs/../meta.json` gains nothing new; the join is by reference. A single
`calibration/cal.json` at the runs root holds the last calibration:

```json
{
  "taken_at": "2026-08-27 14:02:11",
  "method": "ecal_solt2",
  "ports": [1, 2],
  "reference_plane": "cable ends inside the chamber",
  "module": "Copper Mountain Technologies,ACM2202,...",
  "sweep": {"start_hz": 2e9, "stop_hz": 3e9, "points": 101,
            "if_bw_hz": 1000.0, "power_dbm": 0.0},
  "correction_state": "1"
}
```

and each run's `meta.json` gains `"calibration": {...that record...}` copied in
at scan time. Copied, not referenced: a run that points at a file which later
gets overwritten by the next calibration is a run that lies about itself.

# The unverified part

Everything above is design. This is the part that needs the bench.

CMT documents AutoCal under the **ECal** mnemonics rather than anything named
`ACM`, which is worth knowing before searching the manual for the wrong word.
From the published command index, the relevant set appears to be:

| Command | Read as |
|---|---|
| `SENS<ch>:CORR:COLL:ECAL:SOLT2 <p1>,<p2>` | full 2-port AutoCal — *the one we want* |
| `SENS<ch>:CORR:COLL:ECAL:SOLT1 <p>` | 1-port AutoCal |
| `SENS<ch>:CORR:COLL:ECAL:CCH` | confidence check |
| `SENS<ch>:CORR:COLL:ECAL:ORI:EXEC` | orient module to ports |
| `SENS<ch>:CORR:COLL:ECAL:UTHR:STAT` | unknown-thru on/off |
| `SYST:COMM:ECAL:DATA?` | module characterization data |
| `SENS<ch>:CORR:COLL:CLE` | clear the collection buffer |
| `SENS<ch>:CORR:STAT?` | correction applied — *this one is verified* |

**None of these have been seen to answer on our firmware (26.3.1).** They are
centralized in one `AcmCmds` dataclass beside `PositionerCmds`, which exists for
exactly this reason: the last time this project met an instrument, four of the
mnemonics that "obviously" existed returned `ERROR 1`, and having them in one
place is what made that a ten-minute correction instead of a rewrite.

There is a second unknown behind the first. The ACM is a **USB device on the PC
running S2VNA**, not on the VNA. Whether S2VNA exposes it to a SCPI client at
all, or whether AutoCal is a GUI-only path on this software version, is the
single question that decides whether this feature exists. That is what stage 7
of `bringup.py` is for, and it asks read-only.

# Bring-up stage 7 — read-only, ordered by what it rules out

1. `SYST:ERR?` clean to start from a known state.
2. `SYST:COMM:ECAL:DATA?` — does the software see a module at all?
3. `SENS1:CORR:COLL:ECAL:SOLT2?` and friends as **query forms**, purely to see
   whether the header parses. A `-113 Undefined header` is a definite answer; a
   `-110`-then-silence is the `SENS:SWE:TIME?` failure mode again and means the
   short-timeout treatment.
4. `SENS1:CORR:STAT?` before and after, to confirm nothing was disturbed.

It never runs a calibration. Running one needs the module mated, and a probe
that quietly recalibrates the instrument it was asked to inspect is a probe
nobody trusts twice.

# What is built ahead of the bench

- `AcmCmds` + `Vna.acm_*` — one place to correct.
- `FakeVna` ACM support, with faults: module absent, cal fails mid-way, cal
  slow enough to exercise the cancel path.
- The service cal worker, `cal.json`, and the state it publishes.
- The modal, the banner, and the staleness/mismatch reading.
- `rigcheck` scenarios: happy path; module absent; cancel leaves no collection
  state; cal refused while scanning; scan refused while calibrating.

If stage 7 says S2VNA will not expose AutoCal to SCPI, all of that is still
worth having — the modal becomes a **guided manual procedure** that walks the
operator through the front-panel cal and records the same `cal.json` from what
the instrument reports afterwards. The recording is most of the value either
way; the automation is the convenience.

# Open questions

- [ ] Does S2VNA 26.3.1 expose AutoCal over SCPI, or is it GUI-only?
- [ ] Which reference plane does the chamber actually want calibrated — cable
      ends inside the chamber, or the instrument front panel? (Almost certainly
      the former, but it should be written down once rather than assumed.)
- [ ] Does the ACM2202 need `ORI:EXEC` first, or does `SOLT2` orient itself?
- [ ] How long does a 2-port cal take at the sweep settings actually used? That
      number sets the VISA timeout and the progress estimate.
- [ ] Is a confidence check worth wiring, or is it a bench-only nicety?
- [ ] Cable ends inside the chamber means the ACM needs USB *into* the chamber.
      Is there a feedthrough, or does the module have to be reachable from
      outside?
