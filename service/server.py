#!/usr/bin/env python3
"""Headless chamber service: WebSocket control plane over the scan engine.

    python3 -m service.server --mock          # no hardware, fake instruments
    python3 -m service.server                 # real instruments

The scan is blocking (VISA is synchronous), so it runs on a worker thread and
pushes engine events into an asyncio queue, which one broadcast task fans out
to every connected client. The engine itself is untouched by any of this - it
just calls `emit`.

Protocol, JSON both ways.

  client -> server   {"cmd": "get_state"}
                     {"cmd": "connect",   "config": {...}}
                     {"cmd": "start",     "config": {...}}
                     {"cmd": "abort"}
                     {"cmd": "disconnect"}

  server -> client   {"type": "state",  "state": "...", ...}
                     {"type": "log"|"phase"|"ready"|"point"|"closure"|"done", ...}
                     {"type": "error",  "message": "..."}

Every engine event is forwarded verbatim, so the browser sees exactly what the
CLI prints - the two front ends cannot drift apart.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import functools
import json
import sys
import threading
import traceback
from pathlib import Path

import numpy as np
import websockets

from acquisition.config import (PositionerCmds, PositionerConfig, ScanConfig,
                                VnaConfig)
from acquisition.engine import ScanEngine
from acquisition.instruments import Positioner, Vna

ROOT = Path(__file__).resolve().parent.parent


def _coerce(o):
    """Last-resort JSON encoder.

    numpy scalars are not JSON serializable, and a single one escaping into an
    event used to raise inside the broadcast task - which killed every client
    connection at once. Coerce instead of dying.
    """
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    return str(o)


def dumps(obj) -> str:
    return json.dumps(obj, default=_coerce)

# States the UI knows how to render.
IDLE, CONNECTING, READY, SCANNING, ERROR = (
    "idle", "connecting", "ready", "scanning", "error")


def _apply(cls, base, overrides: dict):
    """Build a dataclass from `base`, applying only fields that exist.

    Config arrives from a browser; silently ignoring unknown keys keeps a
    stale UI from crashing the service, and typed coercion keeps strings from
    reaching pyvisa.
    """
    fields = {f.name: f for f in dataclasses.fields(cls)}
    kw = {k: v for k, v in dataclasses.asdict(base).items()} if base else {}
    for k, v in (overrides or {}).items():
        if k not in fields or v is None:
            continue
        t = fields[k].type
        try:
            if k == "outdir":
                v = Path(v)
            elif "float" in str(t):
                v = float(v)
            elif "int" in str(t) and "str" not in str(t):
                v = int(v)
            elif "bool" in str(t):
                v = bool(v)
        except (TypeError, ValueError):
            continue
        kw[k] = v
    if "outdir" in kw and not isinstance(kw["outdir"], Path):
        kw["outdir"] = Path(kw["outdir"])
    return cls(**kw)


class ChamberService:
    def __init__(self, mock: bool = False, outroot: Path | None = None):
        self.mock = mock
        self.outroot = outroot or (ROOT / "runs")
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.clients: set = set()

        self.state = IDLE
        self.detail = ""
        self.vna: Vna | None = None
        self.pos: Positioner | None = None
        self.abort = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_ready: dict | None = None
        self.history: list[dict] = []      # points of the current run, for late joiners

        if mock:
            from acquisition import mock_instruments
            mock_instruments.install()

    # -- plumbing ----------------------------------------------------------

    def emit_threadsafe(self, ev: dict) -> None:
        """Called from the worker thread by the engine."""
        if ev.get("type") == "point":
            self.history.append(ev)
        elif ev.get("type") == "ready":
            self.last_ready = ev
        if self.loop is not None and self.queue is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, ev)

    def set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        self.emit_threadsafe(self.state_frame())

    def state_frame(self) -> dict:
        return {"type": "state", "state": self.state, "detail": self.detail,
                "mock": self.mock, "connected": self.vna is not None}

    async def broadcast(self) -> None:
        assert self.queue is not None
        while True:
            ev = await self.queue.get()
            if not self.clients:
                continue
            msg = dumps(ev)
            await asyncio.gather(
                *[c.send(msg) for c in list(self.clients)],
                return_exceptions=True)

    # -- commands ----------------------------------------------------------

    def _open(self, cfg: dict) -> None:
        import pyvisa
        rm = pyvisa.ResourceManager()
        vcfg = _apply(VnaConfig, VnaConfig(), cfg.get("vna", {}))
        pcfg = _apply(PositionerConfig, PositionerConfig(), cfg.get("positioner", {}))
        cmds = _apply(PositionerCmds, PositionerCmds(), cfg.get("cmds", {}))
        self.vna = Vna(rm, vcfg)
        self.pos = Positioner(rm, pcfg, cmds)

    def do_connect(self, cfg: dict) -> None:
        def work():
            try:
                self.set_state(CONNECTING)
                self._open(cfg)
                idn = self.vna.idn()
                here = self.pos.position()
                self.emit_threadsafe({"type": "log", "level": "info",
                                      "source": "vna", "message": f"VNA: {idn}"})
                self.emit_threadsafe({"type": "log", "level": "info",
                                      "source": "pos",
                                      "message": f"positioner at {here:.2f} deg"})
                self.set_state(READY, idn)
            except Exception as exc:
                self._fail(exc, "connect")
        self._spawn(work)

    def do_start(self, cfg: dict) -> None:
        if self.state == SCANNING:
            self._error("a scan is already running")
            return

        def work():
            try:
                if self.vna is None:
                    self.set_state(CONNECTING)
                    self._open(cfg)
                scan = _apply(ScanConfig, ScanConfig(), cfg.get("scan", {}))
                if "outdir" not in (cfg.get("scan") or {}):
                    scan.outdir = self.outroot / _stamp(cfg)
                self.abort.clear()
                self.history.clear()
                self.set_state(SCANNING)
                ScanEngine(self.vna, self.pos, scan,
                           emit=self.emit_threadsafe, abort=self.abort).run()
                self.set_state(READY)
            except Exception as exc:
                self._fail(exc, "scan")
        self._spawn(work)

    def do_abort(self) -> None:
        self.abort.set()
        self.emit_threadsafe({"type": "log", "level": "warn", "source": "service",
                              "message": "abort requested; stopping after the "
                                         "current angle"})
        # Halt the axis immediately rather than waiting for the loop to notice.
        if self.pos is not None:
            try:
                self.pos.stop()
            except Exception as exc:
                self.emit_threadsafe({"type": "log", "level": "error",
                                      "source": "pos",
                                      "message": f"STOP failed: {exc}"})

    def do_disconnect(self) -> None:
        self.abort.set()
        for dev, name in ((self.pos, "pos"), (self.vna, "vna")):
            if dev is None:
                continue
            try:
                if name == "pos":
                    dev.stop()
                dev.close()
            except Exception:
                pass
        self.vna = self.pos = None
        self.last_ready = None
        self.set_state(IDLE)

    # -- helpers -----------------------------------------------------------

    def _spawn(self, fn) -> None:
        if self.worker is not None and self.worker.is_alive():
            self._error("busy")
            return
        self.worker = threading.Thread(target=fn, daemon=True)
        self.worker.start()

    def _error(self, message: str) -> None:
        self.emit_threadsafe({"type": "error", "message": message})

    def _fail(self, exc: Exception, where: str) -> None:
        # The axis is halted on any failure path, matching the CLI's finally
        # block. A comms error must never leave the table turning.
        if self.pos is not None:
            try:
                self.pos.stop()
            except Exception:
                pass
        traceback.print_exc()
        self._error(f"{where}: {type(exc).__name__}: {exc}")
        self.set_state(ERROR, f"{type(exc).__name__}: {exc}")

    # -- websocket ---------------------------------------------------------

    async def handler(self, ws) -> None:
        self.clients.add(ws)
        try:
            await ws.send(dumps(self.state_frame()))
            # Replay only while a scan is actually running, so a client that
            # reloads mid-run catches up. Replaying a finished run's points
            # would hand a fresh client a plot of the *previous* scan.
            if self.state == SCANNING:
                if self.last_ready:
                    await ws.send(dumps(self.last_ready))
                for ev in list(self.history):
                    await ws.send(dumps(ev))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(dumps({"type": "error",
                                              "message": "malformed JSON"}))
                    continue
                await self.dispatch(msg, ws)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)

    async def dispatch(self, msg: dict, ws) -> None:
        cmd = msg.get("cmd")
        cfg = msg.get("config") or {}
        if cmd == "get_state":
            await ws.send(dumps(self.state_frame()))
        elif cmd == "connect":
            self.do_connect(cfg)
        elif cmd == "start":
            self.do_start(cfg)
        elif cmd == "abort":
            self.do_abort()
        elif cmd == "disconnect":
            self.do_disconnect()
        else:
            await ws.send(dumps({"type": "error",
                                      "message": f"unknown command {cmd!r}"}))


def _stamp(cfg: dict) -> str:
    """Run directory name. Timestamps come from the caller when given."""
    from datetime import datetime, timezone
    return (cfg.get("run_name")
            or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))


def serve_static(directory: Path, port: int) -> None:
    """Serve the built frontend. Enough for a lab machine; not a public server."""
    import http.server
    import socketserver
    if not directory.exists():
        print(f"[http] {directory} does not exist; skipping static server")
        return
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))

    class Quiet(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = Quiet(("0.0.0.0", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[http] serving {directory} on http://localhost:{port}")


async def amain(args) -> None:
    svc = ChamberService(mock=args.mock, outroot=Path(args.outroot))
    svc.loop = asyncio.get_running_loop()
    svc.queue = asyncio.Queue()
    asyncio.create_task(svc.broadcast())

    if args.static:
        serve_static(Path(args.static), args.http_port)

    print(f"[ws] listening on ws://localhost:{args.ws_port}"
          f"{'  (MOCK INSTRUMENTS)' if args.mock else ''}")
    async with websockets.serve(svc.handler, "0.0.0.0", args.ws_port):
        await asyncio.Future()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m service.server",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mock", action="store_true",
                   help="run against fake instruments, no hardware needed")
    p.add_argument("--ws-port", type=int, default=8765)
    p.add_argument("--http-port", type=int, default=8080)
    p.add_argument("--static", default=str(ROOT / "frontend" / "dist"),
                   help="directory of built frontend assets to serve")
    p.add_argument("--outroot", default=str(ROOT / "runs"),
                   help="parent directory for run output folders")
    args = p.parse_args(argv)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
