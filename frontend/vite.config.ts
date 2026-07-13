import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// El backend FastAPI sirve el build bajo `/static/`, así que los assets se
// referencian con esa base. En dev, Vite proxya las llamadas de datos al
// backend (uvicorn en :8000) para que fetch('/api/...') funcione igual que en prod.
//
// OJO: NO proxyamos GET `/login` porque esa ruta es una página del SPA (React
// Router). El endpoint de login del backend vive en POST `/api/login`.
const BACKEND = process.env.HUGO_BACKEND ?? "http://localhost:8000";

export default defineConfig(({ command }) => ({
  // En build (prod) el backend sirve los assets bajo /static/. En dev, Vite
  // sirve el SPA en la raíz para que React Router (rutas '/' y '/login') y el
  // proxy funcionen sin prefijo.
  base: command === "build" ? "/static/" : "/",
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": BACKEND,
      "/audit-log": BACKEND,
      "/audit": BACKEND,
      "/verify": BACKEND,
      "/health": BACKEND,
      "/products": BACKEND,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
}));
