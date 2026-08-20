/**
 * plot.js — canvas renderers for the pattern and spectrum views.
 *
 * Deliberately dependency-free rather than pulling in Plotly as Phaser does.
 * A live polar trace with a dB grid is a small amount of drawing code, and
 * keeping it dependency-free means the UI builds and runs on a lab machine
 * with no package registry reachable.
 *
 * Every class here exposes the same three methods — resize(), setData(),
 * draw() — so swapping any one of them for a charting library later is a
 * single-file change.
 */

function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return v && v.trim() ? v.trim() : fallback;
}

/** Prepare a canvas for the device pixel ratio. Returns [ctx, w, h] in CSS px. */
function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    canvas.width = Math.floor(w * dpr);
    canvas.height = Math.floor(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return [ctx, w, h];
}

export class PolarPlot {
    /**
     * @param {HTMLCanvasElement} canvas
     * @param {{floorDb?: number}} opts
     */
    constructor(canvas, opts = {}) {
        this.canvas = canvas;
        this.floorDb = opts.floorDb ?? -40;
        this.angles = [];       // degrees, in measurement order
        this.values = [];       // dB, un-normalized
        this.fullCircle = false;
        this.current = null;    // angle of the most recent point
        // An imported pattern drawn behind the live trace, normalized the same
        // way, so the two are read off the same rings.
        this.ref = null;        // {angles, values} | null
    }

    setData({ angles, values, fullCircle, current, ref }) {
        if (angles !== undefined) this.angles = angles;
        if (values !== undefined) this.values = values;
        if (fullCircle !== undefined) this.fullCircle = fullCircle;
        if (current !== undefined) this.current = current;
        if (ref !== undefined) this.ref = ref;
    }

    clear() {
        this.angles = [];
        this.values = [];
        this.current = null;
    }

    /** Trace path helper, shared by the live and reference traces. */
    _trace(ctx, cx, cy, R, angles, values, peak, span, close) {
        ctx.beginPath();
        for (let i = 0; i < values.length; i++) {
            const db = values[i] - peak;
            const r01 = (Math.max(db, this.floorDb) - this.floorDb) / span;
            const [x, y] = this._xy(cx, cy, R, angles[i], r01);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        if (close && values.length > 2) ctx.closePath();
        ctx.stroke();
    }

    resize() { this.draw(); }

    // 0 deg at top, positive clockwise — matching the matplotlib output.
    _xy(cx, cy, R, angleDeg, radius01) {
        const a = (angleDeg * Math.PI) / 180;
        const r = R * Math.max(0, Math.min(1, radius01));
        return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
    }

    draw() {
        const [ctx, w, h] = setupCanvas(this.canvas);
        const grid = cssVar('--grid', 'rgba(255,255,255,0.1)');
        const label = cssVar('--grid-label', '#94a3b8');
        const trace = cssVar('--trace', '#0088d1');

        const cx = w / 2;
        const cy = h / 2;
        const R = Math.max(10, Math.min(w, h) / 2 - 26);

        // Normalize to the strongest measured point; before any data, show the
        // empty grid rather than an autoscaled void.
        const peak = this.values.length ? Math.max(...this.values) : 0;
        const span = -this.floorDb;

        ctx.font = '10px Inter, system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        // rings every 10 dB
        ctx.strokeStyle = grid;
        ctx.fillStyle = label;
        ctx.lineWidth = 1;
        for (let db = 0; db >= this.floorDb; db -= 10) {
            const r = R * ((db - this.floorDb) / span);
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, 2 * Math.PI);
            ctx.stroke();
            if (db < 0) ctx.fillText(`${db}`, cx + 1, cy - r + 7);
        }

        // spokes every 30 deg
        for (let a = 0; a < 360; a += 30) {
            const [x, y] = this._xy(cx, cy, R, a, 1);
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(x, y);
            ctx.stroke();
            const [lx, ly] = this._xy(cx, cy, R + 13, a, 1);
            ctx.fillText(`${a}`, lx, ly);
        }

        // The reference goes down first so the live trace stays legible on top
        // of it. It carries its own peak: normalizing each to itself is what
        // makes a simulated pattern comparable to a measured one at all.
        const ref = this.ref;
        if (ref && ref.values?.length > 1) {
            ctx.strokeStyle = cssVar('--reference', '#f59e0b');
            ctx.lineWidth = 1.4;
            ctx.lineJoin = 'round';
            ctx.setLineDash([4, 3]);
            const refPeak = Math.max(...ref.values);
            this._trace(ctx, cx, cy, R, ref.angles, ref.values, refPeak, span,
                        !!ref.fullCircle);
            ctx.setLineDash([]);
        }

        if (this.values.length < 2) return;

        // the trace.  Closed only on a full revolution: closing a partial cut
        // would draw a chord across a span that was never measured.
        ctx.strokeStyle = trace;
        ctx.lineWidth = 1.8;
        ctx.lineJoin = 'round';
        this._trace(ctx, cx, cy, R, this.angles, this.values, peak, span,
                    this.fullCircle);

        // marker on the most recent angle
        if (this.current !== null) {
            ctx.strokeStyle = cssVar('--trace-dim', 'rgba(0,136,209,0.35)');
            ctx.lineWidth = 1.4;
            const [x, y] = this._xy(cx, cy, R, this.current, 1);
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(x, y);
            ctx.stroke();
        }
    }
}

