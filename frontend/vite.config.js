import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:5000', changeOrigin: true },
      '/oauth2': { target: 'http://localhost:5000', changeOrigin: true },
      // Trailing slash matters: Vite's proxy keys are prefix-matched, and
      // '/download' (no slash) also matches '/downloads' -- silently
      // forwarding the React Downloads *page* route to the Flask backend
      // (which has no matching route for it) instead of letting Vite serve
      // the SPA. '/download/' only matches the real SMB file route, which
      // always has a path segment after it anyway (/download/<path>).
      '/download/': { target: 'http://localhost:5000', changeOrigin: true },
      '/debug': { target: 'http://localhost:5000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});