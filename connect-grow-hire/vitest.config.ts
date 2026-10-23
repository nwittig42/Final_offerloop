/// <reference types="vitest" />
//
// Vitest config separate from vite.config.ts on purpose. The production Vite
// build has tightly tuned chunk-splitting rules ("Cannot access before
// initialization" lurks behind any change to vendor-react), and pulling test
// config into that file risks dragging the test runner into the production
// dependency graph.
//
// This file inherits Vite's resolve.alias for "@" but pins the test
// environment to jsdom and points Vitest at the same React-aware Vite plugin
// the dev server uses so component tests render the same way the app does.

import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react-swc'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    // Full-flow component tests (onboarding, walkthrough) render whole pages
    // with many async steps; 5s timed them out on a loaded machine while
    // each passes in under a second alone.
    testTimeout: 15000,
    setupFiles: ['./src/test/setup.ts'],
    // Co-locate tests with code under src/**/__tests__/ AND src/**/*.test.tsx
    // so component tests sit next to the component they exercise. Exclude
    // node_modules and the production build output (just in case).
    include: ['src/**/*.{test,spec}.{ts,tsx}', 'src/**/__tests__/**/*.{ts,tsx}'],
    // Exclude pre-existing standalone Node scripts that use the .test.ts
    // suffix but are not Vitest-compatible (they call process.exit directly).
    exclude: ['node_modules', 'dist', 'src/utils/generateMeta.test.ts'],
    // CSS is loaded but ignored in tests; nothing here renders to a real
    // browser so style assertions would lie. Keep it off.
    css: false,
  },
})
