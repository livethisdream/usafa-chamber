/**
 * transport.js — transport facade.
 *
 * Same shape as Phaser's: main.js talks to one stable interface while the
 * implementation underneath can be the web socket, a desktop IPC bridge, Tauri,
 * or Electron. Only the web transport exists today; the others are wired to
 * throw a clear message rather than fail obscurely, so adding one later is a
 * new file plus a branch here.
 *
 * Interface:
 *   send(msg)                 raw command
 *   start(config)             begin a scan
 *   abort()                   stop the running scan
 *   connectInstruments(cfg)   open the instruments without scanning
 *   disconnect()              close the instruments
 *   close()                   tear down the transport itself
 */

import { createWebTransport } from './transport-web.js';

function resolveTransportMode() {
    if (window.__ELECTRON__) return 'electron';
    if (window.__TAURI_INTERNALS__) return 'tauri';
    if (window.__CHAMBER_TRANSPORT === 'ipc') return 'ipc';
    if (window.location?.protocol === 'file:') return 'ipc';
    if (window.pywebview?.api?.invoke) return 'ipc';
    return 'web';
}

function notImplemented(mode, callbacks) {
    const message = `${mode} transport is not implemented yet; ` +
        `serve the UI over HTTP so the web transport is used`;
    callbacks.onLog?.('error', 'link', message);
    const fail = () => {
        callbacks.onLog?.('error', 'link', message);
        return false;
    };
    return {
        mode, url: null, send: fail, start: fail, abort: fail,
        connectInstruments: fail, disconnect: fail, close: () => {},
    };
}

export function createTransport(callbacks = {}) {
    const mode = resolveTransportMode();
    if (mode === 'web') {
        return createWebTransport(callbacks);
    }
    return notImplemented(mode, callbacks);
}
