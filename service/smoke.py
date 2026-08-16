#!/usr/bin/env python3
"""End-to-end protocol check: real service, mock instruments, real WebSocket.

    python3 -m service.smoke

Starts the service as a subprocess against fake hardware, drives it the way the
browser does, and asserts on the event stream - including that an abort
actually stops the run rather than just setting a flag.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
PORT = 8791
URL = f"ws://localhost:{PORT}"


async def _wait_for_server(timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            async with websockets.connect(URL):
                return
        except (OSError, websockets.InvalidHandshake):
            await asyncio.sleep(0.25)
    raise RuntimeError("service did not come up")


async def _collect(ws, until: str, timeout: float = 120.0) -> list[dict]:
    """Read frames until one of type `until` arrives."""
    out: list[dict] = []
    async with asyncio.timeout(timeout):
        while True:
            ev = json.loads(await ws.recv())
            out.append(ev)
            if ev.get("type") == until:
                return out
            if ev.get("type") == "error":
                raise AssertionError(f"service error: {ev['message']}")


def _cfg(outdir: Path, step: float = 30.0) -> dict:
    # settle/poll are shortened: there is no real mass to ring down.
    return {"vna": {"resource": "TCPIP0::127.0.0.1::5025::SOCKET", "points": 21},
            "positioner": {"resource": "TCPIP0::192.168.1.50::5025::SOCKET",
                           "move_timeout_s": 10.0, "settle_s": 0.02,
                           "poll_s": 0.01},
            "scan": {"start_deg": 0.0, "stop_deg": 355.0, "step_deg": step,
                     "outdir": str(outdir)}}


async def scenario_full_run(tmp: Path) -> str:
    outdir = tmp / "full"
    async with websockets.connect(URL) as ws:
        first = json.loads(await ws.recv())
        assert first["type"] == "state", first
        assert first["mock"] is True, "service is not in mock mode"

        await ws.send(json.dumps({"cmd": "start", "config": _cfg(outdir)}))
        evs = await _collect(ws, "done")

    kinds = [e["type"] for e in evs]
    ready = [e for e in evs if e["type"] == "ready"]
    points = [e for e in evs if e["type"] == "point"]
    closure = [e for e in evs if e["type"] == "closure"]
    done = evs[-1]

    assert ready, f"no ready frame; saw {sorted(set(kinds))}"
    assert len(points) == 12, f"expected 12 points, got {len(points)}"
    assert points[0]["n"] == 12 and points[-1]["index"] == 11
    assert all(len(p["mag_db"]) == 21 for p in points), "wrong spectrum length"
    assert max(p["angle_cmd"] for p in points) <= 355.0, "overshot stop angle"
    assert closure, "no closure frame"
    assert done["aborted"] is False
    assert (outdir / "pattern.csv").exists(), "csv missing"
    assert (outdir / "run_meta.json").exists(), "metadata missing"
    assert (outdir / "pattern.png").exists(), "plot missing"
    return (f"{len(points)} points streamed, closure "
            f"{closure[0]['max_abs_delta_db']:.3f} dB, 3 files written")


async def scenario_abort(tmp: Path) -> str:
    outdir = tmp / "abort"
    async with websockets.connect(URL) as ws:
        await ws.recv()                                    # state
        await ws.send(json.dumps({"cmd": "start",
                                  "config": _cfg(outdir, step=5.0)}))
        # Wait for OUR run to be announced before counting anything, so a
        # replayed frame from an earlier run cannot be mistaken for progress.
        await _collect(ws, "ready")
        seen = 0
        async with asyncio.timeout(60):
            while seen < 3:                                # let it get going
                ev = json.loads(await ws.recv())
                if ev.get("type") == "point":
                    seen += 1
        await ws.send(json.dumps({"cmd": "abort"}))
        evs = await _collect(ws, "done")

    done = evs[-1]
    assert done["aborted"] is True, "abort did not mark the run aborted"
    assert 3 <= done["angles_measured"] < 72, \
        f"expected a partial run, got {done['angles_measured']} of 72 angles"
    assert (outdir / "pattern.csv").exists(), "aborted run kept no data"
    return (f"stopped after {done['angles_measured']} of 72 angles, "
            f"partial data kept")


async def scenario_bad_command() -> str:
    async with websockets.connect(URL) as ws:
        await ws.recv()
        await ws.send(json.dumps({"cmd": "nonsense"}))
        evs = await _collect_errors(ws)
        assert "nonsense" in evs[-1]["message"], evs[-1]
        await ws.send("{not json")
        evs = await _collect_errors(ws)
        assert "JSON" in evs[-1]["message"], evs[-1]
    return "unknown command and malformed JSON both refused, connection kept"


async def _collect_errors(ws, timeout: float = 15.0) -> list[dict]:
    """Read until an error frame, tolerating any other traffic in between."""
    out: list[dict] = []
    async with asyncio.timeout(timeout):
        while True:
            ev = json.loads(await ws.recv())
            out.append(ev)
            if ev.get("type") == "error":
                return out


async def amain() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="chamber_smoke_"))
    log = tmp / "server.log"
    fh = log.open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "service.server", "--mock",
         "--ws-port", str(PORT), "--static", "/nonexistent",
         "--outroot", str(tmp)],
        cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, text=True)
    failures = 0
    try:
        await _wait_for_server()
        for name, coro in (("full_run", scenario_full_run(tmp)),
                           ("abort", scenario_abort(tmp)),
                           ("bad_command", scenario_bad_command())):
            try:
                detail = await coro
                print(f"PASS  {name:<12}  {detail}")
            except (AssertionError, TimeoutError, OSError,
                    websockets.WebSocketException) as e:
                print(f"FAIL  {name:<12}  {type(e).__name__}: {e}")
                failures += 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        fh.close()
        if failures:
            print("\n--- service log tail ---")
            print("\n".join(log.read_text().splitlines()[-40:]))
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{3 - failures}/3 protocol scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
