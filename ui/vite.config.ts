import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The production build goes straight into the Python package, which serves it.
// In development, `npm run dev` proxies the API and WebSocket to `devpilot-ui`
// (start it with DEVPILOT_UI_DEV_ORIGIN=http://localhost:5173 so the WebSocket accepts the dev page).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  build: {
    outDir: "../devpilot_ui/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
});
