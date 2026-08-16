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

- [ ] **Provide the phase GUI repo** — the UI language for this project should
      follow the phase GUI already in progress, rather than inventing a new
      one. That repo is not yet attached to this session, so the front end is
      blocked on it. Needed: the repo (owner/name) so it can be added as a
      source, and a pointer to whichever parts carry the design language worth
      reusing — layout and navigation structure, component set, color and
      typography tokens, and the conventions for live instrument state
      (connection status, run progress, abort/stop affordances).

      Access: no repo with "phase" in its name appears under `livethisdream`,
      `USAFA-ECE`, or `AF-ROBOTICS`, so it is likely a private repo under a
      work organization. A private repo under a personal account attaches
      normally; one under a work org additionally requires the Claude GitHub
      App to be installed on that org, which an admin grants at
      https://claude.ai/admin-settings/claude-in-slack. If access can't be
      granted, the fallback is to lift the design language by hand — export
      the token/theme files and a few representative screens.

## Acquisition script status

`pattern_measure.py` (draft, not yet in the repo) is reviewed but unfixed.
Confirmed against mock instruments; fixes pending:

- [ ] Angle grid overshoots the requested stop angle when the span isn't an
      integer multiple of the step (`--step 30` measured 360°).
- [ ] A mid-scan exception leaves the turntable commanded and moving — only
      `KeyboardInterrupt` issues a stop.
- [ ] A position mismatch warns but still records the sweep at the commanded
      angle, silently misregistering the pattern.
- [ ] A short/truncated VNA response crashes on array assignment.
- [ ] The polar plot closes the trace unconditionally, fabricating data across
      the unmeasured span on partial cuts.
- [ ] `seek()` can return before motion starts (blind 0.2 s sleep, then trusts
      `*OPC?`).

Verify against hardware before the first live run: serial port parameters are
never set in code, instrument state is inherited rather than preset, and
nothing checks that a calibration is applied.

## Open questions

- Cable wrap: is there a rotary joint, or does the scan range need to stay
  within ±180°?
- Which S-parameters to capture per angle — S21 only, or S21 + S11 to catch a
  connection change mid-scan?
