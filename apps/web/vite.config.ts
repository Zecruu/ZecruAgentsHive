import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Port 5174 to avoid colliding with the desktop's Vite dev server (5173).
export default defineConfig({
  plugins: [react()],
  server: { port: 5174 },
});
