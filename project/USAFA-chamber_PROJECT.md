# USAFA Chamber — Custom Antenna Pattern Controller

Replacing EMQuest with a purpose-built step-and-measure controller for the
chamber, driving the VNA and turntable directly.

## Hardware

| Role | Device | Interface |
| --- | --- | --- |
| VNA | Copper Mountain A2202-Fx | SCPI over TCP (S2VNA socket server, default port 5025) |
| Positioner | ETS-Lindgren EMControl 7006-001 card in an EMCenter chassis | Ethernet (VISA/socket) or serial |

Prior workflow: EMQuest with older equipment. The goal here is a scriptable
acquisition path we control end to end — sweep config, motion, data layout,
and plotting.

## TODO

- [x] **Provide the phase GUI repo** — resolved: `livethisdream/phaser`
      (ADI CN0566 Phaser beamformer). Access granted and the repo is attached
      to the session. Design language summarized below.

- [ ] **Decide how much of Phaser to reuse** — the look alone, or the
      look plus its backend/transport architecture. See "Reuse decision".

## Acquisition script status

`acquisition/pattern_measure.py` — reviewed, fixed, and covered by a scenario
suite (`acquisition/dryrun.py`, 9/9 passing against mock instruments). All six
review findings are closed:

- [x] Angle grid overshot the requested stop when the span wasn't an integer
      multiple of the step (`--step 30` measured 360°). Now floors, drops a
      duplicate full-circle endpoint, and reports the shortfall.
- [x] A mid-scan exception left the turntable moving — only `KeyboardInterrupt`
      issued a stop. `STOP` now runs on every exit path, before the socket
      closes.
- [x] A position mismatch only warned, then recorded the sweep at the commanded
      angle. Now retries, then fails loudly; the plot uses readback angles.
- [x] A truncated VNA response crashed on array assignment. Now length-checked
      and re-read once.
- [x] The polar plot closed the trace unconditionally, fabricating data across
      the unmeasured span of a partial cut. Now closes only on a full
      revolution, and the radial axis fits the data instead of clipping at
      −40 dB.
- [x] `seek()` could return before motion started. Completion now requires
      motion-complete *and* an in-tolerance readback on consecutive polls.

Also addressed: serial line parameters are set for ASRL resources; sweep type,
averaging, and smoothing are written explicitly rather than inherited (without
`SYST:PRES`, which would clear the calibration); correction state is queried and
reported; a warm-up sweep runs before the frequency vector is trusted; transient
VISA failures are retried at the query layer; run metadata is written to
`run_meta.json`.

Added on top of the fixes: end-of-run closure check (re-measures the start
angle to quantify drift, on by default), optional backlash takeup, and optional
aux-parameter capture (`--aux-param S11`).

Still unverified against hardware — the EMCenter mnemonics in `PositionerCmds`,
per ETS-Lindgren manual 399342.

## UI language — from `livethisdream/phaser`

Source of truth: `frontend/src/style.css` (~1400 lines) and `frontend/index.html`.
Vanilla JS on Vite 8, no framework; Plotly for charts.

**Tokens** — declared on `:root`, overridden wholesale by `html[data-theme="light"]`,
so both themes use the same names:

- Surfaces: `--bg-dark #0f111a`, `--bg-surface rgba(30,33,44,.6)` with
  `backdrop-filter: blur(12px)`, hairline `--glass-border rgba(255,255,255,.08)`
- Accent: `--primary #0067b9` (ADI blue), `--secondary #0088d1`,
  `--success #10b981`, `--error #ef4444`, amber `#f59e0b` for transitional states
- Text: `--text-main #f8fafc`, `--text-muted #94a3b8`
- Geometry: `--radius 16px`, `--transition all .3s cubic-bezier(.4,0,.2,1)`
- Type: Inter for body, Outfit for headings; wordmark is a gradient clipped to text
- Body carries a fixed two-corner radial-gradient wash in the accent color

**Layout** — icon rail (`.sidebar-icon-btn`) → collapsible accordion settings
panel (`.controls-panel.glass-panel`) → main plot area, plus a logs pane, modals,
and a status bar. `.glass-panel` is the universal container.

**Live-state conventions** — directly relevant to instrument control:

- `.dot.connected` / `.dot.disconnected` — 10px dot with a colored glow
- `.backend-status-pill.state-starting|ready|error` — amber/green/red, each as
  10% background over a 40% border of the same hue
- `.cal-status-indicator`, `.cal-spinner`, `.calibration-feedback` — the
  long-running-operation pattern, already close to what a scan needs
- `.log-console` / `.log-line` — streaming log

**Architecture** — `frontend/src/transport.js` is a facade over four backends
(web REST+WebSocket, PyWebView IPC, Tauri, Electron) chosen at runtime.
`phaser_headless.py` runs ZMQ pub/rep (5555/5556) plus WebSocket (8765) and
HTTP (8080), deployed as a systemd unit. Its `do_sweep()` long-running loop
broadcasting progress is structurally the same problem as a pattern scan.

Note: `transport-{web,ipc,tauri,electron}.js` are gitignored by explicit path
in the root `.gitignore`, so the committed `frontend/` cannot build as-is —
only `frontend-radar/` has its transport committed. Fine for reading the design
language; a blocker if we ever want to run it.

## Reuse decision

Two levels, not yet chosen:

1. **Look only** — new chamber frontend, same tokens and component classes.
   Cheap, no coupling.
2. **Look plus architecture** — also mirror the headless-service +
   transport-facade split, wrapping `pattern_measure.py` as the backend so a
   scan streams progress to the UI the way `do_sweep()` does.

## Open questions

- Cable wrap: is there a rotary joint, or does the scan range need to stay
  within ±180°?
- Which S-parameters to capture per angle — S21 only, or S21 + S11 to catch a
  connection change mid-scan?
