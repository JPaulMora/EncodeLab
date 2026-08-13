import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const apiTarget = process.env.VITE_API_PROXY || 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    host: true,
    // Allow access via LAN / docker-runner.local (Vite blocks unknown Host headers)
    allowedHosts: [
      'docker-runner.local',
      'localhost',
      '.local'
    ],
    proxy: {
      '/api': { target: apiTarget, changeOrigin: true },
      '/presets': { target: apiTarget, changeOrigin: true },
      '/upload': { target: apiTarget, changeOrigin: true },
      '/watch_queue': { target: apiTarget, changeOrigin: true },
      '/watch': { target: apiTarget, changeOrigin: true },
      '/queue': { target: apiTarget, changeOrigin: true },
      '/outputs': { target: apiTarget, changeOrigin: true },
      '/download': { target: apiTarget, changeOrigin: true },
      '/delete': { target: apiTarget, changeOrigin: true },
      '/status': { target: apiTarget, changeOrigin: true },
      '/health': { target: apiTarget, changeOrigin: true },
      '/media': { target: apiTarget, changeOrigin: true },
      '/ws': {
        target: apiTarget.replace(/^http/, 'ws'),
        ws: true
      }
    }
  }
});
