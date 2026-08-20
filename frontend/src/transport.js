/**
 * transport.js — transport facade
 *
 * Keeps a stable interface for main.js while the underlying transport can
 * change. Only the web (WebSocket) transport exists today; the desktop hosts
 * Phaser supports (PyWebView IPC, Tauri, Electron) would slot in here the same
 * way, which is why the indirection is worth having this early.
 */

import { createWebTransport } from './transport-web.js';

function resolveTransportMode() {
    if (window.__ELECTRON__) return 'electron';
    if (window.__TAURI_INTERNALS__) return 'tauri';
    if (window.location?.protocol === 'file:') return 'ipc';
    if (window.pywebview?.api?.invoke) return 'ipc';
    return 'web';
}

export function createTransport(callbacks = {}) {
    const mode = resolveTransportMode();
    if (mode !== 'web') {
        callbacks.onLog?.('warn', 'Transport',
            `${mode} host detected but only the web transport is implemented; using WebSocket`);
    }
    return createWebTransport(callbacks);
}
