/**
 * main.js — wiring only.
 *
 * Reads the form into a config object, hands it to the transport, and renders
 * whatever events come back. It knows nothing about VISA, SCPI, or the scan
 * loop; the service forwards the engine's events verbatim, so what shows up
 * here is exactly what the CLI prints.
 *
 * Chrome follows Phaser: an accordion of setting sections on the left, tabs
 * over the plots on the right, theme control at the foot of the sidebar.
 */

import './style.css';
import { createTransport } from './transport.js';
import { LinePlot, PolarPlot, DialPlot } from './plot.js';
import { parsePattern, compare, traceFor, cutLabel } from './reference.js';

const $ = (id) => document.getElementById(id);

const els = {
    subtitle: $('subtitle'),
    dotLink: $('dot-link'),
    stateText: $('state-text'),
    pillMock: $('pill-mock'),
    pillPhase: $('pill-phase'),
    pillSpan: $('pill-span'),
    pillMotion: $('pill-motion'),
    statAngle: $('stat-angle'),
    statCut: $('stat-cut'),
    statPeak: $('stat-peak'),
    statProgress: $('stat-progress'),
    statDelta: $('stat-delta'),
    statBoxDelta: $('stat-box-delta'),
    statParam: $('stat-param'),
    statPoints: $('stat-points'),
    statCutFreq: $('stat-cutfreq'),
    statCut2: $('stat-cut2'),
    statCmd: $('stat-cmd'),
    statActual: $('stat-actual'),
    statErr: $('stat-err'),
    statGrid: $('stat-grid'),
    statRemaining: $('stat-remaining'),
    progress: $('progress'),
    log: $('log'),
    btnStart: $('btn-start'),
    btnAbort: $('btn-abort'),
    btnConnect: $('btn-connect'),
    btnClearLog: $('btn-clear-log'),
    legendRef: document.querySelector('.legend-item.ref'),
    legendRefLabel: $('legend-ref-label'),
};

const polar = new PolarPlot($('polar'));
const spectrum = new LinePlot($('spectrum'));
const dial = new DialPlot($('dial'));

let run = { angles: [], values: [], freqs: [], grid: [], n: 0, cutIndex: 0 };
let linkUp = false;
let state = 'offline';

// The imported comparison pattern: every cut in the file, plus which one is
// selected and how it is being lined up against the measurement.
let reference = { name: '', cuts: null, key: null };

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
    els.stateText.textContent = next;
    els.dotLink.className = `dot ${linkUp ? 'connected' : 'disconnected'}`;
    if (detail) els.subtitle.textContent = detail;
    const scanning = next === 'scanning';
    els.btnStart.disabled = scanning || !linkUp;
    els.btnAbort.disabled = !scanning;
    els.btnConnect.disabled = scanning || !linkUp;
}

function resetRun(ready) {
    run = {
        angles: [],
        values: [],
        freqs: ready?.freq_hz ?? [],
        grid: ready?.angles_deg ?? [],
        n: ready?.angles_deg?.length ?? 0,
        cutIndex: ready?.cut_index ?? 0,
    };
    polar.clear();
    polar.setData({ fullCircle: !!ready?.full_circle });
    spectrum.clear();
    dial.clear();
    // prepare() reports where the axis is standing before anything moves.
    dial.setData({ grid: run.grid, done: 0, actual: ready?.position_deg ?? null });
    els.progress.style.width = '0%';
    els.statProgress.textContent = `0/${run.n}`;
    for (const el of [els.statAngle, els.statCut, els.statPeak, els.statDelta,
                      els.statCmd, els.statActual, els.statErr, els.statCut2]) {
        el.textContent = '—';
    }
    els.statGrid.textContent = run.n
        ? `${run.grid[0].toFixed(0)}…${run.grid[run.grid.length - 1].toFixed(0)}°`
        : '—';
    els.statRemaining.textContent = `${run.n}`;
    if (typeof ready?.position_deg === 'number') {
        els.statActual.textContent = `${ready.position_deg.toFixed(1)}°`;
    }
    els.statParam.textContent = $('param').value;
    els.statPoints.textContent = run.freqs.length || '—';
    if (run.freqs.length) {
        const f0 = run.freqs[0] / 1e9;
        const f1 = run.freqs[run.freqs.length - 1] / 1e9;
        const fc = run.freqs[run.cutIndex] / 1e9;
        els.pillSpan.textContent =
            `${f0.toFixed(3)}–${f1.toFixed(3)} GHz · cut ${fc.toFixed(4)}`;
        els.statCutFreq.textContent = `${fc.toFixed(4)}`;
    }
    applyReference();
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
    dial.setData({ done, commanded: ev.angle_cmd ?? null, actual: ev.angle_actual });
    els.statProgress.textContent = `${done}/${ev.n}`;
    els.progress.style.width = `${(done / ev.n) * 100}%`;
    els.statAngle.textContent = `${ev.angle_actual.toFixed(1)}°`;
    els.statCut.textContent = `${ev.cut_db.toFixed(2)} dB`;
    els.statCut2.textContent = `${ev.cut_db.toFixed(2)} dB`;
    els.statPeak.textContent = `${Math.max(...run.values).toFixed(2)} dB`;
    els.statCmd.textContent = ev.angle_cmd === undefined ? '—' : `${ev.angle_cmd.toFixed(1)}°`;
    els.statActual.textContent = `${ev.angle_actual.toFixed(1)}°`;
    if (ev.angle_cmd !== undefined) {
        els.statErr.textContent = `${(ev.angle_actual - ev.angle_cmd).toFixed(2)}°`;
    }
    els.statRemaining.textContent = `${Math.max(0, ev.n - done)}`;
    updateDelta();
    draw();
}