export class LinePlot {
    constructor(canvas) {
        this.canvas = canvas;
        this.x = [];
        this.y = [];
        this.xLabel = '';
    }

    setData({ x, y, xLabel }) {
        if (x !== undefined) this.x = x;
        if (y !== undefined) this.y = y;
        if (xLabel !== undefined) this.xLabel = xLabel;
    }

    clear() { this.x = []; this.y = []; }

    resize() { this.draw(); }

    draw() {
        const [ctx, w, h] = setupCanvas(this.canvas);
        const grid = cssVar('--grid', 'rgba(255,255,255,0.1)');
        const label = cssVar('--grid-label', '#94a3b8');
        const trace = cssVar('--trace', '#0088d1');

        const padL = 38, padR = 8, padT = 8, padB = 18;
        const plotW = w - padL - padR;
        const plotH = h - padT - padB;
        if (plotW <= 0 || plotH <= 0) return;

        ctx.font = '9px Inter, system-ui, sans-serif';
        ctx.fillStyle = label;
        ctx.strokeStyle = grid;
        ctx.lineWidth = 1;

        if (this.y.length < 2) {
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText('awaiting sweep', w / 2, h / 2);
            return;
        }

        let lo = Math.min(...this.y);
        let hi = Math.max(...this.y);
        if (!isFinite(lo) || !isFinite(hi)) return;
        if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
        const pad = (hi - lo) * 0.1;
        lo -= pad; hi += pad;

        // horizontal gridlines + y labels
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        for (let i = 0; i <= 3; i++) {
            const v = lo + ((hi - lo) * i) / 3;
            const y = padT + plotH - (plotH * i) / 3;
            ctx.beginPath();
            ctx.moveTo(padL, y);
            ctx.lineTo(padL + plotW, y);
            ctx.stroke();
            ctx.fillText(v.toFixed(1), padL - 5, y);
        }

        ctx.strokeStyle = trace;
        ctx.lineWidth = 1.6;
        ctx.beginPath();
        for (let i = 0; i < this.y.length; i++) {
            const x = padL + (plotW * i) / (this.y.length - 1);
            const y = padT + plotH - (plotH * (this.y[i] - lo)) / (hi - lo);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();

        if (this.xLabel) {
            ctx.fillStyle = label;
            ctx.textAlign = 'center';
            ctx.textBaseline = 'bottom';
            ctx.fillText(this.xLabel, padL + plotW / 2, h - 4);
        }
    }
}

/**
 * DialPlot — the turntable's own view: where the axis was told to go, where it
 * reports being, and which angles on the grid are already measured.
 *
 * chamber-specific; Phaser has no moving hardware to show.
 */
export class DialPlot {
    constructor(canvas) {
        this.canvas = canvas;
        this.grid = [];         // every angle in the run, degrees
        this.done = 0;          // how many of them are measured
        this.commanded = null;
        this.actual = null;
    }

