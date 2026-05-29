import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'node:path';

// `base: "./"` is critical — Electron loads index.html via file:// in
// production, so all asset URLs must be relative to the html file.
export default defineConfig({
  base: './',
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'chrome120',
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