function draw() {
    polar.draw();
    spectrum.draw();
    dial.draw();
}

// ------------------------------------------------------- imported reference

function selectedCut() {
    if (!reference.cuts || !reference.key) return null;
    return reference.cuts.get(reference.key) ?? null;
}

/** Push the selected cut into the polar plot, or take it back out. */
function applyReference() {
    const cut = selectedCut();
    const show = cut && $('ref-show').checked;
    const rotate = parseFloat($('ref-rotate').value) || 0;
    polar.setData({ ref: show ? traceFor(cut, rotate) : null });
    els.legendRef.hidden = !show;
    if (show) els.legendRefLabel.textContent = cutLabel(cut);
    els.statBoxDelta.hidden = !cut;
    updateDelta();
}

/** RMS and worst-case deviation of the live cut from the reference. */
function updateDelta() {
    const cut = selectedCut();
    if (!cut || !run.values.length) {
        els.statDelta.textContent = '—';
        return;
    }
    const stats = compare(run.angles, run.values, cut, {
        normalize: $('ref-normalize').checked,
        rotateDeg: parseFloat($('ref-rotate').value) || 0,
    });
    els.statDelta.textContent = stats
        ? `${stats.rms.toFixed(2)} dB`
        : 'no overlap';
    els.statBoxDelta.title = stats
        ? `RMS ${stats.rms.toFixed(2)} dB over ${stats.n} angles; ` +
          `worst ${stats.max >= 0 ? '+' : ''}${stats.max.toFixed(2)} dB at ${stats.at.toFixed(1)}°`
        : 'the reference does not cover the measured angles';
}

/** Rebuild the parameter/frequency pickers from a freshly parsed file. */
function fillCutPickers() {
    const params = [...new Set([...reference.cuts.values()].map((c) => c.param))];
    const paramSel = $('ref-param');
    paramSel.innerHTML = '';
    for (const p of params) {
        const o = document.createElement('option');
        o.value = p;
        o.textContent = p;
        paramSel.appendChild(o);
    }
    fillFreqPicker();
}

function fillFreqPicker() {
    const param = $('ref-param').value;
    const freqSel = $('ref-freq');
    freqSel.innerHTML = '';
    let first = null;
    for (const [key, cut] of reference.cuts) {
        if (cut.param !== param) continue;
        const o = document.createElement('option');
        o.value = key;
        o.textContent = cut.freqHz ? `${(cut.freqHz / 1e9).toFixed(4)} GHz` : 'no frequency';
        freqSel.appendChild(o);
        if (first === null) first = key;
    }
    // Default to the cut nearest the frequency this run is cutting at, which
    // is almost always the one worth comparing.
    let want = first;
    if (run.freqs.length) {
        const target = run.freqs[run.cutIndex];
        let best = Infinity;
        for (const [key, cut] of reference.cuts) {
            if (cut.param !== param || !cut.freqHz) continue;
            const d = Math.abs(cut.freqHz - target);
            if (d < best) { best = d; want = key; }
        }
    }
    reference.key = want;
    if (want) freqSel.value = want;
}

