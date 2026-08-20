import Plotly from 'plotly.js-dist-min';
import './style.css';
import { createTransport } from './transport.js';

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Scan state. `grid` is angle-major: grid[i] is the full trace for angles[i].
// ---------------------------------------------------------------------------
const state = {
    connected: false,
    scanning: false,
    angles: [],
    freqs: [],
    grid: [],
    cutIndex: 0,
    lastTrace: null,
    lastAngle: null,
};

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
    Plotly.newPlot('chart-polar', [{
        type: 'scatterpolar', mode: 'lines', r: [], theta: [],
        line: { color: c.primary, width: 2 },
    }], {
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
}

function restylePlots() {
    const c = themeColors();
    Plotly.relayout('chart-polar', {
        'font.color': c.muted,
        'polar.radialaxis.gridcolor': c.grid,
        'polar.radialaxis.linecolor': c.grid,
        'polar.angularaxis.gridcolor': c.grid,
        'polar.angularaxis.linecolor': c.grid,
    });
    Plotly.restyle('chart-polar', { 'line.color': c.primary });
    Plotly.relayout('chart-rect', {
        'font.color': c.muted,
        'xaxis.gridcolor': c.grid, 'xaxis.zerolinecolor': c.grid,
        'yaxis.gridcolor': c.grid, 'yaxis.zerolinecolor': c.grid,
    });
    Plotly.restyle('chart-rect', { 'line.color': c.secondary });
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function redrawPolar() {
    if (!state.angles.length || !state.grid.length) return;
    const k = state.cutIndex;
    const r = [], theta = [];
    for (let i = 0; i < state.angles.length; i++) {
        const row = state.grid[i];
        if (!row) continue;                       // angle not measured yet
        r.push(row[k]);
        theta.push(state.angles[i]);
    }
    if (!r.length) return;

    let rr = r;
    if ($('normalize').checked) {
        const peak = Math.max(...r);
        rr = r.map((v) => v - peak);
    }
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

    // Clamp the floor. A single bad point (a null on the noise floor, or a
    // dropped sweep) would otherwise drag the radial axis to -120 dB and
    // squash the entire pattern into the outer ring.
    const FLOOR_DB = -60;
    const lo = Math.max(Math.min(...rr), FLOOR_DB);
    const range = $('normalize').checked
        ? [Math.min(-40, Math.floor(lo / 10) * 10), 0]
        : [Math.floor(lo / 10) * 10 - 5, Math.ceil(Math.max(...rr) / 10) * 10 + 5];

    Plotly.update('chart-polar', { r: [rOut], theta: [tOut] },
                  { 'polar.radialaxis.range': range });
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
    if (!on) {
        $('mode-badge').textContent = 'offline';
        $('mode-badge').removeAttribute('data-mode');
    }
    syncControls();
}

function applyState(s) {
    if (!s) return;
    $('mode-badge').textContent = s.mode === 'sim' ? 'simulated' : 'hardware';
    $('mode-badge').dataset.mode = s.mode;
    $('vna-idn').textContent = s.vna_idn || '—';
    $('pos-idn').textContent = s.pos_idn || '—';
    $('pos-err').textContent = s.latched_error ?? '—';
    if (s.angle != null) $('angle-readout').textContent = `${s.angle.toFixed(1)}°`;
    if (s.speed != null) $('speed-readout').textContent = `${s.speed.toFixed(0)}%`;
    if (s.error) addLog('error', 'state', s.error);
    state.scanning = !!s.scanning;
    syncControls();
}

const SCAN_INPUTS = ['f-start', 'f-stop', 'f-points', 'f-ifbw', 'f-power', 'f-param',
                     'a-start', 'a-stop', 'a-step', 'a-speed', 'jog-abs', 'btn-goto',
                     'btn-zero'];

function syncControls() {
    const busy = state.scanning || !state.connected;
    SCAN_INPUTS.forEach((id) => { const el = $(id); if (el) el.disabled = busy; });
    document.querySelectorAll('[data-jog]').forEach((b) => { b.disabled = busy; });
    $('btn-scan').disabled = busy;
    $('btn-scan').textContent = state.scanning ? 'Scanning…' : 'Start scan';
    $('btn-stop').disabled = !state.connected;
}

function updateAngleCount() {
    const a0 = parseFloat($('a-start').value);
    const a1 = parseFloat($('a-stop').value);
    const st = parseFloat($('a-step').value);
    if (!(st > 0) || !(a1 >= a0)) { $('angle-count').textContent = 'invalid range'; return; }
    let n = Math.floor((a1 - a0) / st + 1e-9) + 1;
    if (n > 1 && Math.abs(((a0 + st * (n - 1) - a0) % 360)) < 1e-9) n -= 1;
    $('angle-count').textContent = `${n} angle${n === 1 ? '' : 's'}`;
}

// ---------------------------------------------------------------------------
// Transport
// ---------------------------------------------------------------------------
const transport = createTransport({
    onLog: addLog,
    onOpen: () => { setConnected(true); transport.getState().then(applyState).catch(() => {}); },
    onClose: () => setConnected(false),
    onState: applyState,
    onPosition: (deg) => { $('angle-readout').textContent = `${deg.toFixed(1)}°`; },
    onScanStarted: (m) => {
        state.scanning = true;
        state.angles = m.angles;
        state.freqs = m.freqs;
        state.grid = new Array(m.angles.length).fill(null);
        populateCutFreqs();
        $('scan-progress').value = 0;
        $('progress-label').textContent = `0 / ${m.angles.length}`;
        syncControls();
    },
    onScanPoint: (m) => {
        state.grid[m.index] = m.mag_db;
        state.lastTrace = m.mag_db;
        state.lastAngle = m.angle_actual;
        const n = state.angles.length;
        $('scan-progress').value = ((m.index + 1) / n) * 100;
        $('progress-label').textContent = `${m.index + 1} / ${n}`;
        redrawPolar();
        redrawRect();
    },
    onScanDone: (m) => {
        state.scanning = false;
        $('progress-label').textContent = m.cancelled
            ? `cancelled${m.n_done != null ? ` at ${m.n_done}` : ''}` : 'complete';
        if (m.error) addLog('error', 'scan', m.error);
        syncControls();
        refreshRuns();
    },
});

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
async function guard(fn) {
    try { return await fn(); }
    catch (e) { addLog('error', 'cmd', e.message); }
}

function scanParams() {
    return {
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
}

function refreshRuns() {
    guard(() => transport.listRuns()).then((r) => {
        if (r?.runs?.length) addLog('info', 'runs', `${r.runs.length} stored run(s)`);
    });
}

function wire() {
    $('btn-scan').addEventListener('click', async () => {
        const speed = parseFloat($('a-speed').value);
        if (Number.isFinite(speed)) await guard(() => transport.setSpeed(speed));
        await guard(() => transport.startScan(scanParams()));
    });

    // Stop stays enabled while scanning: it is the one control that must always
    // be reachable, and the service preempts the move rather than queueing.
    $('btn-stop').addEventListener('click', () => guard(() => transport.stop()));

    document.querySelectorAll('[data-jog]').forEach((b) => {
        b.addEventListener('click', () =>
            guard(() => transport.jog({ delta: parseFloat(b.dataset.jog) })));
    });

    $('btn-goto').addEventListener('click', () => {
        const v = parseFloat($('jog-abs').value);
        if (Number.isFinite(v)) guard(() => transport.jog({ deg: v }));
    });

    $('btn-zero').addEventListener('click', () => guard(() => transport.zeroHere()));
    $('btn-clear-log').addEventListener('click', () => { $('log').innerHTML = ''; });

    $('cut-freq').addEventListener('change', (e) => {
        state.cutIndex = parseInt(e.target.value, 10);
        redrawPolar();
    });
    $('normalize').addEventListener('change', redrawPolar);

    ['a-start', 'a-stop', 'a-step'].forEach((id) =>
        $(id).addEventListener('input', updateAngleCount));

    $('theme-toggle').addEventListener('click', () => {
        const root = document.documentElement;
        const next = root.dataset.theme === 'light' ? 'dark' : 'light';
        root.dataset.theme = next;
        localStorage.setItem('chamber-theme', next);
        restylePlots();
    });

    window.addEventListener('resize', () => {
        Plotly.Plots.resize('chart-polar');
        Plotly.Plots.resize('chart-rect');
    });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
document.documentElement.dataset.theme = localStorage.getItem('chamber-theme') || 'dark';
initPlots();
wire();
updateAngleCount();
setConnected(false);
transport.connect();
