import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// base '/app/' so the built asset URLs resolve under the server's /app mount
// (the agentshive Railway server serves this bundle at /app, same origin).
// Port 5174 to avoid colliding with the desktop's Vite dev server (5173).
export default defineConfig({
  base: '/app/',
  plugins: [react()],
  server: { port: 5174 },
});
