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
                    'theta_deg', 'theta', 'phi_deg', 'phi', 'deg'];
const VALUE_KEYS = ['mag_db', 'gain_db', 'gain_dbi', 'db', 'amplitude_db',
                    'magnitude_db', 'directivity_db', 'gain', 'magnitude'];
const PARAM_KEYS = ['param', 'parameter', 's_param'];
const FREQ_KEYS = ['freq_hz', 'frequency_hz', 'freq', 'frequency', 'f_hz'];

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

/**
 * Parse CSV text into cuts keyed by "param @ freq".
 *
 * @returns {{cuts: Map<string, {param: string, freqHz: number|null,
 *                              angles: number[], values: number[]}>,
 *            rows: number}}
 * @throws {Error} when no angle/dB column pair can be found
 */
export function parsePattern(text) {
    const lines = text.split(/\r?\n/)
        .filter((l) => l.trim() && !l.trim().startsWith('#') && !l.trim().startsWith('!'));
    if (lines.length < 2) throw new Error('file has no data rows');

    const header = splitLine(lines[0]);
    const iAngle = findColumn(header, ANGLE_KEYS);
    const iValue = findColumn(header, VALUE_KEYS);
    if (iAngle < 0 || iValue < 0) {
        throw new Error(`no angle/dB columns in header: ${header.slice(0, 8).join(', ')}`);
    }
    const iParam = findColumn(header, PARAM_KEYS);
    const iFreq = findColumn(header, FREQ_KEYS);

    const cuts = new Map();
    let rows = 0;

    for (let n = 1; n < lines.length; n++) {
        const c = splitLine(lines[n]);
        const angle = parseFloat(c[iAngle]);
        const value = parseFloat(c[iValue]);
        if (!Number.isFinite(angle) || !Number.isFinite(value)) continue;

        const param = iParam >= 0 ? (c[iParam] || '—') : '—';
        let freqHz = iFreq >= 0 ? parseFloat(c[iFreq]) : NaN;
        if (!Number.isFinite(freqHz)) freqHz = null;
        // A GHz column read as Hz would sort and label wrong; scale it up.
        else if (freqHz > 0 && freqHz < 1e6) freqHz *= 1e9;

        const key = `${param}|${freqHz ?? ''}`;
        let cut = cuts.get(key);
        if (!cut) {
            cut = { param, freqHz, angles: [], values: [] };
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
    return { cuts, rows };
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
 * @returns {{rms: number, max: number, at: number, n: number}|null}
 */
export function compare(measAngles, measValues, cut, { normalize = true, rotateDeg = 0 } = {}) {
    if (!measValues.length || !cut?.values.length) return null;

    const measPeak = normalize ? Math.max(...measValues) : 0;
    const refPeak = normalize ? Math.max(...cut.values) : 0;

    let sumSq = 0, n = 0, max = 0, at = 0;
    for (let i = 0; i < measValues.length; i++) {
        const ref = sampleAt(cut, wrap180(measAngles[i] - rotateDeg));
        if (ref === null) continue;
        const d = (measValues[i] - measPeak) - (ref - refPeak);
        sumSq += d * d;
        n++;
        if (Math.abs(d) > Math.abs(max)) { max = d; at = measAngles[i]; }
    }
    return n ? { rms: Math.sqrt(sumSq / n), max, at, n } : null;
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
    return cut.param && cut.param !== '—' ? `${cut.param} @ ${f}` : f;
}