async function loadReference(file) {
    try {
        const { cuts, rows } = parsePattern(await file.text());
        reference = { name: file.name, cuts, key: null };
        $('ref-name').textContent = file.name;
        $('ref-summary').hidden = false;
        fillCutPickers();
        applyReference();
        draw();
        log('info', 'sim', `imported ${file.name}: ${rows} rows, ${cuts.size} cut(s)`);
    } catch (err) {
        log('error', 'sim', `${file.name}: ${err.message}`);
    }
}

function clearReference() {
    reference = { name: '', cuts: null, key: null };
    $('ref-file').value = '';
    $('ref-summary').hidden = true;
    applyReference();
    draw();
    log('info', 'sim', 'reference cleared');
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
            els.pillMotion.textContent = ev.phase === 'homing' ? 'homing' : ev.phase;
            els.pillMotion.className = `status-pill state-${ev.phase}`;
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
            els.pillMotion.textContent = 'stopped';
            els.pillMotion.className = 'status-pill';
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
        els.btnConnect.disabled = !up || state === 'scanning';
    },
});

// ------------------------------------------------------------------ config

function num(id) {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) ? v : null;
}

function buildConfig() {
    const cut = num('cut-ghz');
    const outdir = $('outdir').value.trim();
    const aux = $('aux-param').value;
    const scan = {
        start_deg: num('from-deg'),
        stop_deg: num('to-deg'),
        step_deg: num('step-deg'),
        cut_freq_hz: cut ? cut * 1e9 : null,
        db_floor: num('db-floor'),
        closure_check: $('closure').checked,
        return_home: $('return-home').checked,
        write_s2p: $('write-s2p').checked,
        write_plot: $('write-plot').checked,
    };
    // Left empty, the key is omitted entirely: the service stamps a run
    // directory of its own, and an empty string would land in the CWD.
    if (outdir) scan.outdir = outdir;

    return {
        vna: {
            resource: $('vna-res').value.trim(),
            start_hz: num('start-ghz') * 1e9,
            stop_hz: num('stop-ghz') * 1e9,
            points: num('points'),
            if_bw_hz: num('ifbw'),
            power_dbm: num('power'),
            parameter: $('param').value,
            aux_parameter: aux || null,
            averaging: num('averaging'),
            channel: num('channel'),
            timeout_ms: (num('vna-timeout') ?? 120) * 1000,
        },
        positioner: {
            resource: $('pos-res').value.trim(),
            speed_preset: num('speed-preset'),
            settle_s: num('settle-s'),
            backlash_deg: num('backlash-deg'),
            position_tol_deg: num('tol-deg'),
            move_timeout_s: num('move-timeout'),
        },
        cmds: { slot: num('slot'), device: $('device').value },
        scan,
    };
}

// ------------------------------------------------------------------- chrome

/* Accordion. Icons match the collapsed rail, so a section is recognizable
 * whichever state the sidebar is in. */
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

document.querySelectorAll('.accordion-icon[data-icon]').forEach((el) => {
    const i = parseInt(el.getAttribute('data-icon'), 10);
    if (sectionIcons[i]) el.innerHTML = sectionIcons[i];
});

const accordionItems = [...document.querySelectorAll('.accordion-item')];

function markRailActive(idx) {
    document.querySelectorAll('.sidebar-icon-btn[data-section]').forEach((btn) => {
        btn.classList.toggle('active', parseInt(btn.dataset.section, 10) === idx);
    });
}

accordionItems.forEach((item, idx) => {
    item.querySelector('.accordion-header').addEventListener('click', () => {
        item.classList.toggle('active');
        if (item.classList.contains('active')) {
            markRailActive(idx);
            // Bring the section the click just opened to the top, or its body
            // opens below the fold on a short sidebar.
            setTimeout(() => {
                const acc = $('accordionSettings');
                acc.scrollTop += item.getBoundingClientRect().top
                              - acc.getBoundingClientRect().top;
            }, 120);
        }
    });
});
markRailActive(accordionItems.findIndex((i) => i.classList.contains('active')));

/* Sidebar collapse. The rail stays behind so the sections are still reachable
 * one click away. */
function setCollapsed(collapsed, openSection = null) {
    $('settings-panel').classList.toggle('collapsed', collapsed);
    $('dashboard').classList.toggle('settings-collapsed', collapsed);
    if (openSection !== null) {
        accordionItems.forEach((item, i) => item.classList.toggle('active', i === openSection));
        markRailActive(openSection);
    }
    requestAnimationFrame(resizeAll);
}

