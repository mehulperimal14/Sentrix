import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';

const r = (p: string) => fileURLToPath(new URL(p, import.meta.url));

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      { find: /^@sentrix\/ui$/, replacement: r('./packages/ui/src/index.ts') },
      { find: /^@sentrix\/ui\/(.*)$/, replacement: `${r('./packages/ui/src')}/$1` },
      { find: /^@sentrix\/api$/, replacement: r('./packages/api/src/index.ts') },
    ],
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: [r('./vitest.setup.ts')],
    include: ['packages/**/src/**/*.test.{ts,tsx}', 'apps/**/src/**/*.test.{ts,tsx}'],
    css: false,
  },
});
