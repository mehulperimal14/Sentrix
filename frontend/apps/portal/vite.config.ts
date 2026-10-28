import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';

const r = (p: string) => fileURLToPath(new URL(p, import.meta.url));
const api = 'http://127.0.0.1:8100';

// Built files are served by the Python app under /static/app/ and committed,
// so the server needs no Node (docs/9_FRONTEND.md section 2).
export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/static/app/' : '/',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: [
      { find: /^@sentrix\/ui$/, replacement: r('../../packages/ui/src/index.ts') },
      { find: /^@sentrix\/ui\/(.*)$/, replacement: `${r('../../packages/ui/src')}/$1` },
      { find: /^@sentrix\/api$/, replacement: r('../../packages/api/src/index.ts') },
    ],
  },
  build: {
    outDir: r('../../../sentrix_portal/static/app'),
    emptyOutDir: true,
    assetsInlineLimit: 0,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: api, changeOrigin: false },
      '/d/': { target: api, ws: true, changeOrigin: false },
      '/ws': { target: api, ws: true, changeOrigin: false },
      '/video': { target: api, changeOrigin: false },
      '/media': { target: api, changeOrigin: false },
    },
  },
}));
