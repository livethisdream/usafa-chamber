/**
 * main.js — wiring only.
 *
 * Reads the form into a config object, hands it to the transport, and renders
 * whatever events come back. It knows nothing about VISA, SCPI, or the scan
 * loop; the service forwards the engine's events verbatim, so what shows up
 * here is exactly what the CLI prints.
 */

import './style.css';
import { createTransport } from './transport.js';
import { LinePlot, PolarPlot } from './plot.js';

const $ = (id) => document.getElementById(id);

const els = {
    subtitle: $('subtitle'),
    dotLink: $('dot-link'),
    stateText: $('state-text'),
    pillState: $('pill-state'),
    pillMock: $('pill-mock'),
    pillPhase: $('pill-phase'),
    pillSpan: $('pill-span'),
    statAngle: $('stat-angle'),
    statCut: $('stat-cut'),
    statPeak: $('stat-peak'),
    statProgress: $('stat-progress'),
    progress: $('progress'),
    log: $('log'),
    btnStart: $('btn-start'),
    btnAbort: $('btn-abort'),
    btnTheme: $('btn-theme'),
    btnClearLog: $('btn-clear-log'),
};

const polar = new PolarPlot($('polar'));
const spectrum = new LinePlot($('spectrum'));

let run = { angles: [], values: [], freqs: [], n: 0, cutIndex: 0 };
let linkUp = false;
let state = 'offline';

// ------------------------------------------------------------------ logging

function log(level, source, message) {
    const line = document.createElement('div');
    line.className = `log-line ${level}`;
    const ts = new Date().toLocaleTimeString('en-GB', { hour12: false });
    line.innerHTML =
        `<span class="ts"></span><span class="src"></span><span class="msg"></span>`;
    line.children[0].textContent = ts;
    line.children[1].textContent = source;
    line.children[2].textContent = message;
    els.log.appendChild(line);
    while (els.log.childElementCount > 500) els.log.firstElementChild.remove();
    els.log.scrollTop = els.log.scrollHeight;
}

// --------------------------------------------------------------- rendering

function setState(next, detail) {
    state = next;
    els.pillState.className = `status-pill state-${next}`;
    els.stateText.textContent = next;
    els.dotLink.className = `dot ${linkUp ? 'connected' : 'disconnected'}`;
    if (detail) els.subtitle.textContent = detail;
    const scanning = next === 'scanning';
    els.btnStart.disabled = scanning || !linkUp;
    els.btnAbort.disabled = !scanning;
}

function resetRun(ready) {
    run = {
        angles: [],
        values: [],
        freqs: ready?.freq_hz ?? [],
        n: ready?.angles_deg?.length ?? 0,
        cutIndex: ready?.cut_index ?? 0,
    };
    polar.clear();
    polar.setData({ fullCircle: !!ready?.full_circle });
    spectrum.clear();
    els.progress.style.width = '0%';
    els.statProgress.textContent = `0/${run.n}`;
    els.statAngle.textContent = '—';
    els.statCut.textContent = '—';
    els.statPeak.textContent = '—';
    if (run.freqs.length) {
        const f0 = run.freqs[0] / 1e9;
        const f1 = run.freqs[run.freqs.length - 1] / 1e9;
        const fc = run.freqs[run.cutIndex] / 1e9;
        els.pillSpan.textContent =
            `${f0.toFixed(3)}–${f1.toFixed(3)} GHz · cut ${fc.toFixed(4)}`;
    }
    draw();
}

function onPoint(ev) {
    // Plot the angle the positioner actually reached, not the one commanded.
    run.angles.push(ev.angle_actual);
    run.values.push(ev.cut_db);
    polar.setData({
        angles: run.angles,
        values: run.values,
        current: ev.angle_actual,
    });
    spectrum.setData({
        y: ev.mag_db,
        xLabel: `${(run.freqs[0] / 1e9).toFixed(3)} – ` +
                `${(run.freqs[run.freqs.length - 1] / 1e9).toFixed(3)} GHz`,
    });

    const done = ev.index + 1;
    els.statProgress.textContent = `${done}/${ev.n}`;
    els.progress.style.width = `${(done / ev.n) * 100}%`;
    els.statAngle.textContent = `${ev.angle_actual.toFixed(1)}°`;
    els.statCut.textContent = `${ev.cut_db.toFixed(2)} dB`;
    els.statPeak.textContent = `${Math.max(...run.values).toFixed(2)} dB`;
    draw();
}

