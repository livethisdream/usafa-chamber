/**
 * main.js — dashboard wiring.
 *
 * Talks only to the transport facade, renders whatever the service pushes, and
 * owns no knowledge of SCPI or the rig. Plots stay on Plotly, as they have
 * been since phase 1; the chrome around them is Phaser's — an accordion of
 * setting sections on the left, tabs over the plots on the right, the theme
 * control at the foot of the sidebar.
 */

import Plotly from 'plotly.js-dist-min';
import './style.css';
import { createTransport } from './transport.js';
import { DialPlot } from './dial.js';
import { parsePattern, compare, cutLabel, planesOf } from './reference.js';

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Scan state. `grid` is angle-major: grid[i] is the full trace for angles[i].
// ---------------------------------------------------------------------------
const state = {
    connected: false,
    scanning: false,
    mode: null,
    calibrating: false,
    calibration: null,
    angles: [],
    freqs: [],
    grid: [],
    cutIndex: 0,
    lastTrace: null,
    lastAngle: null,
    commanded: null,
    done: 0,
    // VNA-only capture. sweepData is keyed by S-parameter, each {re, im}.
    sweeping: false,
    hasPositioner: true,
    sweepFreqs: [],
    sweepData: {},
    sweepMarker: 0,
};

// Reference impedance. The A2202-Fx is a 50 ohm instrument and nothing in this
// project changes that, so it is a constant rather than a setting nobody would
// ever move.
const Z0 = 50;
const REFLECTION = ['S11', 'S22'];

// The imported comparison pattern: every cut in the file, plus which one is
// selected. Display-side only — the service never learns a comparison is on.
let reference = { name: '', cuts: null, key: null };

const dial = new DialPlot($('dial'));

// ---------------------------------------------------------------------------
// Theme-aware Plotly styling. Plotly does not read CSS variables, so pull the
// resolved values back out of the document whenever the theme changes.
// ---------------------------------------------------------------------------
function themeColors() {
    const cs = getComputedStyle(document.documentElement);
    return {
        text: cs.getPropertyValue('--text-main').trim(),
        muted: cs.getPropertyValue('--text-muted').trim(),
        grid: cs.getPropertyValue('--grid-color').trim(),
        primary: cs.getPropertyValue('--primary').trim(),
        secondary: cs.getPropertyValue('--secondary').trim(),
        reference: cs.getPropertyValue('--reference').trim(),
    };
}

function baseLayout() {
    const c = themeColors();
    return {
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: 'rgba(0,0,0,0)',
        font: { family: 'Inter, sans-serif', color: c.muted, size: 11 },
        margin: { l: 48, r: 16, t: 16, b: 40 },
        showlegend: false,
    };
}

const PLOT_CONFIG = { displayModeBar: false, responsive: true };

function initPlots() {
    const c = themeColors();
    Plotly.newPlot('chart-polar', [
        // 0: the measurement.
        { type: 'scatterpolar', mode: 'lines', r: [], theta: [],
          line: { color: c.primary, width: 2 }, name: 'measured' },
        // 1: the imported reference, behind it and dashed.
        { type: 'scatterpolar', mode: 'lines', r: [], theta: [],
          line: { color: c.reference, width: 1.5, dash: 'dot' },
          name: 'reference', visible: false, hoverinfo: 'skip' },
    ], {
        ...baseLayout(),
        margin: { l: 30, r: 30, t: 20, b: 20 },
        polar: {
            bgcolor: 'rgba(0,0,0,0)',
            radialaxis: { range: [-40, 0], gridcolor: c.grid, linecolor: c.grid,
                          tickfont: { size: 9 }, angle: 135 },
            angularaxis: { direction: 'clockwise', rotation: 90,
                           gridcolor: c.grid, linecolor: c.grid,
                           tickfont: { size: 9 }, dtick: 30 },
        },
    }, PLOT_CONFIG);

    // The same cut in rectangular form. Sidelobe levels and null depths are
    // read off this one; the polar view is for shape and pointing. Both carry
    // the same two traces so the overlay can be put on either.
    Plotly.newPlot('chart-pattern-rect', [
        { type: 'scatter', mode: 'lines', x: [], y: [],
          line: { color: c.primary, width: 2 }, name: 'measured' },
        { type: 'scatter', mode: 'lines', x: [], y: [],
          line: { color: c.reference, width: 1.5, dash: 'dot' },
          name: 'reference', visible: false, hoverinfo: 'skip' },
    ], {
        ...baseLayout(),
        xaxis: { title: { text: 'Angle (deg)', font: { size: 10 } },
                 gridcolor: c.grid, zerolinecolor: c.grid, dtick: 45 },
        yaxis: { title: { text: 'Magnitude (dB)', font: { size: 10 } },
                 gridcolor: c.grid, zerolinecolor: c.grid },
    }, PLOT_CONFIG);

    Plotly.newPlot('chart-rect', [{
        type: 'scatter', mode: 'lines', x: [], y: [],
        line: { color: c.secondary, width: 1.8 },
    }], {
        ...baseLayout(),
        xaxis: { title: { text: 'Frequency (GHz)', font: { size: 10 } },
                 gridcolor: c.grid, zerolinecolor: c.grid },
        yaxis: { title: { text: 'Magnitude (dB)', font: { size: 10 } },
                 gridcolor: c.grid, zerolinecolor: c.grid },
    }, PLOT_CONFIG);

    // Smith chart: reflection only. Plotting S21 on one is meaningless - a
    // transmission coefficient is not a load impedance - so the traces are
    // fixed to S11 and S22 rather than following whatever was captured.
    Plotly.newPlot('chart-smith', REFLECTION.map((name, i) => ({
        type: 'scattersmith', mode: 'lines', real: [], imag: [], name,
        line: { color: i === 0 ? c.primary : c.reference, width: 1.8 },
    })).concat([{
        // The marker, drawn as its own trace so moving it does not redraw the
        // sweep underneath it.
        type: 'scattersmith', mode: 'markers', real: [], imag: [], name: 'marker',
        marker: { color: c.secondary, size: 9 },
    }]), {
        ...baseLayout(),
        margin: { l: 24, r: 24, t: 16, b: 24 },
        smith: {
            bgcolor: 'rgba(0,0,0,0)',
            realaxis: { gridcolor: c.grid, linecolor: c.grid, tickfont: { size: 9 } },
            imaginaryaxis: { gridcolor: c.grid, linecolor: c.grid, tickfont: { size: 9 } },
        },
    }, PLOT_CONFIG);

    Plotly.newPlot('chart-sweep-mag', SWEEP_PARAMS.map((name, i) => ({
        type: 'scatter', mode: 'lines', x: [], y: [], name,
        line: { color: SWEEP_COLORS(c)[i], width: 1.6 },
    })), {
        ...baseLayout(),
        showlegend: true,
        legend: { orientation: 'h', y: 1.12, x: 0, font: { size: 10 } },
        margin: { l: 48, r: 12, t: 28, b: 40 },
        xaxis: { title: { text: 'Frequency (GHz)', font: { size: 10 } },
                 gridcolor: c.grid, zerolinecolor: c.grid },
        yaxis: { title: { text: 'Magnitude (dB)', font: { size: 10 } },
                 gridcolor: c.grid, zerolinecolor: c.grid },
    }, PLOT_CONFIG);
}

const SWEEP_PARAMS = ['S11', 'S21', 'S12', 'S22'];
const SWEEP_COLORS = (c) => [c.primary, c.secondary, c.reference, c.muted];

