/**
 * dial.js — the turntable's own view.
 *
 * Where the axis was told to go, where it reports being, and which angles on
 * the grid are already measured. Canvas rather than Plotly: this is an
 * instrument face, not a chart, and it needs no axes, legend, or hover.
 *
 * Colors come from the CSS custom properties, so the theme toggle carries it
 * along with everything else — but only if draw() is called again afterwards.
 */

function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
}

/** Size the backing store to the element, accounting for device pixel ratio. */
function setupCanvas(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(rect.width, 1);
    const h = Math.max(rect.height, 1);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return [ctx, w, h];
}

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

    // 0 deg at the top, positive clockwise — the same convention the polar
    // plot uses, so the two read the same way round.
    _xy(cx, cy, r, angleDeg) {
        const a = (angleDeg * Math.PI) / 180;
        return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
    }

    draw() {
        if (!this.canvas) return;
        const [ctx, w, h] = setupCanvas(this.canvas);
        const grid = cssVar('--grid-color', 'rgba(255,255,255,0.12)');
        const label = cssVar('--grid-label', '#94a3b8');
        const trace = cssVar('--secondary', '#0088d1');
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
