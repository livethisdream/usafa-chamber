import { defineConfig } from 'vite';

// Relative base so the built app can be served from any path - including the
// service's own static server, or straight off the filesystem.
export default defineConfig({
    base: './',
    build: { outDir: 'dist', emptyOutDir: true },
    server: { port: 5173, host: true },
});