function restylePlots() {
    const c = themeColors();
    Plotly.relayout('chart-polar', {
        'font.color': c.muted,
        'polar.radialaxis.gridcolor': c.grid,
        'polar.radialaxis.linecolor': c.grid,
        'polar.angularaxis.gridcolor': c.grid,
        'polar.angularaxis.linecolor': c.grid,
    });
    Plotly.restyle('chart-polar', { 'line.color': [c.primary, c.reference] }, [0, 1]);
    Plotly.relayout('chart-pattern-rect', {
        'font.color': c.muted,
        'xaxis.gridcolor': c.grid, 'xaxis.zerolinecolor': c.grid,
        'yaxis.gridcolor': c.grid, 'yaxis.zerolinecolor': c.grid,
    });
    Plotly.restyle('chart-pattern-rect', { 'line.color': [c.primary, c.reference] }, [0, 1]);
    Plotly.relayout('chart-rect', {
        'font.color': c.muted,
        'xaxis.gridcolor': c.grid, 'xaxis.zerolinecolor': c.grid,
        'yaxis.gridcolor': c.grid, 'yaxis.zerolinecolor': c.grid,
    });
    Plotly.restyle('chart-rect', { 'line.color': c.secondary });
    Plotly.relayout('chart-smith', {
        'font.color': c.muted,
        'smith.realaxis.gridcolor': c.grid, 'smith.realaxis.linecolor': c.grid,
        'smith.imaginaryaxis.gridcolor': c.grid,
        'smith.imaginaryaxis.linecolor': c.grid,
    });
    Plotly.restyle('chart-smith', { 'line.color': [c.primary, c.reference] }, [0, 1]);
    Plotly.restyle('chart-smith', { 'marker.color': c.secondary }, [2]);
    Plotly.relayout('chart-sweep-mag', {
        'font.color': c.muted,
        'xaxis.gridcolor': c.grid, 'xaxis.zerolinecolor': c.grid,
        'yaxis.gridcolor': c.grid, 'yaxis.zerolinecolor': c.grid,
    });
    Plotly.restyle('chart-sweep-mag', { 'line.color': SWEEP_COLORS(c) },
                   SWEEP_PARAMS.map((_, i) => i));
    dial.draw();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

/** The measured cut: one dB value per angle that has been measured so far. */
function measuredCut() {
    const k = state.cutIndex;
    const r = [], theta = [];
    for (let i = 0; i < state.angles.length; i++) {
        const row = state.grid[i];
        if (!row) continue;                       // angle not measured yet
        r.push(row[k]);
        theta.push(state.angles[i]);
    }
    return { r, theta };
}

/** Bring an angle into [lo, lo+360), so a reference and a measurement that
 *  were written on different conventions share one axis. */
function wrapInto(a, lo) {
    return lo + ((((a - lo) % 360) + 360) % 360);
}

/**
 * One trace for the rectangular view, sorted along the angle axis.
 *
 * A cut that wraps out of the measured span arrives in two pieces once its
 * angles are brought into the same domain. A line drawn straight across the
 * gap between them is not data, so the gap is broken with a null instead.
 */
function rectSeries(angles, values, lo) {
    const pts = angles.map((a, i) => [wrapInto(a, lo), values[i]])
        .sort((u, v) => u[0] - v[0]);
    const steps = pts.slice(1).map((q, i) => q[0] - pts[i][0]).sort((u, v) => u - v);
    const typical = steps.length ? steps[Math.floor(steps.length / 2)] : 0;
    const x = [], y = [];
    for (let i = 0; i < pts.length; i++) {
        if (i && typical > 0 && pts[i][0] - pts[i - 1][0] > 3 * typical) {
            x.push(null); y.push(null);
        }
        x.push(pts[i][0]); y.push(pts[i][1]);
    }
    return { x, y };
}

function redrawPattern() {
    if (!state.angles.length || !state.grid.length) return;
    const { r, theta } = measuredCut();
    if (!r.length) return;

    const normalize = $('normalize').checked;
    const measPeak = Math.max(...r);
    const rr = normalize ? r.map((v) => v - measPeak) : r;

    // Close the trace only once the full rotation is present. The test is the
    // wrap-around gap from the last angle back to the first: for 0..355 step 5
    // that gap is 5 and the loop should close, while a 0..90 arc leaves 270 and
    // must stay open.
    const step = state.angles.length > 1
        ? Math.abs(state.angles[1] - state.angles[0]) : 360;
    const gap = (((theta[0] - theta[theta.length - 1]) % 360) + 360) % 360;
    const full = theta.length === state.angles.length && gap <= step + 1e-6;
    const rOut = full ? [...rr, rr[0]] : rr;
    const tOut = full ? [...theta, theta[0]] : theta;

    // The reference rides on the same rings. Normalized, both sit against their
    // own peak; un-normalized, the reference is shifted onto the measurement's
    // peak, which is the only way a model in dBi and a measurement in raw dB
    // can share an axis at all.
    const cut = selectedCut();
    const showRef = !!cut && $('ref-show').checked;
    let refR = [], refT = [];
    if (showRef) {
        const rot = parseFloat($('ref-rotate').value) || 0;
        const refPeak = Math.max(...cut.values);
        const shift = normalize ? -refPeak : measPeak - refPeak;
        refR = cut.values.map((v) => v + shift);
        refT = cut.angles.map((a) => a + rot);
        if (cut.angles.length > 2
            && cut.angles[cut.angles.length - 1] - cut.angles[0] >= 350) {
            refR = [...refR, refR[0]];
            refT = [...refT, refT[0]];
        }
    }

    // Which view the overlay lands on. The comparison is the same either way;
    // the answer is only about where it is easiest to read.
    const target = $('ref-target').value;
    const onPolar = showRef && target !== 'rect';
    const onRect = showRef && target !== 'polar';

    // Clamp the floor. A single bad point (a null on the noise floor, or a
    // dropped sweep) would otherwise drag the radial axis to -120 dB and
    // squash the entire pattern into the outer ring.
    const FLOOR_DB = -60;
    const span = showRef ? [...rr, ...refR] : rr;
    const lo = Math.max(Math.min(...span), FLOOR_DB);
    const range = normalize
        ? [Math.min(-40, Math.floor(lo / 10) * 10), 0]
        : [Math.floor(lo / 10) * 10 - 5, Math.ceil(Math.max(...span) / 10) * 10 + 5];

    Plotly.update('chart-polar',
        { r: [rOut, onPolar ? refR : []], theta: [tOut, onPolar ? refT : []],
          visible: [true, onPolar] },
        { 'polar.radialaxis.range': range }, [0, 1]);

    // The rectangular view shares the reference's shift and the radial range;
    // only the axis differs. Angles run along it from wherever the scan
    // started, so a 0..355 run and a -180..175 run each read as one span
    // rather than one wrapped in half.
    const base = Math.min(...theta);
    const meas = rectSeries(theta, rr, base);
    const ref = onRect ? rectSeries(refT, refR, base) : { x: [], y: [] };
    const xs = [...meas.x, ...ref.x].filter((v) => v !== null);
    // One angle in, both ends of the range are the same number and the axis
    // has nowhere to draw. Give it a degree either side until a second arrives.
    const xLo = Math.min(...xs), xHi = Math.max(...xs);
    Plotly.update('chart-pattern-rect',
        { x: [meas.x, ref.x], y: [meas.y, ref.y], visible: [true, onRect] },
        { 'yaxis.range': range,
          'xaxis.range': xHi > xLo ? [xLo, xHi] : [xLo - 1, xLo + 1] }, [0, 1]);

    const peak = Math.max(...r);
    $('stat-peak').textContent = `${peak.toFixed(2)} dB`;
    const lastIdx = theta.length - 1;
    $('stat-cut').textContent = `${r[lastIdx].toFixed(2)} dB`;
    updateDelta();
}

function redrawRect() {
    if (!state.lastTrace || !state.freqs.length) return;
    Plotly.update('chart-rect', {
        x: [state.freqs.map((f) => f / 1e9)],
        y: [state.lastTrace],
    });
    $('trace-label').textContent = state.lastAngle === null
        ? 'latest cut' : `cut at ${state.lastAngle.toFixed(1)}°`;
}

function populateCutFreqs() {
    const sel = $('cut-freq');
    sel.innerHTML = '';
    state.freqs.forEach((f, i) => {
        const o = document.createElement('option');
        o.value = i;
        o.textContent = `${(f / 1e9).toFixed(4)} GHz`;
        sel.appendChild(o);
    });
    state.cutIndex = Math.floor(state.freqs.length / 2);
    sel.value = state.cutIndex;
}

// ---------------------------------------------------------------------------
// Imported reference
// ---------------------------------------------------------------------------
function selectedCut() {
    if (!reference.cuts || !reference.key) return null;
    return reference.cuts.get(reference.key) ?? null;
}

/** RMS deviation of the live cut from the reference. */
function updateDelta() {
    const cut = selectedCut();
    $('stat-box-delta').hidden = !cut;
    if (!cut) return;
    const { r, theta } = measuredCut();
    if (!r.length) { $('stat-delta').textContent = '—'; return; }

    const stats = compare(theta, r, cut, {
        normalize: $('ref-normalize').checked,
        rotateDeg: parseFloat($('ref-rotate').value) || 0,
    });
    $('stat-delta').textContent = stats ? `${stats.rms.toFixed(2)} dB` : 'no overlap';
    $('stat-box-delta').title = stats
        ? `RMS ${stats.rms.toFixed(2)} dB over ${stats.n} angles, ` +
          `down to ${stats.floorDb} dB from peak; ` +
          `worst ${stats.max >= 0 ? '+' : ''}${stats.max.toFixed(2)} dB ` +
          `at ${stats.at.toFixed(1)}°`
        : 'the reference does not cover the measured angles';
}

function applyReference() {
    redrawPattern();
    updateDelta();
}

function fillCutPickers() {
    const params = [...new Set([...reference.cuts.values()].map((c) => c.param))];
    const sel = $('ref-param');
    sel.innerHTML = '';
    for (const p of params) {
        const o = document.createElement('option');
        o.value = p;
        o.textContent = p;
        sel.appendChild(o);
    }
    fillPlanePicker();
}

/**
 * The cut planes the file holds for the parameter in hand.
 *
 * Hidden when there is only one, because a picker with a single answer is not
 * a question. Shown the moment a file distinguishes its cuts, which is the
 * point of the thing: a modelled elevation cut differenced against a measured
 * azimuth cut produces a number that is about nothing at all.
 */
function fillPlanePicker() {
    const param = $('ref-param').value;
    const planes = planesOf(
        new Map([...reference.cuts].filter(([, c]) => c.param === param)));
    const sel = $('ref-plane');
    const prev = sel.value;
    sel.innerHTML = '';
    for (const plane of planes) {
        const o = document.createElement('option');
        o.value = plane;
        o.textContent = plane || 'the only cut in the file';
        sel.appendChild(o);
    }
    sel.value = planes.includes(prev) ? prev : (planes[0] ?? '');
    $('ref-plane-group').hidden = planes.length < 2;
    fillFreqPicker();
}

function fillFreqPicker() {
    const param = $('ref-param').value;
    const plane = $('ref-plane').value;
    const sel = $('ref-freq');
    sel.innerHTML = '';
    let first = null;
    for (const [key, cut] of reference.cuts) {
        if (cut.param !== param || cut.plane !== plane) continue;
        const o = document.createElement('option');
        o.value = key;
        o.textContent = cut.freqHz ? `${(cut.freqHz / 1e9).toFixed(4)} GHz` : 'no frequency';
        sel.appendChild(o);
        if (first === null) first = key;
    }
    // Default to the cut nearest the frequency this run is cutting at, which is
    // almost always the one worth comparing.
    let want = first;
    if (state.freqs.length) {
        const target = state.freqs[state.cutIndex];
        let best = Infinity;
        for (const [key, cut] of reference.cuts) {
            if (cut.param !== param || cut.plane !== plane || !cut.freqHz) continue;
            const d = Math.abs(cut.freqHz - target);
            if (d < best) { best = d; want = key; }
        }
    }
    reference.key = want;
    if (want) sel.value = want;
}

async function loadReference(file) {
    try {
        const { cuts, rows, planes } = parsePattern(await file.text());
        reference = { name: file.name, cuts, key: null };
        $('ref-name').textContent = file.name;
        $('ref-summary').hidden = false;
        fillCutPickers();
        applyReference();
        // Say how the file was read. Which plane the comparison is against is
        // the one thing an import can get silently wrong, so it is stated
        // rather than left to be noticed.
        const how = planes.length > 1
            ? `${planes.length} cut planes (${planes.join(', ')})`
            : planes[0] ? `cut plane ${planes[0]}` : 'one cut plane';
        addLog('info', 'sim',
               `imported ${file.name}: ${rows} rows, ${cuts.size} cut(s), ${how}`);
        const picked = selectedCut();
        if (planes.length > 1 && picked) {
            addLog('warn', 'sim',
                   `comparing against ${cutLabel(picked)} \u2014 pick the plane the tower actually cut in`);
        }
    } catch (e) {
        addLog('error', 'sim', `${file.name}: ${e.message}`);
    }
}

function clearReference() {
    reference = { name: '', cuts: null, key: null };
    $('ref-file').value = '';
    $('ref-summary').hidden = true;
    $('ref-plane-group').hidden = true;
    applyReference();
    addLog('info', 'sim', 'reference cleared');
}

// ---------------------------------------------------------------------------
// Log
// ---------------------------------------------------------------------------
function addLog(level, source, message) {
    const el = $('log');
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    const line = document.createElement('div');
    line.className = `log-line ${level}`;
    const t = new Date().toLocaleTimeString([], { hour12: false });
    line.innerHTML =
        `<span class="log-time"></span><span class="log-src"></span><span class="log-msg"></span>`;
    line.children[0].textContent = t;
    line.children[1].textContent = source;
    line.children[2].textContent = message;
    el.appendChild(line);
    while (el.childElementCount > 500) el.removeChild(el.firstChild);
    if (atBottom) el.scrollTop = el.scrollHeight;
}

// ---------------------------------------------------------------------------
// UI state
// ---------------------------------------------------------------------------
function setConnected(on) {
    state.connected = on;
    $('conn-dot').classList.toggle('on', on);
    $('conn-dot').title = on ? 'connected' : 'disconnected';
    $('conn-text').textContent = on ? 'connected' : 'disconnected';
    if (!on) {
        $('mode-badge').textContent = 'offline';
        $('mode-badge').removeAttribute('data-mode');
        $('corr-badge').hidden = true;
        $('vna-corr').textContent = '—';
    }
    syncControls();
}

/**
 * Read a CORR:STAT? reply the way acquisition-side `correction_is_on` does:
 * true / false / null-for-unknown. An instrument that did not answer is not
 * the same as one that answered "off".
 */
function correctionIsOn(raw) {
    if (raw == null) return null;
    const s = String(raw).trim().toUpperCase();
    if (s === '1' || s === '+1' || s === 'ON' || s === 'TRUE') return true;
    if (s === '0' || s === '+0' || s === 'OFF' || s === 'FALSE') return false;
    return null;
}

/**
 * Show whether the VNA is running against a calibration.
 *
 * Deliberately not an error state. An uncalibrated sweep is still a sweep, and
 * plenty of alignment work is done with correction off on purpose - so this
 * warns, in the corner, and never blocks. In sim there is no calibration to
 * report at all: the backend says so with "n/a" and the badge stays down
 * rather than inventing a green light behind synthesized data.
 */
function setCorrection(raw, mode) {
    const on = correctionIsOn(raw);
    const kv = $('vna-corr');
    const badge = $('corr-badge');
    if (!kv || !badge) return;

    if (mode === 'sim' || raw == null || raw === '' || raw === 'n/a') {
        kv.textContent = mode === 'sim' ? 'n/a (simulated)' : '—';
        badge.hidden = true;
        return;
    }
    badge.hidden = false;
    if (on === true) {
        kv.textContent = `on (${raw})`;
        badge.textContent = 'cal';
        badge.className = 'status-pill corr-on';
        badge.title = 'error correction on - this run is calibrated';
    } else if (on === false) {
        kv.textContent = `off (${raw})`;
        badge.textContent = 'uncal';
        badge.className = 'status-pill corr-off';
        badge.title = 'error correction off - this run is uncalibrated';
    } else {
        kv.textContent = `unknown (${raw})`;
        badge.textContent = 'cal?';
        badge.className = 'status-pill corr-unknown';
        badge.title = 'the VNA did not answer SENS:CORR:STAT?';
    }
}

// ---------------------------------------------------------------------------
// Calibration
//
// A wizard, not a button. Running an AutoCal means somebody walks into the
// chamber, unmates the horn and the AUT, mates the ACM across the cable ends
// and comes back out - so the UI's job is to state the procedure, take an
// explicit acknowledgement that it has been done, and refuse to pretend the
// cancel button can do more than it can.
// ---------------------------------------------------------------------------

function calParams() {
    return {
        start_hz: parseFloat($('f-start').value) * 1e9,
        stop_hz: parseFloat($('f-stop').value) * 1e9,
        points: parseInt($('f-points').value, 10),
        if_bw_hz: parseFloat($('f-ifbw').value),
        power_dbm: parseFloat($('f-power').value),
        parameter: $('f-param').value,
        ports: $('cal-ports').value.split(',').map(Number),
        reference_plane: $('cal-plane').value,
    };
}

/** The checklist depends on how many ports are being calibrated: a 1-port needs
 *  one cable end on the module, not both, and saying "mate it across the two
 *  ends" for a 1-port sends somebody to disconnect an antenna for no reason. */
function renderCalChecklist() {
    const one = $('cal-ports').value.split(',').length === 1;
    const port = $('cal-ports').value.split(',')[0];
    $('cal-step-unmate').textContent = one
        ? `Unmate whatever is on the port ${port} cable end.`
        : 'Unmate the transmit horn and the AUT from the cable ends.';
    $('cal-step-mate').textContent = one
        ? `Mate one port of the ACM2202 to that end.`
        : 'Mate the ACM2202 across those two ends.';
}

/** Which sweep settings a prospective run does not share with the cal. Mirrors
 *  cal_mismatch() in the service, so the panel says the same thing the run's
 *  meta.json will. */
function calDrift(record) {
    if (!record?.sweep) return [];
    const want = calParams();
    const label = { start_hz: 'start', stop_hz: 'stop', points: 'points',
                    if_bw_hz: 'IF BW', power_dbm: 'power' };
    return Object.keys(label)
        .filter((k) => Math.abs(Number(record.sweep[k]) - Number(want[k])) > 1e-6)
        .map((k) => label[k]);
}

function calAgeText(record) {
    const t = Date.parse((record.taken_at || '').replace(' ', 'T'));
    if (!Number.isFinite(t)) return record.taken_at || 'unknown date';
    const days = Math.floor((Date.now() - t) / 86400000);
    if (days <= 0) return 'today';
    return days === 1 ? 'yesterday' : `${days} days ago`;
}

/** The one-line summary in the VNA panel, plus the drift warning under it. */
function renderCalibration() {
    const rec = state.calibration;
    const summary = $('cal-summary');
    const warn = $('cal-drift');
    if (!rec) {
        summary.textContent = 'none recorded';
        warn.hidden = true;
        return;
    }
    const span = `${(rec.sweep.start_hz / 1e9).toFixed(3)}–`
               + `${(rec.sweep.stop_hz / 1e9).toFixed(3)} GHz`;
    summary.textContent = `${calAgeText(rec)}, ${span}`
        + (rec.mode === 'sim' ? ' (simulated)' : '');

    const drift = calDrift(rec);
    if (drift.length) {
        warn.hidden = false;
        warn.textContent = `The sweep set above differs from the calibration `
            + `(${drift.join(', ')}). A run taken this way is corrected by `
            + `interpolation at best. Recalibrate, or set the sweep back.`;
    } else if (rec.mode === 'sim') {
        warn.hidden = false;
        warn.textContent = 'Simulated calibration — nothing was corrected.';
    } else {
        warn.hidden = true;
    }
}

const CAL_STAGES = ['setup', 'run', 'done'];

function calStage(which) {
    CAL_STAGES.forEach((s) => { $(`cal-stage-${s}`).hidden = s !== which; });
    const running = which === 'run';
    $('cal-go').hidden = which !== 'setup';
    $('cal-ack').disabled = running;
    // "Cancel" while running means stop after the current step, and while
    // finished means close. Saying so on the button is the whole point.
    $('cal-cancel').textContent = running ? 'Stop after this step'
                                : which === 'done' ? 'Close' : 'Cancel';
}

function openCalModal() {
    const p = calParams();
    $('cal-span').textContent = `${(p.start_hz / 1e9).toFixed(3)}–`
                              + `${(p.stop_hz / 1e9).toFixed(3)} GHz`;
    $('cal-pts').textContent = `${p.points} pts / ${p.if_bw_hz} Hz`;
    $('cal-pow').textContent = `${p.power_dbm} dBm`;
    $('cal-ack').checked = false;
    $('cal-go').disabled = true;
    $('cal-log').innerHTML = '';
    calStage('setup');
    $('cal-modal').hidden = false;
}

function closeCalModal() {
    $('cal-modal').hidden = true;
}

function calLog(text) {
    const line = document.createElement('div');
    line.className = 'log-line info';
    line.textContent = text;
    $('cal-log').appendChild(line);
    $('cal-log').scrollTop = $('cal-log').scrollHeight;
}

function applyState(s) {
    if (!s) return;
    $('mode-badge').textContent = s.mode === 'sim' ? 'simulated' : 'hardware';
    $('mode-badge').dataset.mode = s.mode;
    state.mode = s.mode;
    $('vna-idn').textContent = s.vna_idn || '—';
    state.hasPositioner = s.has_positioner !== false;
    $('pos-idn').textContent = state.hasPositioner
        ? (s.pos_idn || '—') : 'not attached (--no-positioner)';
    $('pos-err').textContent = state.hasPositioner ? (s.latched_error ?? '—') : '—';
    applyPositionerPresence();
    setCorrection(s.correction, s.mode);
    state.calibrating = !!s.calibrating;
    state.sweeping = !!s.sweeping;
    state.calibration = s.calibration || null;
    renderCalibration();
    if (s.angle != null) setPosition(s.angle);
    if (s.speed != null) $('speed-readout').textContent = `${s.speed.toFixed(0)}%`;
    if (s.error) addLog('error', 'state', s.error);
    state.scanning = !!s.scanning;
    syncControls();
}

/** One place for "the axis is here", since three frames can report it. */
function setPosition(deg) {
    $('angle-readout').textContent = `${deg.toFixed(1)}°`;
    $('stat-angle').textContent = `${deg.toFixed(1)}°`;
    dial.setData({ actual: deg });
    dial.draw();
}

const SCAN_INPUTS = ['f-start', 'f-stop', 'f-points', 'f-ifbw', 'f-power', 'f-param',
                     'a-start', 'a-stop', 'a-step', 'a-speed', 'jog-abs', 'btn-goto',
                     'btn-zero', 'run-name'];


/**
 * Grey out everything that turns the tower when there is no tower.
 *
 * Disabled and explained, not hidden: a control that vanishes looks like a
 * missing feature, and the operator has no way to tell that from a broken UI.
 */
function applyPositionerPresence() {
    const none = !state.hasPositioner;
    const POS_CONTROLS = ['a-start', 'a-stop', 'a-step', 'a-speed', 'jog-abs',
                          'btn-goto', 'btn-zero', 'btn-scan'];
    if (none) {
        POS_CONTROLS.forEach((id) => { const el = $(id); if (el) el.disabled = true; });
        document.querySelectorAll('[data-jog]').forEach((b) => { b.disabled = true; });
        $('btn-scan').textContent = 'no positioner';
        $('btn-scan').title = 'The service was started with --no-positioner. '
                            + 'Sweeps work; nothing can turn the tower.';
    }
}

function syncControls() {
    const busy = state.scanning || state.calibrating || state.sweeping
              || !state.connected;
    SCAN_INPUTS.forEach((id) => { const el = $(id); if (el) el.disabled = busy; });
    document.querySelectorAll('[data-jog]').forEach((b) => { b.disabled = busy; });
    $('btn-scan').disabled = busy;
    $('btn-scan').textContent = state.scanning ? 'Scanning…'
                             : state.calibrating ? 'Calibrating…'
                             : state.sweeping ? 'Sweeping…' : 'Start scan';
    $('btn-cal').disabled = busy;
    $('btn-sweep').disabled = busy;
    $('btn-sweep').textContent = state.sweeping ? 'Sweeping…' : 'Sweep';
    // A sweep never touches the tower, so it stays available on a rig that
    // has none. Everything that turns something does not.
    if (!state.hasPositioner) applyPositionerPresence();
    // Stop stays enabled whenever there is a link: it is the one control that
    // must always be reachable, and the service preempts rather than queues.
    $('btn-stop').disabled = !state.connected;

    const pill = $('pill-motion');
    pill.textContent = state.scanning ? 'scanning'
                     : state.sweeping ? 'sweeping' : 'stopped';
    pill.className = `status-pill${state.scanning ? ' scanning' : ''}`;
}

/**
 * The angle grid the service will produce for the current form values.
 *
 * Mirrors pm.ScanConfig.angles(), including the full-circle trim, so the dial
 * can show the planned grid before a scan starts and the count never disagrees
 * with what comes back.
 */
function plannedAngles() {
    const a0 = parseFloat($('a-start').value);
    const a1 = parseFloat($('a-stop').value);
    const st = parseFloat($('a-step').value);
    if (!(st > 0) || !(a1 >= a0)) return null;
    let n = Math.floor((a1 - a0) / st + 1e-9) + 1;
    if (n > 1 && Math.abs(((st * (n - 1)) % 360)) < 1e-9) n -= 1;
    return Array.from({ length: n }, (_, i) => a0 + st * i);
}

function updateAngleCount() {
    const a = plannedAngles();
    if (!a) { $('angle-count').textContent = 'invalid range'; return; }
    $('angle-count').textContent = `${a.length} angle${a.length === 1 ? '' : 's'}`;
    $('stat-grid').textContent = a.length
        ? `${a[0].toFixed(0)}…${a[a.length - 1].toFixed(0)}°` : '—';
    if (!state.scanning) {
        $('stat-remaining').textContent = `${a.length}`;
        dial.setData({ grid: a, done: 0 });
        dial.draw();
    }
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------
const transport = createTransport({
    onLog: addLog,
    onOpen: () => { setConnected(true); transport.getState().then(applyState).catch(() => {}); refreshRuns(); },
    onClose: () => setConnected(false),
    onState: applyState,
    onPosition: (deg) => setPosition(deg),
    onCalStarted: (m) => {
        state.calibrating = true;
        calStage('run');
        $('cal-status').textContent = 'Calibrating…';
        calLog(`ports ${m.ports.join(' and ')}, plane: ${m.reference_plane || '—'}`);
        syncControls();
    },
    onCalStep: (m) => calLog(m.message),
    onSweepStarted,
    onSweepTrace,
    onSweepDone,
    onCalDone: (m) => {
        state.calibrating = false;
        calStage('done');
        if (m.ok) {
            state.calibration = m.record;
            renderCalibration();
            $('cal-result').textContent = m.record?.mode === 'sim'
                ? 'Simulated calibration recorded. Nothing was corrected.'
                : 'Calibration applied and recorded.';
        } else {
            $('cal-result').textContent = m.cancelled
                ? `Stopped. ${m.error || ''}`
                : `Calibration failed: ${m.error || 'unknown error'}`;
        }
        syncControls();
    },
    onScanStarted: (m) => {
        state.scanning = true;
        setCorrection(m.correction, state.mode);
        if (m.cal_mismatch?.length) {
            addLog('warn', 'vna',
                   `this run does not match the calibration: ${m.cal_mismatch.join('; ')}`);
        }
        state.angles = m.angles;
        state.freqs = m.freqs;
        state.grid = new Array(m.angles.length).fill(null);
        state.done = 0;
        populateCutFreqs();
        $('progress-fill').style.width = '0%';
        $('progress-label').textContent = `0 / ${m.angles.length}`;
        $('stat-grid').textContent = m.angles.length
            ? `${m.angles[0].toFixed(0)}…${m.angles[m.angles.length - 1].toFixed(0)}°` : '—';
        $('stat-remaining').textContent = `${m.angles.length}`;
        dial.setData({ grid: m.angles, done: 0, commanded: null });
        dial.draw();
        // A reference picked before the sweep was known can now choose the cut
        // nearest this run's frequency.
        if (reference.cuts) fillFreqPicker();
        syncControls();
    },
    onScanPoint: (m) => {
        state.grid[m.index] = m.mag_db;
        state.lastTrace = m.mag_db;
        state.lastAngle = m.angle_actual;
        state.commanded = m.angle_cmd;
        state.done = m.index + 1;
        const n = state.angles.length;
        $('progress-fill').style.width = `${(state.done / n) * 100}%`;
        $('progress-label').textContent = `${state.done} / ${n}`;
        $('stat-cmd').textContent = `${m.angle_cmd.toFixed(1)}°`;
        $('stat-remaining').textContent = `${Math.max(0, n - state.done)}`;
        dial.setData({ done: state.done, commanded: m.angle_cmd });
        redrawPattern();
        redrawRect();
    },
    onScanDone: (m) => {
        state.scanning = false;
        $('progress-label').textContent = m.cancelled
            ? `cancelled${m.n_done != null ? ` at ${m.n_done}` : ''}` : 'complete';
        if (m.error) addLog('error', 'scan', m.error);
        dial.setData({ commanded: null });
        dial.draw();
        syncControls();
        refreshRuns();
    },
});

// ---------------------------------------------------------------------------
// Chrome: accordion, icon rail, tabs, theme
// ---------------------------------------------------------------------------

/* Icons match the collapsed rail, so a section is recognizable in either
 * state. */
const sectionIcons = [
    // 0. VNA
    '<svg viewBox="0 0 24 24" style="width:100%;height:100%;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;"><path d="M3.5 6.5h17v11h-17z"></path><path d="M6 14c1.6 0 1.6-4 3.2-4s1.6 4 3.2 4 1.6-4 3.2-4 1.6 4 3.2 4"></path></svg>',
    // 1. Turntable
    '<svg viewBox="0 0 24 24" style="width:100%;height:100%;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;"><circle cx="12" cy="12" r="7.5"></circle><circle cx="12" cy="12" r="1.6"></circle><path d="M12 4.5V2.6"></path><path d="M18.6 8.4a7.5 7.5 0 0 1-2.2 9.6"></path><path d="M16.6 15.4l-.2 2.6 2.6-.3"></path></svg>',
    // 2. Simulation
    '<svg viewBox="0 0 24 24" style="width:100%;height:100%;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;"><path d="M3.5 18.5h17"></path><path d="M4.5 15c2.5 0 3.2-8 5.7-8s3.2 8 5.7 8 2.3-4 3.6-4"></path><path d="M4.5 18c2.5 0 3.6-5.5 5.7-5.5s3.6 5.5 5.7 5.5" stroke-dasharray="2.4 2.2"></path></svg>',
    // 3. Output
    '<svg viewBox="0 0 24 24" style="width:100%;height:100%;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;"><path d="M5 4.5h9.5L19 9v10.5H5z"></path><path d="M14 4.5V9h5"></path><path d="M8.5 13.5h7M8.5 16.5h4.5"></path></svg>',
];

const accordionItems = [...document.querySelectorAll('.accordion-item')];

function markRailActive(idx) {
    document.querySelectorAll('.sidebar-icon-btn[data-section]').forEach((btn) => {
        btn.classList.toggle('active', parseInt(btn.dataset.section, 10) === idx);
    });
}

function wireChrome() {
    document.querySelectorAll('.accordion-icon[data-icon]').forEach((el) => {
        const i = parseInt(el.getAttribute('data-icon'), 10);
        if (sectionIcons[i]) el.innerHTML = sectionIcons[i];
    });

    accordionItems.forEach((item, idx) => {
        item.querySelector('.accordion-header').addEventListener('click', () => {
            item.classList.toggle('active');
            if (item.classList.contains('active')) {
                markRailActive(idx);
                // Bring the section the click just opened to the top, or its
                // body opens below the fold on a short sidebar.
                setTimeout(() => {
                    const acc = $('accordionSettings');
                    acc.scrollTop += item.getBoundingClientRect().top
                                  - acc.getBoundingClientRect().top;
                }, 120);
            }
        });
    });
    markRailActive(accordionItems.findIndex((i) => i.classList.contains('active')));

    const setCollapsed = (collapsed, openSection = null) => {
        $('settings-panel').classList.toggle('collapsed', collapsed);
        $('dashboard').classList.toggle('settings-collapsed', collapsed);
        if (openSection !== null) {
            accordionItems.forEach((it, i) => it.classList.toggle('active', i === openSection));
            markRailActive(openSection);
        }
        requestAnimationFrame(resizeAll);
    };
    $('btn-toggle-settings').addEventListener('click', () => setCollapsed(true));
    $('btn-toggle-settings-icon').addEventListener('click', () => setCollapsed(false));
    document.querySelectorAll('.sidebar-icon-btn[data-section]').forEach((btn) => {
        btn.addEventListener('click', () =>
            setCollapsed(false, parseInt(btn.dataset.section, 10)));
    });

    // Panes stay laid out while hidden — a Plotly chart that measures zero
    // while its tab is inactive comes back the wrong size.
    document.querySelectorAll('.tab-btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
            btn.classList.add('active');
            $(btn.dataset.target).classList.add('active');
            requestAnimationFrame(resizeAll);
        });
    });
}