    setData({ grid, done, commanded, actual }) {
        if (grid !== undefined) this.grid = grid;
        if (done !== undefined) this.done = done;
        if (commanded !== undefined) this.commanded = commanded;
        if (actual !== undefined) this.actual = actual;
    }

    clear() {
        this.grid = [];
        this.done = 0;
        this.commanded = null;
        this.actual = null;
    }

    resize() { this.draw(); }

    // Same convention as the polar plot: 0 deg at top, positive clockwise.
    _xy(cx, cy, r, angleDeg) {
        const a = (angleDeg * Math.PI) / 180;
        return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
    }

    draw() {
        const [ctx, w, h] = setupCanvas(this.canvas);
        const grid = cssVar('--grid', 'rgba(255,255,255,0.1)');
        const label = cssVar('--grid-label', '#94a3b8');
        const trace = cssVar('--trace', '#0088d1');
        const warn = cssVar('--warn', '#f59e0b');

        const cx = w / 2;
        const cy = h / 2;
        const R = Math.max(10, Math.min(w, h) / 2 - 24);

        ctx.font = '10px Inter, system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        // the table itself, and the 30 deg bearing marks around it
        ctx.strokeStyle = grid;
        ctx.fillStyle = label;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(cx, cy, R, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(cx, cy, R * 0.12, 0, 2 * Math.PI);
        ctx.stroke();
        for (let a = 0; a < 360; a += 30) {
            const [x0, y0] = this._xy(cx, cy, R * 0.9, a);
            const [x1, y1] = this._xy(cx, cy, R, a);
            ctx.beginPath();
            ctx.moveTo(x0, y0);
            ctx.lineTo(x1, y1);
            ctx.stroke();
            const [lx, ly] = this._xy(cx, cy, R + 12, a);
            ctx.fillText(`${a}`, lx, ly);
        }

        // every angle on the grid as a tick; the measured ones filled in
        for (let i = 0; i < this.grid.length; i++) {
            const [x, y] = this._xy(cx, cy, R * 0.8, this.grid[i]);
            ctx.beginPath();
            ctx.arc(x, y, 2, 0, 2 * Math.PI);
            ctx.fillStyle = i < this.done ? trace : grid;
            ctx.fill();
        }

        // commanded angle: where the axis was sent
        if (this.commanded !== null) {
            const [x, y] = this._xy(cx, cy, R * 0.95, this.commanded);
            ctx.strokeStyle = warn;
            ctx.lineWidth = 1.2;
            ctx.setLineDash([4, 3]);
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(x, y);
            ctx.stroke();
            ctx.setLineDash([]);
        }

        // actual angle: where it reports being. Drawn last, drawn solid.
        if (this.actual !== null) {
            const [x, y] = this._xy(cx, cy, R * 0.86, this.actual);
            ctx.strokeStyle = trace;
            ctx.lineWidth = 2.4;
            ctx.lineCap = 'round';
            ctx.beginPath();
            ctx.moveTo(cx, cy);
            ctx.lineTo(x, y);
            ctx.stroke();
            ctx.fillStyle = trace;
            ctx.beginPath();
            ctx.arc(x, y, 3.5, 0, 2 * Math.PI);
            ctx.fill();
        } else {
            ctx.fillStyle = label;
            ctx.fillText('no position reported', cx, cy + R + 22);
        }
    }
}
