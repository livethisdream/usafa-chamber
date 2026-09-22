/**
 * reference.js — import a pattern and compare it against the live cut.
 *
 * The point of this is the loop a chamber run actually sits in: someone
 * simulated a pattern, the antenna got built, and the question is how far the
 * measurement has drifted from the model. So the importer is deliberately
 * loose about where the file came from — it reads this project's own
 * `pattern.csv` first, and falls back to any CSV with an angle column and a dB
 * column, which is what most solvers export.
 *
 * Parsing happens in the browser. Nothing is uploaded, and the service never
 * learns a comparison is going on: the reference is a display-side artifact.
 */

const ANGLE_KEYS = ['angle_actual_deg', 'angle_cmd_deg', 'angle_deg', 'angle',
                    'theta_deg', 'theta', 'phi_deg', 'phi', 'az_deg', 'azimuth',
                    'el_deg', 'elevation', 'deg'];
const VALUE_KEYS = ['mag_db', 'gain_db', 'gain_dbi', 'db', 'amplitude_db',
                    'magnitude_db', 'directivity_db', 'gain', 'magnitude'];
const PARAM_KEYS = ['param', 'parameter', 's_param'];
const FREQ_KEYS = ['freq_hz', 'frequency_hz', 'freq', 'frequency', 'f_hz'];
// Columns that name a cut plane outright, whatever they hold ('E-plane',
// 'phi=90', '0'). These are never the swept axis.
const PLANE_KEYS = ['cut_plane', 'plane', 'cut'];
// Columns that are an angular axis but not yet known to be *the* axis. A
// solver export usually carries both Theta and Phi: one of them is swept and
// the other names the plane it was swept in, and which is which is a property
// of the rows, not of the header order.
const AXIS_KEYS = ['theta_deg', 'theta', 'phi_deg', 'phi',
                   'az_deg', 'azimuth', 'el_deg', 'elevation'];

function splitLine(line) {
    // Comma, tab, or semicolon — whichever the file actually uses. Whitespace
    // alone is not enough of a signal to guess from, so it is not offered.
    const delim = line.includes('\t') ? '\t' : line.includes(';') ? ';' : ',';
    return line.split(delim).map((c) => c.trim().replace(/^"|"$/g, ''));
}

function findColumn(header, candidates) {
    const lower = header.map((h) => h.toLowerCase());
    for (const want of candidates) {
        const i = lower.indexOf(want);
        if (i >= 0) return i;
    }
    // Nothing matched exactly; accept a column that contains the name, so
    // 'Gain (dB)' and 'Theta [deg]' still land.
    for (const want of candidates) {
        const i = lower.findIndex((h) => h.includes(want));
        if (i >= 0) return i;
    }
    return -1;
}

/** Every distinct column index any of `candidates` resolves to, in header order. */
function columnsFor(header, candidates) {
    const found = new Set();
    for (const want of candidates) {
        const i = findColumn(header, [want]);
        if (i >= 0) found.add(i);
    }
    return [...found].sort((a, b) => a - b);
}