/* Theme: system unless told otherwise, and the button says which of the three
 * it is on rather than what the next click does. */
const THEME_KEY = 'chamber-theme';
const THEME_ORDER = ['system', 'light', 'dark'];
const media = window.matchMedia('(prefers-color-scheme: dark)');
let themeMode = 'system';

function applyTheme(mode, persist = true) {
    themeMode = THEME_ORDER.includes(mode) ? mode : 'system';
    const resolved = themeMode === 'system' ? (media.matches ? 'dark' : 'light') : themeMode;
    document.documentElement.dataset.theme = resolved;
    if (persist) localStorage.setItem(THEME_KEY, themeMode);

    const label = { system: 'System theme', light: 'Light mode', dark: 'Dark mode' }[themeMode];
    const glyph = { system: '◐', light: '☀', dark: '☾' }[themeMode];
    const next = THEME_ORDER[(THEME_ORDER.indexOf(themeMode) + 1) % THEME_ORDER.length];
    const title = `Theme: ${themeMode} — click for ${next}`;

    const btn = $('theme-toggle');
    btn.querySelector('.theme-icon').textContent = glyph;
    btn.querySelector('.theme-label').textContent = label;
    btn.title = title;
    btn.setAttribute('aria-label', title);

    const icon = $('btn-theme-icon');
    icon.title = title;
    icon.setAttribute('aria-label', title);
    icon.querySelector('.icon-system').style.display = themeMode === 'system' ? 'block' : 'none';
    icon.querySelector('.icon-sun').style.display = themeMode === 'light' ? 'block' : 'none';
    icon.querySelector('.icon-moon').style.display = themeMode === 'dark' ? 'block' : 'none';

    restylePlots();
}

