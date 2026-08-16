# Service

WebSocket control plane over the scan engine.

```bash
python3 -m service.server --mock     # fake instruments, nothing moves
python3 -m service.server            # real instruments
```

Ports: WebSocket on 8765, static frontend on 8080 (`--ws-port`, `--http-port`).
Run output goes under `--outroot`, one directory per run.

## Why a thread

VISA is synchronous, so the scan runs on a worker thread and pushes engine
events into an asyncio queue. One broadcast task fans them out to every
connected client. The engine is untouched by any of this — it just calls
`emit`, exactly as it does for the CLI.

## Protocol

JSON both directions.

| client → server | effect |
| --- | --- |
| `{"cmd":"get_state"}` | current state frame |
| `{"cmd":"connect","config":{...}}` | open the instruments, do not scan |
| `{"cmd":"start","config":{...}}` | open if needed, then scan |
| `{"cmd":"abort"}` | halt the axis and stop after the current angle |
| `{"cmd":"disconnect"}` | stop and close the instruments |

Server frames are the engine's events forwarded verbatim — `phase`, `ready`,
`point`, `closure`, `done`, `log` — plus `state` and `error` from the service
itself. States: `idle`, `connecting`, `ready`, `scanning`, `error`.

`config` is nested by target: `{"vna":{...},"positioner":{...},"cmds":{...},"scan":{...}}`.
Keys map onto the dataclasses in `acquisition/config.py`. Unknown keys are
ignored rather than fatal, so a stale UI cannot crash the service.

## Things worth knowing

- **Abort halts the axis immediately** rather than only setting a flag, then
  lets the loop exit after the current angle. Partial data is kept and still
  plotted.
- **Any failure path stops the axis**, matching the CLI's `finally` block. A
  comms error must never leave the table turning.
- **Point replay is gated on `state == "scanning"`.** A client that reloads
  mid-run catches up on the points it missed; a client connecting after a run
  finished gets a clean slate rather than a plot of the previous scan.
- **The JSON encoder coerces numpy scalars.** One `np.bool_` reaching
  `json.dumps` used to raise inside the broadcast task and drop every client at
  once, so the encoder degrades instead of dying.

## Running as a unit

`chamber.service` is a systemd template. Adjust `WorkingDirectory`, `User`, and
the resource strings, then:

```bash
sudo cp service/chamber.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now chamber
```
