import path from 'node:path'
import { fileURLToPath } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const rootDir = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(rootDir, './src'),
    },
  },
  server: {
    port: 5175,
    host: true,
    proxy: {
      '/api': {
        target: process.env.VITE_DEV_API_PROXY ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