function cycleTheme() {
    applyTheme(THEME_ORDER[(THEME_ORDER.indexOf(themeMode) + 1) % THEME_ORDER.length]);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
async function guard(fn) {
    try { return await fn(); }
    catch (e) { addLog('error', 'cmd', e.message); }
}

function scanParams() {
    const p = {
        start_deg: parseFloat($('a-start').value),
        stop_deg: parseFloat($('a-stop').value),
        step_deg: parseFloat($('a-step').value),
        start_hz: parseFloat($('f-start').value) * 1e9,
        stop_hz: parseFloat($('f-stop').value) * 1e9,
        points: parseInt($('f-points').value, 10),
        if_bw_hz: parseFloat($('f-ifbw').value),
        power_dbm: parseFloat($('f-power').value),
        parameter: $('f-param').value,
    };
    // Left empty, the key is omitted entirely so the service stamps its own
    // name; an empty string would create a directory called "".
    const name = $('run-name').value.trim();
    if (name) p.name = name;
    return p;
}

/**
 * The sweep takes its frequency settings from the same VNA panel a scan does,
 * so a capture and a scan cannot silently disagree about what was measured.
 * The Parameter dropdown is not consulted: a capture takes all four.
 */
function sweepParams() {
    return {
        // One sweep per parameter, so asking for four when you want one costs
        // four times the wait. An S11-only measurement on a single antenna is
        // the common case and there is no port 2 to measure anyway.
        parameters: $('sweep-params').value.split(','),
        start_hz: parseFloat($('f-start').value) * 1e9,
        stop_hz: parseFloat($('f-stop').value) * 1e9,
        points: parseInt($('f-points').value, 10),
        if_bw_hz: parseFloat($('f-ifbw').value),
        power_dbm: parseFloat($('f-power').value),
    };
}

function refreshRuns() {
    guard(() => transport.listRuns()).then((r) => renderRuns(r?.runs || []));
}

function renderRuns(runs) {
    const box = $('runs-list');
    box.innerHTML = '';
    if (!runs.length) {
        box.innerHTML = '<p class="hint">No stored runs yet.</p>';
        return;
    }
    for (const meta of runs) {
        const row = document.createElement('button');
        row.className = 'run-row';
        row.title = `Load ${meta.name}`;
        const name = document.createElement('span');
        name.textContent = meta.name;
        const detail = document.createElement('span');
        detail.className = 'run-meta';
        detail.textContent = `${meta.n_angles ?? '?'}×${meta.n_freqs ?? '?'}`;
        row.append(name, detail);
        row.addEventListener('click', () => loadRun(meta.name));
        box.appendChild(row);
    }
}

async function loadRun(name) {
    const d = await guard(() => transport.loadRun(name));
    if (!d) return;
    state.angles = d.angles;
    state.freqs = d.freqs;
    state.grid = d.mag_db;
    state.done = d.angles.length;
    populateCutFreqs();
    if (reference.cuts) fillFreqPicker();
    state.lastTrace = d.mag_db[d.mag_db.length - 1];
    state.lastAngle = d.angles[d.angles.length - 1];
    $('progress-label').textContent = 'loaded';
    $('progress-fill').style.width = '100%';
    $('stat-remaining').textContent = '0';
    dial.setData({ grid: d.angles, done: d.angles.length, commanded: null });
    dial.draw();
    redrawPattern();
    redrawRect();
    addLog('info', 'runs', `loaded ${name}: ${d.angles.length} angles`);
}


// ---------------------------------------------------------------------------
// VNA-only sweep: Smith chart, magnitude, and the impedance behind a marker.
//
// The service ships complex S-parameters and nothing else. Everything derived -
// dB, impedance, VSWR, return loss - is computed here, so the payload stays a
// measurement rather than a measurement plus somebody's arithmetic.
// ---------------------------------------------------------------------------

function db(re, im) {
    return 20 * Math.log10(Math.max(Math.hypot(re, im), 1e-15));
}

/**
 * Load impedance behind a reflection coefficient: Z = Z0 (1 + G) / (1 - G).
 *
 * A short is G = -1, where the denominator is zero and Z is genuinely zero, so
 * the singular case is real rather than a rounding artefact and is reported as
 * such instead of as Infinity.
 */
function gammaToZ(re, im) {
    const dr = 1 - re, di = -im;
    const den = dr * dr + di * di;
    if (den < 1e-18) return { r: Infinity, x: Infinity };
    const nr = 1 + re, ni = im;
    return {
        r: Z0 * (nr * dr + ni * di) / den,
        x: Z0 * (ni * dr - nr * di) / den,
    };
}

function fmtOhms(z) {
    if (!Number.isFinite(z.r) || !Number.isFinite(z.x)) return 'open';
    const sign = z.x >= 0 ? '+' : '−';
    return `${z.r.toFixed(1)} ${sign} j${Math.abs(z.x).toFixed(1)}`;
}

function populateSweepMarker() {
    const sel = $('sweep-marker');
    const f = state.sweepFreqs;
    sel.innerHTML = '';
    f.forEach((hz, i) => {
        const o = document.createElement('option');
        o.value = String(i);
        o.textContent = `${(hz / 1e9).toFixed(4)} GHz`;
        sel.appendChild(o);
    });
    if (state.sweepMarker >= f.length) state.sweepMarker = Math.max(0, f.length - 1);
    sel.value = String(state.sweepMarker);
}

function redrawSweep() {
    const f = state.sweepFreqs;
    if (!f.length) return;
    const ghz = f.map((hz) => hz / 1e9);

    // Magnitude: one trace per parameter, empty for any not captured.
    Plotly.update('chart-sweep-mag', {
        x: SWEEP_PARAMS.map(() => ghz),
        y: SWEEP_PARAMS.map((name) => {
            const d = state.sweepData[name];
            return d ? d.re.map((re, i) => db(re, d.im[i])) : [];
        }),
    }, {}, SWEEP_PARAMS.map((_, i) => i));

    // Smith: reflection parameters plus the marker point.
    const i = state.sweepMarker;
    const primary = state.sweepData.S11 || state.sweepData.S22;
    Plotly.update('chart-smith', {
        real: REFLECTION.map((n) => (state.sweepData[n] || { re: [] }).re)
            .concat([primary ? [primary.re[i]] : []]),
        imag: REFLECTION.map((n) => (state.sweepData[n] || { im: [] }).im)
            .concat([primary ? [primary.im[i]] : []]),
    }, {}, [0, 1, 2]);

    renderSweepReadout();
}

function renderSweepReadout() {
    const i = state.sweepMarker;
    const f = state.sweepFreqs;
    // S11 is the readout's subject; S22 only if S11 was not captured. Averaging
    // the two would describe a port that does not exist.
    const d = state.sweepData.S11 || state.sweepData.S22;
    const blank = !d || !f.length;
    $('sw-freq').textContent = blank ? '—' : `${(f[i] / 1e9).toFixed(4)} GHz`;
    if (blank) {
        ['sw-z', 'sw-gamma', 'sw-vswr', 'sw-rl'].forEach((id) => {
            $(id).textContent = '—';
        });
        return;
    }
    const re = d.re[i], im = d.im[i];
    const mag = Math.hypot(re, im);
    const z = gammaToZ(re, im);
    $('sw-z').textContent = `${fmtOhms(z)} Ω`;
    $('sw-gamma').textContent = mag.toFixed(4);
    // A passive load cannot reflect more than it receives. Measured slightly
    // over 1 means noise or a stale calibration, and quoting a negative VSWR
    // from it would dress that up as a number.
    $('sw-vswr').textContent = mag >= 1 ? '∞' : ((1 + mag) / (1 - mag)).toFixed(2);
    $('sw-rl').textContent = `${(-20 * Math.log10(Math.max(mag, 1e-15))).toFixed(2)} dB`;
}

/**
 * Touchstone .s2p, written in the browser from what is already here.
 *
 * Deliberately not the service's _write_s2p: that one zeroes three of the four
 * parameters because a scan only ever measures one, and writing a file that
 * claims a full 2-port set it does not have is worse than not writing one.
 * A capture has all four, so this is a real .s2p.
 */
function exportTouchstone() {
    const f = state.sweepFreqs;
    if (!f.length) return;

    // A capture of one reflection parameter is a 1-port measurement, and
    // Touchstone has a format for that. Writing it as .s2p with the other
    // three columns zeroed would hand somebody a file claiming a through path
    // of exactly zero and an infinitely reflective port 2 - numbers a reader
    // will happily plot. Which parameter it is goes in the header, because
    // .s1p itself cannot say whether it is S11 or S22.
    const have = SWEEP_PARAMS.filter((n) => state.sweepData[n]);
    const onePort = have.length === 1 && REFLECTION.includes(have[0]);

    const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
    const cal = state.calibration;
    const lines = [
        `! USAFA chamber VNA capture ${stamp}`,
        `! ${state.mode === 'sim' ? 'SIMULATED - not a measurement' : 'hardware'}`,
        `! measured: ${have.join(' ')}`,
    ];
    if (cal) {
        lines.push(`! calibration: ${cal.method || 'unknown'}`
                 + ` ports ${(cal.ports || []).join('+') || '?'}`
                 + `, plane: ${cal.reference_plane || 'not stated'}`);
    } else {
        lines.push('! calibration: none recorded by this service');
    }
    if (!onePort) {
        const need = SWEEP_PARAMS.filter((n) => !state.sweepData[n]);
        lines.push(need.length
            ? `! NOT MEASURED, written as zero: ${need.join(' ')}`
            : '! all four parameters measured');
    }
    lines.push('# HZ S RI R 50');

    const at = (name, i) => {
        const d = state.sweepData[name];
        return d ? [d.re[i], d.im[i]] : [0, 0];
    };
    // Touchstone 2-port column order is S11 S21 S12 S22 - not the order a
    // reader expects, and the usual source of transposed data.
    const cols = onePort ? have : ['S11', 'S21', 'S12', 'S22'];
    f.forEach((hz, i) => {
        const vals = cols.map((n) => at(n, i).map((v) => v.toExponential(9)).join(' '));
        lines.push(`${hz.toFixed(0)} ${vals.join(' ')}`);
    });

    const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `sweep_${stamp}.${onePort ? 's1p' : 's2p'}`;
    a.click();
    URL.revokeObjectURL(a.href);
    addLog('info', 'sweep', `exported ${a.download} (${have.join(' ')})`);
}

function onSweepStarted(m) {
    state.sweeping = true;
    state.sweepFreqs = m.freqs || [];
    state.sweepData = {};
    state.sweepMarker = 0;
    populateSweepMarker();
    $('sweep-state').hidden = false;
    $('btn-sweep-export').disabled = true;
    (m.cal_mismatch || []).forEach((w) =>
        addLog('warn', 'vna', `this sweep does not match the calibration: ${w}`));
    syncControls();
}

function onSweepTrace(m) {
    state.sweepData[m.parameter] = { re: m.re, im: m.im };
    redrawSweep();
}

function onSweepDone(m) {
    state.sweeping = false;
    $('sweep-state').hidden = true;
    $('btn-sweep-export').disabled = !Object.keys(state.sweepData).length;
    if (m.error) addLog('error', 'sweep', m.error);
    else if (m.cancelled) addLog('warn', 'sweep', 'capture cancelled');
    redrawSweep();
    syncControls();
}

/** Polar or rectangular. Both charts hold the same traces; this only says
 *  which one is on screen. */
function setPatternView(view) {
    $('chart-polar').classList.toggle('active', view !== 'rect');
    $('chart-pattern-rect').classList.toggle('active', view === 'rect');
    requestAnimationFrame(resizeAll);
}

function resizeAll() {
    Plotly.Plots.resize('chart-polar');
    Plotly.Plots.resize('chart-pattern-rect');
    Plotly.Plots.resize('chart-rect');
    Plotly.Plots.resize('chart-smith');
    Plotly.Plots.resize('chart-sweep-mag');
    dial.resize();
}

function wire() {
    $('btn-scan').addEventListener('click', async () => {
        const speed = parseFloat($('a-speed').value);
        if (Number.isFinite(speed)) await guard(() => transport.setSpeed(speed));
        await guard(() => transport.startScan(scanParams()));
    });

    $('btn-stop').addEventListener('click', () => guard(() => transport.stop()));

    document.querySelectorAll('[data-jog]').forEach((b) => {
        b.addEventListener('click', () =>
            guard(() => transport.jog({ delta: parseFloat(b.dataset.jog) })));
    });

    $('btn-goto').addEventListener('click', () => {
        const v = parseFloat($('jog-abs').value);
        if (!Number.isFinite(v)) return;
        dial.setData({ commanded: v });
        dial.draw();
        guard(() => transport.jog({ deg: v }));
    });

    $('btn-sweep').addEventListener('click', () =>
        guard(() => transport.sweep(sweepParams())));
    $('btn-sweep-export').addEventListener('click', exportTouchstone);
    $('sweep-marker').addEventListener('change', (e) => {
        state.sweepMarker = parseInt(e.target.value, 10) || 0;
        redrawSweep();
    });

    $('cal-ports').addEventListener('change', () => {
        renderCalChecklist();
        renderCalibration();
    });
    $('btn-cal').addEventListener('click', () => { renderCalChecklist(); openCalModal(); });
    $('cal-close').addEventListener('click', closeCalModal);
    $('cal-ack').addEventListener('change', (e) => {
        $('cal-go').disabled = !e.target.checked;
    });
    $('cal-go').addEventListener('click', () => {
        guard(() => transport.startCal(calParams()));
    });
    $('cal-cancel').addEventListener('click', () => {
        if (state.calibrating) guard(() => transport.cancelCal());
        else closeCalModal();
    });
    // The sweep fields are what a calibration is pinned to, so the drift
    // warning has to follow them as they are typed, not only on reconnect.
    ['f-start', 'f-stop', 'f-points', 'f-ifbw', 'f-power'].forEach((id) =>
        $(id).addEventListener('input', renderCalibration));

    $('btn-zero').addEventListener('click', () => guard(() => transport.zeroHere()));
    $('btn-clear-log').addEventListener('click', () => { $('log').innerHTML = ''; });
    $('btn-refresh-runs').addEventListener('click', refreshRuns);

    $('cut-freq').addEventListener('change', (e) => {
        state.cutIndex = parseInt(e.target.value, 10);
        if (reference.cuts) fillFreqPicker();
        redrawPattern();
    });
    $('normalize').addEventListener('change', redrawPattern);

    $('ref-file').addEventListener('change', (e) => {
        const f = e.target.files?.[0];
        if (f) loadReference(f);
    });
    $('btn-ref-clear').addEventListener('click', clearReference);
    $('ref-param').addEventListener('change', () => { fillPlanePicker(); applyReference(); });
    $('ref-plane').addEventListener('change', () => { fillFreqPicker(); applyReference(); });
    $('ref-freq').addEventListener('change', (e) => {
        reference.key = e.target.value;
        applyReference();
    });
    for (const id of ['ref-show', 'ref-normalize', 'ref-rotate', 'ref-target']) {
        $(id).addEventListener('input', applyReference);
    }

    $('pattern-view').addEventListener('change', (e) => setPatternView(e.target.value));

    ['a-start', 'a-stop', 'a-step'].forEach((id) =>
        $(id).addEventListener('input', updateAngleCount));

    $('theme-toggle').addEventListener('click', cycleTheme);
    $('btn-theme-icon').addEventListener('click', cycleTheme);
    // Follow the OS while nobody has overridden it — including a switch made
    // while the page is open, which is what "follow system" has to mean.
    media.addEventListener('change', () => { if (themeMode === 'system') applyTheme('system', false); });

    window.addEventListener('resize', resizeAll);
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
initPlots();
wireChrome();
wire();
applyTheme(localStorage.getItem(THEME_KEY) || 'system', false);
updateAngleCount();
setConnected(false);
transport.connect();

// Exposed for the screenshot harness and for poking at state from devtools.
window.__chamber = { state, transport, dial, applyTheme };