$('btn-toggle-settings').addEventListener('click', () => setCollapsed(true));
$('btn-toggle-settings-icon').addEventListener('click', () => setCollapsed(false));
document.querySelectorAll('.sidebar-icon-btn[data-section]').forEach((btn) => {
    btn.addEventListener('click', () =>
        setCollapsed(false, parseInt(btn.dataset.section, 10)));
});

/* Tabs. Panes stay laid out while hidden — a canvas that measures zero while
 * its tab is inactive comes back blank. */
document.querySelectorAll('.tab-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach((b) => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
        btn.classList.add('active');
        $(btn.dataset.target).classList.add('active');
        requestAnimationFrame(resizeAll);
    });
});

/* Theme: system unless told otherwise, and the button says which of the three
 * it is on rather than what the next click does. */
const THEME_KEY = 'chamber-theme';
const THEME_ORDER = ['system', 'light', 'dark'];
const media = window.matchMedia('(prefers-color-scheme: dark)');
let themeMode = 'system';

function applyTheme(mode, persist = true) {
    themeMode = THEME_ORDER.includes(mode) ? mode : 'system';
    const resolved = themeMode === 'system' ? (media.matches ? 'dark' : 'light') : themeMode;
    document.documentElement.setAttribute('data-theme', resolved);
    if (persist) localStorage.setItem(THEME_KEY, themeMode);

    const label = { system: 'System theme', light: 'Light mode', dark: 'Dark mode' }[themeMode];
    const glyph = { system: '◐', light: '☀', dark: '☾' }[themeMode];
    const btn = $('btn-theme');
    btn.querySelector('.theme-icon').textContent = glyph;
    btn.querySelector('.theme-label').textContent = label;
    btn.title = `Theme: ${themeMode} — click for ${THEME_ORDER[(THEME_ORDER.indexOf(themeMode) + 1) % 3]}`;
    btn.setAttribute('aria-label', btn.title);

    const iconBtn = $('btn-theme-icon');
    iconBtn.title = btn.title;
    iconBtn.setAttribute('aria-label', btn.title);
    iconBtn.querySelector('.icon-system').style.display = themeMode === 'system' ? 'block' : 'none';
    iconBtn.querySelector('.icon-sun').style.display = themeMode === 'light' ? 'block' : 'none';
    iconBtn.querySelector('.icon-moon').style.display = themeMode === 'dark' ? 'block' : 'none';

    draw();                              // canvases read their colors from CSS
}

function cycleTheme() {
    applyTheme(THEME_ORDER[(THEME_ORDER.indexOf(themeMode) + 1) % THEME_ORDER.length]);
}

$('btn-theme').addEventListener('click', cycleTheme);
$('btn-theme-icon').addEventListener('click', cycleTheme);
// Follow the OS while nobody has overridden it — including a switch made
// while the page is open, which is what "follow system" has to mean.
media.addEventListener('change', () => { if (themeMode === 'system') applyTheme('system', false); });
applyTheme(localStorage.getItem(THEME_KEY) || 'system', false);

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

els.btnConnect.addEventListener('click', () => {
    log('info', 'ui', 'opening instruments');
    transport.connectInstruments(buildConfig());
});

els.btnClearLog.addEventListener('click', () => { els.log.innerHTML = ''; });

$('ref-file').addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    if (file) loadReference(file);
});
$('btn-ref-clear').addEventListener('click', clearReference);
$('ref-param').addEventListener('change', () => { fillFreqPicker(); applyReference(); draw(); });
$('ref-freq').addEventListener('change', (e) => {
    reference.key = e.target.value;
    applyReference();
    draw();
});
for (const id of ['ref-show', 'ref-normalize', 'ref-rotate']) {
    $(id).addEventListener('input', () => { applyReference(); draw(); });
}

$('db-floor').addEventListener('change', () => {
    const v = num('db-floor');
    if (v !== null && v < 0) { polar.floorDb = v; draw(); }
});

$('param').addEventListener('change', () => { els.statParam.textContent = $('param').value; });

function resizeAll() {
    polar.resize();
    spectrum.resize();
    dial.resize();
}

window.addEventListener('resize', resizeAll);

setState('offline');
draw();

// Exposed for the screenshot harness and for poking at state from devtools.
window.__chamber = { handleEvent, transport, polar, spectrum, dial, applyTheme };
