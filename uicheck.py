#!/usr/bin/env python3
"""Drive the built dashboard against the simulated backend in a real browser.

    pip install playwright && playwright install chromium
    npm --prefix frontend install && npm --prefix frontend run build
    python uicheck.py

Starts chamber_service.py with the simulated backend, serves the built
frontend, clicks through a scan in headless Chromium, and asserts on what the
page actually shows - not on what the service sent. Fails on any uncaught JS
error.

The chrome is exercised as well as the scan, because that is where a silent
regression hides: the accordion sections must open, every tab must render, an
imported reference must overlay, jog must move the axis, and the theme button
must walk system -> light -> dark.

Screenshots land in --shots (default ./ui-shots) for eyeballing the layout in
both themes.
"""

from __future__ import annotations

import argparse
import math
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUN_NAME = "uicheck"


def wait_port(port: int, timeout: float = 25.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return True
        except OSError:
            time.sleep(0.3)
    return False


def open_section(page, title: str) -> None:
    """Expand an accordion section by its header text, if it is not already."""
    item = page.locator(f".accordion-item:has(.accordion-header:has-text('{title}'))")
    if "active" not in (item.get_attribute("class") or ""):
        page.locator(f".accordion-header:has-text('{title}')").click()
        page.wait_for_timeout(450)


def synthetic_pattern() -> str:
    """A stand-in for a solver export: the same array factor the simulator
    uses, but for a slightly smaller array.

    Deliberately not identical - a model that matched the measurement exactly
    would prove nothing about the comparison. It also carries a `param` column
    the service's own pattern.csv does not, which exercises the importer's
    loose-header path rather than only its native one.
    """
    rows = ["angle_cmd_deg,angle_actual_deg,param,freq_hz,re,im,mag_db,phase_deg"]
    for i in range(180):
        ang = -180 + 2 * i
        x = math.pi * 3.7 * math.sin(math.radians(ang))
        af = 1.0 if abs(x) < 1e-9 else abs(math.sin(x) / x)
        db = max(20.0 * math.log10(max(af, 1e-12)), -45.0)
        rows.append(f"{ang:.2f},{ang:.2f},S21,2500000000,0,0,{db:.4f},0.0")
    return "\n".join(rows) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ws-port", type=int, default=8766)
    p.add_argument("--http-port", type=int, default=8090)
    p.add_argument("--shots", type=Path, default=ROOT / "ui-shots")
    p.add_argument("--browser", default=None,
                   help="path to a chromium binary, if not on the default path")
    a = p.parse_args(argv)

    from playwright.sync_api import sync_playwright

    dist = ROOT / "frontend" / "dist"
    if not dist.exists():
        print("frontend/dist not found - run: npm --prefix frontend run build",
              file=sys.stderr)
        return 2
    a.shots.mkdir(parents=True, exist_ok=True)

    # A stale directory from a previous run would make the round-trip check
    # compare against the wrong data.
    shutil.rmtree(ROOT / "runs" / RUN_NAME, ignore_errors=True)

    svc = subprocess.Popen(
        [sys.executable, str(ROOT / "chamber_service.py"), "--sim",
         "--port", str(a.ws_port)],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    http = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(a.http_port),
         "--bind", "127.0.0.1", "--directory", str(dist)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    errors: list[str] = []
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {label:<24}  {detail}")
        if not ok:
            failures += 1

    try:
        if not (wait_port(a.ws_port) and wait_port(a.http_port)):
            print("service did not come up", file=sys.stderr)
            return 1

        with sync_playwright() as pw:
            launch = {"executable_path": a.browser} if a.browser else {}
            browser = pw.chromium.launch(**launch)
            page = browser.new_page(viewport={"width": 1500, "height": 950},
                                    color_scheme="dark")
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

            page.goto(f"http://127.0.0.1:{a.http_port}/", wait_until="networkidle")
            page.wait_for_timeout(1500)
            check("connects to service", page.inner_text("#mode-badge") == "SIMULATED",
                  page.inner_text("#vna-idn"))

            # Correction state, in sim. There is no calibration behind a
            # synthesized pattern, so the badge must stay down rather than show
            # a green CAL the numbers do not deserve.
            check("sim claims no calibration",
                  "n/a" in page.inner_text("#vna-corr")
                  and page.locator("#corr-badge").is_hidden(),
                  page.inner_text("#vna-corr"))

            # System theme is the default, and the page starts on whatever the
            # browser reports. Assert that before touching the button.
            check("follows system theme",
                  page.get_attribute("html", "data-theme") == "dark",
                  "emulated prefers-color-scheme: dark")

            # VNA is the open section; the rest need a click on their header.
            page.fill("#f-points", "51")
            open_section(page, "Turntable")
            check("accordion opens", page.is_visible("#a-step"),
                  "Turntable section expanded")

            # Jog is main's own capability; the dial has to follow it. Read
            # the footer stat, not the dial's own readout - that one lives in an
            # inactive tab, and an invisible element has no inner_text.
            before = page.inner_text("#stat-angle")
            page.click("[data-jog='10']")
            page.wait_for_timeout(2000)
            after = page.inner_text("#stat-angle")
            check("jog moves the axis", after != before and after != "—",
                  f"{before} -> {after}")

            open_section(page, "Output")
            page.fill("#run-name", RUN_NAME)

            open_section(page, "Simulation")
            ref = a.shots / "reference.csv"
            ref.write_text(synthetic_pattern())
            page.set_input_files("#ref-file", str(ref))
            page.wait_for_timeout(400)
            check("imports reference", page.is_visible("#ref-summary"),
                  page.inner_text("#ref-name"))

            page.click("#btn-scan")
            page.wait_for_timeout(6000)
            progress = page.inner_text("#progress-label")
            check("streams mid-scan", "/" in progress and not progress.startswith("0 /"),
                  f"progress {progress}")
            check("stop is live", page.is_enabled("#btn-stop"))
            page.screenshot(path=str(a.shots / "scanning.png"))

            for _ in range(120):
                if page.inner_text("#progress-label") == "complete":
                    break
                page.wait_for_timeout(500)
            check("completes", page.inner_text("#progress-label") == "complete",
                  f"peak {page.inner_text('#stat-peak')}")
            page.wait_for_timeout(400)
            page.screenshot(path=str(a.shots / "done.png"))

            check("compares to reference",
                  page.inner_text("#stat-delta") not in ("", "—"),
                  f"delta {page.inner_text('#stat-delta')}")

            # The sim is seeded per angle, so re-importing the run's own CSV is
            # a round trip through writer, parser and comparator: it has to come
            # back zero. A non-zero answer here means one of the three is wrong.
            own = ROOT / "runs" / RUN_NAME / "pattern.csv"
            if own.is_file():
                open_section(page, "Simulation")
                page.set_input_files("#ref-file", str(own))
                page.wait_for_timeout(900)
                delta = page.inner_text("#stat-delta")
                try:
                    ok = abs(float(delta.split()[0])) < 0.05
                except (ValueError, IndexError):
                    ok = False
                check("round-trips its own run", ok, f"delta {delta}")
            else:
                check("round-trips its own run", False, f"{own} not written")

            open_section(page, "Output")
            page.click("#btn-refresh-runs")
            page.wait_for_timeout(700)
            check("lists stored runs", page.locator(".run-row").count() > 0,
                  f"{page.locator('.run-row').count()} run(s)")

            for tab, probe in (("VNA", "#chart-rect"),
                               ("Turntable", "#dial"),
                               ("Logs", "#log")):
                page.click(f".tab-btn:has-text('{tab}')")
                page.wait_for_timeout(400)
                check(f"{tab.lower()} tab renders", page.is_visible(probe))
                page.screenshot(path=str(a.shots / f"tab-{tab.lower()}.png"))

            page.click(".tab-btn:has-text('Pattern Measurement')")
            page.wait_for_timeout(300)

            # system -> light -> dark, and the label has to keep up.
            page.click("#theme-toggle")
            page.wait_for_timeout(700)
            check("light theme renders",
                  page.get_attribute("html", "data-theme") == "light",
                  page.inner_text("#theme-toggle").replace("\n", " "))
            page.screenshot(path=str(a.shots / "light.png"))

            page.click("#theme-toggle")
            page.wait_for_timeout(500)
            check("dark theme renders",
                  page.get_attribute("html", "data-theme") == "dark",
                  page.inner_text("#theme-toggle").replace("\n", " "))

            page.click("#btn-toggle-settings")
            page.wait_for_timeout(600)
            check("sidebar collapses", page.is_visible("#sidebar-icons")
                  and not page.is_visible("#accordionSettings"))
            page.screenshot(path=str(a.shots / "collapsed.png"))
            browser.close()
    finally:
        for proc in (svc, http):
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()

    check("no JS errors", not errors, "; ".join(errors[:3]) or "clean")
    print(f"\n{'ok' if not failures else str(failures) + ' failed'} - "
          f"screenshots in {a.shots}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
