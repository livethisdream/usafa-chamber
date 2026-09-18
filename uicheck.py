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


def fmt_disabled(page, value: str) -> bool:
    """Whether a trace-format option is greyed out.

    Reads the DOM property rather than asking Playwright, because `<option>`
    is exactly the element where "disabled" is ambiguous between the option and
    the select that holds it, and this test cares about the option.
    """
    return page.eval_on_selector(f"#trace-format option[value='{value}']",
                                 "o => o.disabled")


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

            # Trace formats. All four are transforms of the vector the scan
            # already delivered, so the test is that switching format never
            # goes back to the instrument - and that the two the data cannot
            # support are refused rather than drawn. The run above was S21, so
            # VSWR and Smith must both be unavailable on it.
            page.click(".tab-btn:has-text('VNA')")
            page.wait_for_timeout(400)
            check("phase is available on a scan",
                  not fmt_disabled(page, "phase"),
                  "complex data arrived with scan_point")
            check("VSWR refused for S21",
                  fmt_disabled(page, "vswr") and fmt_disabled(page, "smith"),
                  page.locator("#trace-format option[value='vswr']").inner_text())

            page.select_option("#trace-format", "phase")
            page.wait_for_timeout(400)
            check("phase renders", page.is_visible("#chart-rect")
                  and not page.is_visible("#chart-smith"),
                  page.inner_text("#trace-label"))

            # A single sweep: the tower must not move, and the reflection
            # parameter must unlock the two formats S21 could not support.
            open_section(page, "VNA")
            page.select_option("#f-param", "S11")
            page.select_option("#run-type", "single")
            page.wait_for_timeout(200)
            check("run type relabels the button",
                  page.inner_text("#btn-scan").strip() == "Sweep once"
                  and not page.is_enabled("#a-step"),
                  "angle grid disabled for a single sweep")

            angle_before = page.inner_text("#stat-angle")
            page.click("#btn-scan")
            for _ in range(60):
                if "S11" in page.inner_text("#trace-label"):
                    break
                page.wait_for_timeout(200)
            check("single sweep measures",
                  "S11" in page.inner_text("#trace-label"),
                  page.inner_text("#trace-label"))
            check("single sweep leaves the axis alone",
                  page.inner_text("#stat-angle") == angle_before,
                  f"still at {angle_before}")

            check("reflection unlocks VSWR and Smith",
                  not fmt_disabled(page, "vswr") and not fmt_disabled(page, "smith"))

            # The sweep ran at 51 points while the pattern above was measured at
            # 51 too, so force them apart: a sweep at a different span must not
            # re-label the pattern's cut picker or shift what the imported
            # reference is compared against. The trace carries its own axis.
            page.fill("#f-points", "21")
            page.click("#btn-scan")
            page.wait_for_timeout(1500)
            axes = page.evaluate(
                "() => ({ trace: window.__chamber.state.traceFreqs.length,"
                " pattern: window.__chamber.state.freqs.length,"
                " cuts: document.getElementById('cut-freq').options.length })")
            check("a sweep leaves the pattern's axis alone",
                  axes["trace"] == 21 and axes["pattern"] == 51
                  and axes["cuts"] == 51,
                  f"trace {axes['trace']}, pattern {axes['pattern']}, "
                  f"{axes['cuts']} cuts")
            page.fill("#f-points", "51")

            page.select_option("#trace-format", "vswr")
            page.wait_for_timeout(400)
            page.screenshot(path=str(a.shots / "format-vswr.png"))

            # The Smith chart is the one format that swaps the plot div, so
            # assert the swap both ways rather than only that it appeared.
            page.select_option("#trace-format", "smith")
            page.wait_for_timeout(600)
            check("smith chart renders",
                  page.is_visible("#chart-smith") and not page.is_visible("#chart-rect"),
                  f"{page.locator('#chart-smith .trace').count()} trace(s)")

            # The trace is drawn on the impedance plane, so what reaches Plotly
            # must be z, not Gamma. Feeding it Gamma renders something that
            # looks like a Smith chart right up until you notice the match is
            # missing: every point with a negative real part lands outside
            # r = 0 and is clipped, and near resonance Gamma's real part is
            # exactly what goes negative. A passive load has r >= 0, so the
            # locus staying inside the chart is the assertion that catches it.
            locus = page.evaluate(
                "() => { const t = document.getElementById('chart-smith').data[0];"
                " return { n: t.real.length, minReal: Math.min(...t.real),"
                " want: window.__chamber.state.traceFreqs.length,"
                " finite: t.real.every(Number.isFinite)"
                " && t.imag.every(Number.isFinite) }; }")
            check("smith locus stays inside the chart",
                  locus["n"] == locus["want"] and locus["n"] > 0
                  and locus["minReal"] >= 0 and locus["finite"],
                  f"{locus['n']}/{locus['want']} points, "
                  f"min r = {locus['minReal']:.3f}")
            page.screenshot(path=str(a.shots / "format-smith.png"))

            # Retuning the sidebar must NOT disturb the plot: what is on screen
            # is still the S11 that was measured, and it stays captioned that
            # way. The fallback is owed to new data, not to a form field.
            page.select_option("#f-param", "S21")
            page.wait_for_timeout(400)
            check("sidebar retune leaves measured data alone",
                  page.input_value("#trace-format") == "smith"
                  and "S11" in page.inner_text("#trace-label"),
                  page.inner_text("#trace-label"))

            # ...but the S21 sweep that follows cannot be shown on a Smith
            # chart, so the format has to give way when that data lands.
            page.click("#btn-scan")
            for _ in range(60):
                if "S21" in page.inner_text("#trace-label"):
                    break
                page.wait_for_timeout(200)
            check("smith gives way to data it cannot show",
                  page.input_value("#trace-format") == "mag"
                  and page.is_visible("#chart-rect")
                  and not page.is_visible("#chart-smith"),
                  f"format now {page.input_value('#trace-format')}")
            page.select_option("#run-type", "pattern")

            # Calibration. The sim corrects nothing and says so - what is
            # under test is the wizard: the modal opens, the acknowledgement
            # gates the button, the run reports steps, and the panel picks up
            # the record. The drift warning is then forced by retuning the
            # sweep away from what was calibrated.
            page.click(".tab-btn:has-text('Pattern Measurement')")
            open_section(page, "VNA")
            page.click("#btn-cal")
            page.wait_for_timeout(400)
            check("cal modal opens",
                  page.is_visible("#cal-modal") and not page.is_enabled("#cal-go"),
                  "start disabled until the module is acknowledged")
            page.screenshot(path=str(a.shots / "cal-setup.png"))

            # The parameter decides how many ports the cal covers, and the modal
            # has to say which - a 2-port label over a 1-port procedure sends
            # somebody into the chamber to mate a thru nothing will collect.
            check("cal modal says 2-port for S21",
                  "2-port" in page.inner_text("#cal-title")
                  and "across those two ends" in page.inner_text("#cal-step-mate"),
                  page.inner_text("#cal-title"))
            page.click("#cal-close")
            open_section(page, "VNA")
            page.select_option("#f-param", "S11")
            page.click("#btn-cal")
            page.wait_for_timeout(400)
            check("cal modal says 1-port for S11",
                  "1-port" in page.inner_text("#cal-title")
                  and "port 1" in page.inner_text("#cal-title")
                  and "port 1" in page.inner_text("#cal-step-unmate"),
                  page.inner_text("#cal-title"))
            page.screenshot(path=str(a.shots / "cal-1port.png"))
            page.click("#cal-close")
            page.select_option("#f-param", "S21")
            page.click("#btn-cal")
            page.wait_for_timeout(400)
            page.check("#cal-ack")
            page.wait_for_timeout(200)
            check("acknowledgement enables start", page.is_enabled("#cal-go"))
            page.click("#cal-go")
            page.wait_for_timeout(600)
            page.screenshot(path=str(a.shots / "cal-running.png"))

            for _ in range(40):
                if page.is_visible("#cal-stage-done"):
                    break
                page.wait_for_timeout(300)
            check("calibration completes",
                  page.is_visible("#cal-stage-done"),
                  page.inner_text("#cal-result") if page.is_visible("#cal-result") else "")
            check("sim cal claims nothing",
                  "imulated" in page.inner_text("#cal-result"),
                  page.inner_text("#cal-result"))
            page.click("#cal-cancel")
            page.wait_for_timeout(300)
            check("cal record lands in the panel",
                  page.inner_text("#cal-summary") != "none recorded",
                  page.inner_text("#cal-summary"))
            check("panel records the port count",
                  "2-port" in page.inner_text("#cal-summary"),
                  page.inner_text("#cal-summary"))

            # A 2-port calibration covers every parameter it collected, so
            # retuning to S11 must not raise a coverage warning. The inverse
            # case - a 1-port cal against S21 - is rigcheck's cal_ports_vs_param.
            page.select_option("#f-param", "S11")
            page.wait_for_timeout(300)
            check("2-port cal covers a reflection term",
                  "covers" not in page.inner_text("#cal-drift"),
                  page.inner_text("#cal-drift")[:60])
            page.select_option("#f-param", "S21")

            page.fill("#f-start", "5")
            page.dispatch_event("#f-start", "input")
            page.wait_for_timeout(300)
            check("sweep drift is flagged",
                  page.locator("#cal-drift").is_visible()
                  and "differs" in page.inner_text("#cal-drift"),
                  page.inner_text("#cal-drift")[:60])
            page.fill("#f-start", "2")
            page.dispatch_event("#f-start", "input")

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
