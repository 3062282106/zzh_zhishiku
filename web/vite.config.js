import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Served by FastAPI under /admin, so every asset URL is prefixed with /admin/.
// In dev, API calls are proxied to the local service so the app behaves the same
// as it does behind uvicorn.
export default defineConfig({
  base: '/admin/',
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/v1': { target: 'http://127.0.0.1:18081', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true, assetsDir: 'assets' },
})