/** 'Theta [deg]' -> 'theta'. The units are already implied by the degree sign. */
function axisName(head) {
    return head.toLowerCase()
        .replace(/[[(].*$/, '')
        .replace(/[_\s]*deg(rees)?$/, '')
        .replace(/[_\s]+$/, '')
        .trim() || 'cut';
}

/**
 * Name the plane a row was taken in, from whatever columns discriminate it.
 *
 * A numeric column reads as 'theta=90°'; anything else is taken as already
 * being a name ('E-plane') and used as it stands. Several columns join, so a
 * file cut in both theta and phi still names each plane uniquely.
 */
function planeLabel(header, cols, cells) {
    const parts = [];
    for (const i of cols) {
        const raw = (cells[i] ?? '').trim();
        if (!raw) continue;
        const num = parseFloat(raw);
        parts.push(Number.isFinite(num) && /^[-+0-9.eE]+$/.test(raw)
            ? `${axisName(header[i])}=${Number(num.toFixed(1))}\u00b0`
            : raw);
    }
    return parts.join(', ');
}

/**
 * Parse CSV text into cuts keyed by "param @ freq, plane".
 *
 * The plane matters as much as the frequency does. A solver asked for a
 * pattern usually returns every cut it computed in one file, and differencing
 * a measured azimuth cut against a modelled elevation cut produces a number
 * that looks like an answer and is not one. So a file that discriminates its
 * planes is split by them here, and the caller gets to say which one it meant.
 *
 * @returns {{cuts: Map<string, {param: string, freqHz: number|null, plane: string,
 *                              angles: number[], values: number[]}>,
 *            rows: number, planes: string[]}}
 * @throws {Error} when no angle/dB column pair can be found
 */
export function parsePattern(text) {
    const lines = text.split(/\r?\n/)
        .filter((l) => l.trim() && !l.trim().startsWith('#') && !l.trim().startsWith('!'));
    if (lines.length < 2) throw new Error('file has no data rows');

    const header = splitLine(lines[0]);
    let iAngle = findColumn(header, ANGLE_KEYS);
    const iValue = findColumn(header, VALUE_KEYS);
    if (iAngle < 0 || iValue < 0) {
        throw new Error(`no angle/dB columns in header: ${header.slice(0, 8).join(', ')}`);
    }
    const iParam = findColumn(header, PARAM_KEYS);
    const iFreq = findColumn(header, FREQ_KEYS);

    const body = [];
    for (let n = 1; n < lines.length; n++) body.push(splitLine(lines[n]));

    // Decide which angular column is the sweep before reading any of it.
    // Header order cannot answer this: an azimuth cut is theta held at 90 with
    // phi swept, and reading it the other way round collapses a whole pattern
    // onto one angle. Whichever column actually moves is the sweep.
    const axisCols = columnsFor(header, AXIS_KEYS);
    if (axisCols.length > 1 && axisCols.includes(iAngle)) {
        const spread = (i) => new Set(body.map((c) => (c[i] ?? '').trim())).size;
        iAngle = axisCols.reduce((a, b) => (spread(b) > spread(a) ? b : a));
    }
    // Everything angular that is not the sweep names the plane, as does any
    // column that says so outright.
    const planeCols = [...new Set([...columnsFor(header, PLANE_KEYS),
                                   ...axisCols.filter((i) => i !== iAngle)])]
        .filter((i) => i !== iAngle && i !== iValue && i !== iParam && i !== iFreq)
        .sort((a, b) => a - b);

    const cuts = new Map();
    let rows = 0;

    for (const c of body) {
        const angle = parseFloat(c[iAngle]);
        const value = parseFloat(c[iValue]);
        if (!Number.isFinite(angle) || !Number.isFinite(value)) continue;

        const param = iParam >= 0 ? (c[iParam] || '—') : '—';
        let freqHz = iFreq >= 0 ? parseFloat(c[iFreq]) : NaN;
        if (!Number.isFinite(freqHz)) freqHz = null;
        // A GHz column read as Hz would sort and label wrong; scale it up.
        else if (freqHz > 0 && freqHz < 1e6) freqHz *= 1e9;

        const plane = planeCols.length ? planeLabel(header, planeCols, c) : '';
        const key = `${param}|${freqHz ?? ''}|${plane}`;
        let cut = cuts.get(key);
        if (!cut) {
            cut = { param, freqHz, plane, angles: [], values: [] };
            cuts.set(key, cut);
        }
        cut.angles.push(angle);
        cut.values.push(value);
        rows++;
    }

    if (!cuts.size) throw new Error('no numeric rows found');

    // Sort each cut by angle so the trace and the interpolation both behave.
    for (const cut of cuts.values()) {
        const order = cut.angles.map((a, i) => i).sort((x, y) => cut.angles[x] - cut.angles[y]);
        cut.angles = order.map((i) => cut.angles[i]);
        cut.values = order.map((i) => cut.values[i]);
    }
    return { cuts, rows, planes: planesOf(cuts) };
}

/** The distinct cut planes present, in the order they first appear. */
export function planesOf(cuts) {
    return [...new Set([...cuts.values()].map((c) => c.plane))];
}

/** Wrap to (-180, 180]. Matches acquisition/config.py's wrap180. */
export function wrap180(x) {
    return ((x + 180) % 360 + 360) % 360 - 180;
}

/**
 * Sample a cut at one angle, interpolating between its two nearest samples.
 *
 * Angles are compared wrapped, so a reference that runs 0..355 still answers
 * for a measurement that runs -180..175. Returns null when the nearest sample
 * is further away than `maxGapDeg` — better a hole in the comparison than a
 * number invented across a span the reference never covered.
 */
export function sampleAt(cut, angleDeg, maxGapDeg = 10) {
    const n = cut.angles.length;
    if (!n) return null;

    let bestBelow = null, bestAbove = null;
    for (let i = 0; i < n; i++) {
        const d = wrap180(cut.angles[i] - angleDeg);
        if (d <= 0 && (bestBelow === null || d > bestBelow.d)) bestBelow = { d, v: cut.values[i] };
        if (d >= 0 && (bestAbove === null || d < bestAbove.d)) bestAbove = { d, v: cut.values[i] };
    }
    if (!bestBelow || !bestAbove) {
        const one = bestBelow || bestAbove;
        return Math.abs(one.d) <= maxGapDeg ? one.v : null;
    }
    const gap = bestAbove.d - bestBelow.d;
    if (gap > 2 * maxGapDeg) return null;
    if (gap === 0) return bestBelow.v;
    const t = -bestBelow.d / gap;
    return bestBelow.v + t * (bestAbove.v - bestBelow.v);
}

/**
 * Compare a measured cut against a reference.
 *
 * Both are normalized to their own peak first when `normalize` is set, which
 * is the only way a simulated pattern in dBi and a measured one in raw S21 dB
 * can be differenced at all. `rotateDeg` takes out a known mount offset.
 *
 * Both traces are then clamped at `floorDb` below the peak before being
 * differenced. This is not cosmetic. A pattern's nulls are where the model and
 * the measurement disagree most and where the disagreement means least: a null
 * one degree off its predicted angle differences to tens of dB against a
 * neighbouring lobe, and an un-clamped RMS ends up reporting null alignment
 * rather than pattern agreement. Clamping states the dynamic range the
 * comparison is over and keeps the number about the part of the pattern that
 * carries power. Raise `floorDb` toward the noise floor to include more of the
 * nulls, at the cost of a noisier answer.
 *
 * @returns {{rms: number, max: number, at: number, n: number, floorDb: number}|null}
 */
export function compare(measAngles, measValues, cut,
                        { normalize = true, rotateDeg = 0, floorDb = -30 } = {}) {
    if (!measValues.length || !cut?.values.length) return null;

    const measPeak = normalize ? Math.max(...measValues) : 0;
    const refPeak = normalize ? Math.max(...cut.values) : 0;
    const clamp = (v) => Math.max(v, floorDb);

    let sumSq = 0, n = 0, max = 0, at = 0;
    for (let i = 0; i < measValues.length; i++) {
        const ref = sampleAt(cut, wrap180(measAngles[i] - rotateDeg));
        if (ref === null) continue;
        const d = clamp(measValues[i] - measPeak) - clamp(ref - refPeak);
        sumSq += d * d;
        n++;
        if (Math.abs(d) > Math.abs(max)) { max = d; at = measAngles[i]; }
    }
    return n ? { rms: Math.sqrt(sumSq / n), max, at, n, floorDb } : null;
}

/** Reference trace ready for the polar plot, rotated as configured. */
export function traceFor(cut, rotateDeg = 0) {
    if (!cut) return null;
    const angles = cut.angles.map((a) => a + rotateDeg);
    const spread = cut.angles.length > 2
        ? cut.angles[cut.angles.length - 1] - cut.angles[0]
        : 0;
    return { angles, values: cut.values, fullCircle: spread >= 350 };
}

/** Human label for a cut, used in the picker and the legend. */
export function cutLabel(cut) {
    const f = cut.freqHz ? `${(cut.freqHz / 1e9).toFixed(4)} GHz` : 'no freq';
    const head = cut.param && cut.param !== '—' ? `${cut.param} @ ${f}` : f;
    return cut.plane ? `${head}, ${cut.plane}` : head;
}
