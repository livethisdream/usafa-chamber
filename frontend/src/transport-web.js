/**
 * transport-web.js — WebSocket transport to the headless chamber service.
 *
 * Reconnects on its own, because a lab machine's service gets restarted a lot
 * and a dead page that needs a manual refresh is worse than useless mid-run.
 */

const DEFAULT_PORT = 8765;

export function createWebTransport(callbacks = {}) {
    const url = resolveUrl();
    let ws = null;
    let closed = false;
    let backoff = 500;
    let timer = null;

    function resolveUrl() {
        const override = new URLSearchParams(window.location.search).get('ws');
        if (override) return override;
        const host = window.location.hostname || 'localhost';
        return `ws://${host}:${DEFAULT_PORT}`;
    }

    function connect() {
        if (closed) return;
        callbacks.onLog?.('info', 'link', `connecting to ${url}`);
        ws = new WebSocket(url);

        ws.onopen = () => {
            backoff = 500;
            callbacks.onLink?.(true);
            send({ cmd: 'get_state' });
        };

        ws.onmessage = (e) => {
            let msg;
            try {
                msg = JSON.parse(e.data);
            } catch {
                callbacks.onLog?.('error', 'link', 'malformed frame from service');
                return;
            }
            callbacks.onEvent?.(msg);
        };

        ws.onclose = () => {
            callbacks.onLink?.(false);
            if (closed) return;
            timer = setTimeout(connect, backoff);
            backoff = Math.min(backoff * 2, 8000);
        };

        // onerror is always followed by onclose; let onclose own the retry.
        ws.onerror = () => {};
    }

    function send(msg) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(msg));
            return true;
        }
        callbacks.onLog?.('warn', 'link', 'not connected; command dropped');
        return false;
    }

    connect();

    return {
        mode: 'web',
        url,
        send,
        start: (config) => send({ cmd: 'start', config }),
        abort: () => send({ cmd: 'abort' }),
        connectInstruments: (config) => send({ cmd: 'connect', config }),
        disconnect: () => send({ cmd: 'disconnect' }),
        close: () => {
            closed = true;
            clearTimeout(timer);
            ws?.close();
        },
    };
}
