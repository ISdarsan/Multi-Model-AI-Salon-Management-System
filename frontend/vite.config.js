import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Vite builds and serves the dashboard. The two plugins add React and Tailwind CSS.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // The backend only allows the address http://localhost:5173 (CORS), so the dashboard must use exactly this port.
  server: { port: 5173, strictPort: true },
})
