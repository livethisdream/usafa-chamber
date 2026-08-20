// WebSocket transport for browser mode.
//
// Mirrors the Phaser transport: a request/response layer keyed by `id` over the
// same socket that carries unsolicited push frames, with automatic reconnect.

export function createWebTransport(callbacks = {}) {
    let ws = null;
    let reconnectTimer = null;
    const pending = new Map();
    let requestId = 0;
    const host = window.location.hostname || 'localhost';
    const port = import.meta.env?.VITE_SERVICE_PORT || 8766;

    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

        ws = new WebSocket(`ws://${host}:${port}`);

        ws.onopen = () => {
            callbacks.onLog?.('info', 'WS', `Connected to ${host}:${port}`);
            callbacks.onOpen?.();
        };

        ws.onclose = () => {
            callbacks.onLog?.('warn', 'WS', 'Disconnected');
            callbacks.onClose?.();
            // Reject anything still outstanding, so callers are not left hanging.
            for (const [id, { reject }] of pending) reject(new Error('Disconnected'));
            pending.clear();
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 2000);
        };

        ws.onerror = () => callbacks.onLog?.('error', 'WS', 'Connection error');

        ws.onmessage = (event) => {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch (e) {
                callbacks.onLog?.('error', 'WS', `Parse error: ${e}`);
                return;
            }

            // Response to a pending request?
            if (data.id && pending.has(data.id)) {
                const { resolve, reject } = pending.get(data.id);
                pending.delete(data.id);
                if (data.ok) resolve(data.data);
                else reject(new Error(data.error || 'command failed'));
                return;
            }

            // Otherwise it is a push frame.
            switch (data.type) {
                case 'state':       callbacks.onState?.(data.data); break;
                case 'position':    callbacks.onPosition?.(data.deg); break;
                case 'scan_started':callbacks.onScanStarted?.(data); break;
                case 'scan_point':  callbacks.onScanPoint?.(data); break;
                case 'scan_done':   callbacks.onScanDone?.(data); break;
                case 'log':         callbacks.onLog?.(data.level, data.source, data.message); break;
                default:            callbacks.onMessage?.(data);
            }
        };
    }

    function invoke(cmd, args = {}, timeoutMs = 300000) {
        return new Promise((resolve, reject) => {
            if (!ws || ws.readyState !== WebSocket.OPEN) {
                reject(new Error('Not connected'));
                return;
            }
            const id = `req_${++requestId}`;
            pending.set(id, { resolve, reject });
            ws.send(JSON.stringify({ cmd, ...args, id }));
            setTimeout(() => {
                if (pending.has(id)) {
                    pending.delete(id);
                    reject(new Error('Timeout'));
                }
            }, timeoutMs);
        });
    }

    return {
        connect,
        invoke,
        getState:    ()      => invoke('get_state'),
        startScan:   (p)     => invoke('start_scan', p),
        cancelScan:  ()      => invoke('cancel_scan'),
        stop:        ()      => invoke('stop'),
        jog:         (a)     => invoke('jog', a),
        zeroHere:    ()      => invoke('zero_here'),
        setSpeed:    (pct)   => invoke('set_speed', { percent: pct }),
        listRuns:    ()      => invoke('list_runs'),
        loadRun:     (name)  => invoke('load_run', { name }),
        get isConnected() { return !!ws && ws.readyState === WebSocket.OPEN; },
    };
}
