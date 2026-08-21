---
name: "#usafa-chamber / instrument plane"
dateCreated: 2026-08-21
dateModified: 2026-08-21
container: cdocker
---
# Instrument plane — design note

**Nothing here is built.** This records a design conversation while the reasoning
is fresh, so the decisions that matter get made deliberately rather than
discovered halfway through an implementation. Treat the *Decisions* below as
provisional until something is on the bench.

The trigger: the chamber should be able to drive more than a VNA and a tower.
The available inventory is several **B205minis**, a couple of **X310s**, an
**AWG**, and a **wideband amplifier and PA** for generating large signals.
*(Confirm that inventory — the amp/PA line is second-hand.)*

# Roles, not devices

A pane per device falls over at about the fourth box. From the scan loop's point
of view everything in that inventory collapses into three roles:

| Role | Devices | Contract |
|---|---|---|
| **Source** | AWG, USRP TX, VNA port 1 | set frequency/level, RF on, RF off |
| **Receiver** | VNA, B205, X310 | configure sweep, capture → vector |
| **Positioner** | EMControl tower | seek, position, stop |

The PA is an **accessory on the source chain**, not a peer — it has no meaning
except as a stage between a source and an antenna, and its interlocks belong to
whatever owns the source.

`run_scan` currently hardcodes a VNA. The move is to give it a Receiver instead:
*go to this angle, hand back a vector and its metadata*. The function is short,
the seam is obvious, and `rigcheck.py` can then run the same scenarios against
whichever receiver is installed.

Adding a box should be a driver class plus a config entry, not edits in four
places. That settles an open UI question too: **panes get generated from a
device list the service publishes**, rather than hand-written. The data-driven
accordion stops being a nice-to-have.

# What each device is for

- **B205mini** — single channel, USB, very limited clocking. Good for scalar
  power patterns: cheap, plentiful, one per position. *Verify whether the units
  on hand expose any external reference; plan on no shared clock.*
- **X310** — external 10 MHz + PPS, two daughterboard slots. This is what gets
  used for anything phase-coherent or multi-channel. *Check whether both are
  populated with matching daughterboards; that decides whether the monitor
  channel below is available today or a purchase.*
- **AWG** — almost certainly SCPI over LAN, so it drops into the existing pyvisa
  path and `mock_instruments` fakes it with no new machinery. The cheapest thing
  on the list to integrate.
- **PA** — see *Safety*. *Does it have a control interface, or is it a bias line
  and a switch?*

# The ratio problem

What the VNA quietly provides is a **ratio**. S21 is referenced against its own
source, so source drift cancels and the measurement is meaningful without
calibration.

An AWG → PA → antenna → SDR chain has drift in every stage and nothing
cancelling it. The fix is a monitor path: a coupler on the source feeding a
second receive channel, and ratio the two. That is the strongest practical
argument for an X310 over a B205 for anything to be trusted — **the second
channel is the calibration.**

Patterns are normalized to peak, so relative shape survives an uncalibrated
chain reasonably well. Absolute gain does not, and needs a substitution
measurement against a known standard. `thru_run/` remains the reference for what
the chain's noise floor looks like: 0.019 dB peak-to-peak.

# Safety: the RF-off invariant

A PA in an anechoic chamber has consequences the positioner does not.

- A USRP RX front end damages around −15 dBm, X310 daughterboards well below.
  A keyed PA and a rotating tower is an efficient way to destroy receivers.
- Absorber damage and personnel exposure at real power.
- Sequencing: RF off before the tower moves, before the door opens.

This project already holds the analogous invariant for motion — STOP always
enabled, and (as of the driver-test work) the axis commanded to stop on **every**
exit path, not only `KeyboardInterrupt`. A PA wants exactly the same treatment:

> **RF off on every exit path, before the axis stop** — it is the faster hazard
> to remove.

With a `rigcheck` scenario holding it there, the way `drop_sweep` and
`service_stops` hold the axis stop. If one thing gets built before any of the
measurement work, make it this.

# The ZMQ instrument plane

GNU Radio and Python over ZMQ, with a small middleware layer, as the transport
for SDR control.

