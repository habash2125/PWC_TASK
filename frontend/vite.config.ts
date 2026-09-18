import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    port: 3000,
    proxy: { "/api": { target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: {
    sourcemap: false,
    rollupOptions: { output: { manualChunks: { plotly: ["plotly.js-dist-min"], vendor: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"] } } },
    chunkSizeWarningLimit: 5000,
  },
});
