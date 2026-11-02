import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import * as path from 'node:path';

/**
 * Two renderer entry points are built into desktop/dist/renderer:
 *  - index.html   the assistant window
 *  - overlay.html the transparent highlight overlay
 */
export default defineConfig({
  root: path.resolve(__dirname, 'renderer'),
  base: './',
  plugins: [react()],
  build: {
    outDir: path.resolve(__dirname, 'dist', 'renderer'),
    emptyOutDir: true,
    target: 'chrome120',
    sourcemap: false,
    rollupOptions: {
      input: {
        index: path.resolve(__dirname, 'renderer', 'index.html'),
        overlay: path.resolve(__dirname, 'renderer', 'overlay.html'),
      },
    },
  },
});