function draw() {
    polar.draw();
    spectrum.draw();
}

// ------------------------------------------------------------------ events

function handleEvent(ev) {
    switch (ev.type) {
        case 'state':
            els.pillMock.hidden = !ev.mock;
            setState(ev.state, ev.detail);
            break;
        case 'ready':
            resetRun(ev);
            break;
        case 'phase':
            els.pillPhase.textContent = ev.phase;
            els.pillPhase.className = `status-pill state-${ev.phase}`;
            if (ev.detail) log('info', 'scan', `${ev.phase}: ${ev.detail}`);
            break;
        case 'point':
            onPoint(ev);
            break;
        case 'closure':
            log('info', 'scan',
                `closure: ${ev.max_abs_delta_db.toFixed(3)} dB max drift, ` +
                `${ev.mean_delta_db >= 0 ? '+' : ''}${ev.mean_delta_db.toFixed(3)} dB mean`);
            break;
        case 'done':
            log(ev.aborted ? 'warn' : 'info', 'scan',
                `${ev.aborted ? 'aborted' : 'complete'} — ` +
                `${ev.angles_measured} angles → ${ev.outdir}`);
            polar.setData({ current: null });
            draw();
            break;
        case 'log':
            log(ev.level, ev.source, ev.message);
            break;
        case 'error':
            log('error', 'service', ev.message);
            break;
        default:
            break;
    }
}

const transport = createTransport({
    onEvent: handleEvent,
    onLog: log,
    onLink: (up) => {
        linkUp = up;
        if (!up) setState('offline', 'service unreachable');
        else log('info', 'link', 'connected to service');
        els.dotLink.className = `dot ${up ? 'connected' : 'disconnected'}`;
        els.btnStart.disabled = !up || state === 'scanning';
    },
});

// ------------------------------------------------------------------ config

function num(id) {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) ? v : null;
}

function buildConfig() {
    const cut = num('cut-ghz');
    return {
        vna: {
            resource: $('vna-res').value.trim(),
            start_hz: num('start-ghz') * 1e9,
            stop_hz: num('stop-ghz') * 1e9,
            points: num('points'),
            if_bw_hz: num('ifbw'),
            power_dbm: num('power'),
            parameter: $('param').value,
        },
        positioner: { resource: $('pos-res').value.trim() },
        cmds: { slot: num('slot'), device: $('device').value },
        scan: {
            start_deg: num('from-deg'),
            stop_deg: num('to-deg'),
            step_deg: num('step-deg'),
            cut_freq_hz: cut ? cut * 1e9 : null,
            closure_check: $('closure').checked,
            return_home: $('return-home').checked,
        },
    };
}

// ------------------------------------------------------------------- wiring

els.btnStart.addEventListener('click', () => {
    const cfg = buildConfig();
    if (!(cfg.scan.step_deg > 0)) {
        log('error', 'ui', 'step must be greater than zero');
        return;
    }
    if (cfg.scan.stop_deg < cfg.scan.start_deg) {
        log('error', 'ui', 'stop angle must not be below the start angle');
        return;
    }
    log('info', 'ui', `starting scan ${cfg.scan.start_deg}° → ${cfg.scan.stop_deg}° ` +
                      `step ${cfg.scan.step_deg}°`);
    transport.start(cfg);
});

els.btnAbort.addEventListener('click', () => {
    log('warn', 'ui', 'abort requested');
    transport.abort();
});

els.btnClearLog.addEventListener('click', () => { els.log.innerHTML = ''; });

els.btnTheme.addEventListener('click', () => {
    const root = document.documentElement;
    const next = root.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
    root.setAttribute('data-theme', next);
    localStorage.setItem('chamber-theme', next);
    draw();                                  // canvases read theme from CSS vars
});

const savedTheme = localStorage.getItem('chamber-theme');
if (savedTheme) document.documentElement.setAttribute('data-theme', savedTheme);

window.addEventListener('resize', () => {
    polar.resize();
    spectrum.resize();
});

setState('offline');
draw();

// Exposed for the screenshot harness and for poking at state from devtools.
window.__chamber = { handleEvent, transport, polar, spectrum };
