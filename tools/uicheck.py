#!/usr/bin/env python3
"""Drive the built UI against the mock service in a real browser.

    pip install playwright && playwright install chromium
    npm --prefix frontend run build
    python3 tools/uicheck.py

Starts the service with fake instruments, serves the built frontend, clicks
through a scan in headless Chromium, and asserts on what the page actually
shows - not on what the service sent. Fails on any uncaught JS error.

The chrome is exercised too, because it is where a silent regression hides:
the accordion sections must open, every tab must render, an imported reference
must overlay, and the theme button must walk system -> light -> dark.

Screenshots land in --shots (default ./ui-shots) for eyeballing the layout in
both themes.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
    header = page.locator(f".accordion-header:has-text('{title}')")
    item = page.locator(f".accordion-item:has(.accordion-header:has-text('{title}'))")
    if "active" not in (item.get_attribute("class") or ""):
        header.click()
        page.wait_for_timeout(450)


def synthetic_pattern() -> str:
    """A pattern.csv-shaped reference: one cardioid cut, in the real columns."""
    import math
    rows = ["angle_cmd_deg,angle_actual_deg,param,freq_hz,re,im,mag_db,phase_deg"]
    for i in range(72):
        ang = -180 + 5 * i
        mag = 20 * math.log10(max(abs(0.5 * (1 + math.cos(math.radians(ang)))), 1e-3))
        rows.append(f"{ang:.2f},{ang:.2f},S21,2500000000,0,0,{mag:.4f},0.0")
    return "\n".join(rows) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ws-port", type=int, default=8765)
    p.add_argument("--http-port", type=int, default=8088)
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

    proc = subprocess.Popen(
        [sys.executable, "-m", "service.server", "--mock",
         "--ws-port", str(a.ws_port), "--http-port", str(a.http_port),
         "--static", str(dist), "--outroot", "/tmp/uicheck_runs"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    errors: list[str] = []
    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"{'PASS' if ok else 'FAIL'}  {label:<22}  {detail}")
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

            page.goto(f"http://localhost:{a.http_port}/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            check("connects to service", page.is_visible("#pill-mock"),
                  "mock badge shown")

            # System theme is the default, and the page starts on whatever the
            # browser reports. Assert that before touching the button.
            check("follows system theme",
                  page.get_attribute("html", "data-theme") == "dark",
                  "emulated prefers-color-scheme: dark")

            # VNA is the open section; the rest need a click on their header.
            page.fill("#points", "51")
            open_section(page, "Turntable")
            page.fill("#from-deg", "-180")
            page.fill("#to-deg", "175")
            page.fill("#step-deg", "5")
            check("accordion opens", page.is_visible("#step-deg"),
                  "Turntable section expanded")

            # An imported reference has to survive a scan running underneath it.
            open_section(page, "Simulation")
            ref = a.shots / "reference.csv"
            ref.write_text(synthetic_pattern())
            page.set_input_files("#ref-file", str(ref))
            page.wait_for_timeout(400)
            check("imports reference", page.is_visible("#ref-summary"),
                  page.inner_text("#ref-name"))

            page.click("#btn-start")
            page.wait_for_timeout(6000)

            progress = page.inner_text("#stat-progress")
            check("streams mid-scan", "/72" in progress and not progress.startswith("0/"),
                  f"progress {progress}")
            check("abort is live", page.is_enabled("#btn-abort"),
                  f"phase {page.inner_text('#pill-phase')}")
            page.screenshot(path=str(a.shots / "scanning.png"))

            for _ in range(160):
                if "done" in page.inner_text("#pill-phase"):
                    break
                page.wait_for_timeout(500)
            check("completes", page.inner_text("#stat-progress") == "72/72",
                  f"peak {page.inner_text('#stat-peak')}")
            page.wait_for_timeout(400)
            page.screenshot(path=str(a.shots / "done.png"))

            check("compares to reference",
                  page.inner_text("#stat-delta") not in ("", "\u2014"),
                  f"delta {page.inner_text('#stat-delta')}")

            for tab, probe in (("VNA", "#spectrum"),
                               ("Turntable", "#dial"),
                               ("Logs", "#log")):
                page.click(f".tab-btn:has-text('{tab}')")
                page.wait_for_timeout(400)
                check(f"{tab.lower()} tab renders", page.is_visible(probe))
                page.screenshot(path=str(a.shots / f"tab-{tab.lower()}.png"))

            page.click(".tab-btn:has-text('Pattern Measurement')")
            page.wait_for_timeout(300)

            # system -> light -> dark, and the label has to keep up.
            page.click("#btn-theme")
            page.wait_for_timeout(700)
            check("light theme renders",
                  page.get_attribute("html", "data-theme") == "light",
                  page.inner_text("#btn-theme"))
            page.screenshot(path=str(a.shots / "light.png"))

            page.click("#btn-theme")
            page.wait_for_timeout(500)
            check("dark theme renders",
                  page.get_attribute("html", "data-theme") == "dark",
                  page.inner_text("#btn-theme"))

            page.click("#btn-toggle-settings")
            page.wait_for_timeout(600)
            check("sidebar collapses", page.is_visible("#sidebar-icons")
                  and not page.is_visible("#accordionSettings"))
            page.screenshot(path=str(a.shots / "collapsed.png"))
            browser.close()
    finally:
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
