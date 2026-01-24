import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// https://vite.dev/config/
export default defineConfig({
  // Use relative asset paths so the UI can be served under an arbitrary path prefix.
  // Example: https://host/<PATH_PREFIX>/ -> assets + API requests stay under the prefix.
  base: './',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: path.resolve(__dirname, '../static'),
    emptyOutDir: false,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:7860',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/login': 'http://localhost:7860',
      '/logout': 'http://localhost:7860',
      '/admin': 'http://localhost:7860',
      '/public': 'http://localhost:7860',
    },
  },
})
