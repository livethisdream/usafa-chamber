import { defineConfig } from 'vite';

// Relative asset URLs, so a built bundle also loads from file:// if this ever
// gets wrapped in a desktop host the way Phaser is.
export default defineConfig({
  base: './',
  server: { host: true },
});
