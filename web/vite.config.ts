import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2020',
    cssCodeSplit: false,
    lib: {
      entry: 'src/main.ts',
      name: 'MoveToCache',
      formats: ['iife'],
      fileName: () => 'app.js',
      cssFileName: 'app'
    }
  }
});
