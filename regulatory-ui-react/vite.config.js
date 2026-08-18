import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The UI talks to the FastAPI pipeline (api.py) at http://localhost:8000.
// Override at runtime by setting window.PIPELINE_API before the app loads,
// or by changing BACKEND in src/App.jsx.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173, host: true },
})
