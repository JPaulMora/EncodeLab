import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    proxy: {
      '/api': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/presets': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/upload': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/watch_queue': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/watch': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/queue': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/outputs': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/download': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/delete': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/status': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/health': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/media': { target: process.env.VITE_API_PROXY || 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': {
        target: (process.env.VITE_API_PROXY || 'http://127.0.0.1:8000').replace(/^http/, 'ws'),
        ws: true
      }
    }
  }
});
