import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/mcp": {
        target: "http://localhost:8000",
        changeOrigin: true,
        xfwd: true,
      },
      "/.well-known": {
        target: "http://localhost:8000",
        changeOrigin: true,
        xfwd: true,
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks: {
          maplibre: ["maplibre-gl"],
          pmtiles: ["pmtiles"],
        },
      },
    },
  },
});
