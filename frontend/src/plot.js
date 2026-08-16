/**
 * plot.js — canvas renderers for the pattern and spectrum views.
 *
 * Deliberately dependency-free rather than pulling in Plotly as Phaser does.
 * A live polar trace with a dB grid is a small amount of drawing code, and
 * keeping it dependency-free means the UI builds and runs on a lab machine
 * with no package registry reachable.
 *
 * Both classes expose the same three methods — resize(), setData(), draw() —
 * so swapping either one for a charting library later is a single-file change.
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
    }

    setData({ angles, values, fullCircle, current }) {
        if (angles !== undefined) this.angles = angles;
        if (values !== undefined) this.values = values;
        if (fullCircle !== undefined) this.fullCircle = fullCircle;
        if (current !== undefined) this.current = current;
    }

    clear() {
        this.angles = [];
        this.values = [];
        this.current = null;
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

        if (this.values.length < 2) return;

        // the trace
        ctx.strokeStyle = trace;
        ctx.lineWidth = 1.8;
        ctx.lineJoin = 'round';
        ctx.beginPath();
        for (let i = 0; i < this.values.length; i++) {
            const db = this.values[i] - peak;                   // normalized
            const r01 = (Math.max(db, this.floorDb) - this.floorDb) / span;
            const [x, y] = this._xy(cx, cy, R, this.angles[i], r01);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        // Close the trace only on a full revolution. Closing a partial cut
        // would draw a chord across a span that was never measured.
        if (this.fullCircle && this.values.length > 2) ctx.closePath();
        ctx.stroke();

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