## Why it earns its place

- **Process isolation.** The UHD Python bindings ship with the driver install
  rather than as a plain pip dependency, which fights the `.venv-win` /
  OneDrive arrangement. A socket boundary makes that vanish: GNU Radio keeps its
  own interpreter, its own UHD, potentially its own machine.
- **GRC is the right editor for DSP.** Demod, filtering, sync, EVM. Rewriting
  that in numpy to avoid a dependency would be the wrong trade.
- **Architecturally consistent.** The service is already a socket control plane
  speaking JSON with request/response plus push frames. This is a second one,
  not a new paradigm.
- **It makes testing easier, not harder.** The fake is an *endpoint*: bind a
  socket, speak the same JSON, no UHD and no GNU Radio installed. `rigcheck` can
  cover the SDR path on any machine.

## Where not to use it

Plain power-vs-angle with a B205. `uhd.recv_num_samps()` in-process is
synchronous, simple, and has one fewer failure mode; a flowgraph and a socket
there is a layer for nothing.

**Two Receiver implementations behind one interface** — direct UHD for scalar
captures, GNU Radio over ZMQ when there is real DSP — and the measurement
decides which.

## Three constraints to design in from the start

**1. Split the socket types by guarantee.** The GNU Radio ZMQ blocks are best
documented as PUB/SUB, so that is what gets reached for, and it is wrong for
control: PUB/SUB is lossy and has the slow-joiner problem, where a subscriber
that connects late silently misses messages. Control belongs on REQ/REP or
DEALER/ROUTER; sample data can be PUSH/PULL or PUB/SUB where dropping is
acceptable. A control channel that silently drops a command is the last thing
wanted with a PA in the room.

**2. Gate the capture; do not free-run it.** This is the subtle one.

The scan loop is seek → settle → measure → record, synchronous, so a sample is
unambiguously attributed to an angle. A free-running flowgraph across a socket
destroys that guarantee: GNU Radio buffers are often 8k+ samples, plus ZMQ
queueing, so a burst "collected at 90°" can contain samples from while the tower
was still swinging toward 90°.

That is the same bug class as the split position reply — plausible output,
silently wrong — and the same class the motion-start grace period exists to
prevent. Make capture an explicit request: **flush, then capture N samples after
arrival, and answer when done.** Timestamping samples and correlating against
the position timeline is the alternative; it needs a shared clock and is much
more work for the same answer.

**3. A deadman on the transmit side.** In-process, a Python `finally` enforces
the RF-off invariant. Across a socket it does not — ZMQ peers fail silently and
a closed socket tells the far end nothing useful. The SDR side needs a
heartbeat: **control channel quiet for N seconds, stop transmitting.** Without
it, the dashboard worker dying leaves a PA keyed with nothing watching.

## Contract

Small, versioned, same JSON discipline as the existing service:

```
describe            -> what am I, what can I do, what are my limits
configure(params)   -> freq, rate, gain, antenna, waveform
capture(n)          -> flush, capture n samples after arrival, return vector
tx_on / tx_off      -> gated by the deadman
status              -> lock state, overflow counts, last error
```

Kept device-agnostic, this stops being a GNU Radio bridge and becomes **the
instrument plane**: anything that speaks it can join, including a Pluto, a
second rack PC, or the AWG if it ever wants to be out-of-process.

# Suggested first build

AWG as source, B205 as receiver, VNA out of the loop entirely.

It is a complete alternative measurement chain, it needs no phase coherence, and
it forces the Source/Receiver abstraction to be real on the first attempt rather
than retrofitted. The RF-off invariant comes first regardless.

# Open questions

- [ ] Does the PA have a control interface, or is it a bias line and a switch?
- [ ] Are both X310s populated with matching daughterboards?
- [ ] Do the B205minis on hand expose any external reference input?
- [ ] Which measurement is actually wanted first — scalar power patterns,
      modulated-waveform performance vs angle, or a second emitter for nulling
      work? The answer changes what gets built after the abstraction lands.
- [ ] Absolute gain, or relative patterns only? Absolute needs a substitution
      measurement and a known standard.
