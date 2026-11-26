import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

export default defineConfig({
  publicDir: false,
  plugins: [
    viteSingleFile({
      outputFileName: "explorer.html",
      removeViteModuleLoader: true,
    }),
  ],
  build: {
    outDir: "dist-mcp",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    cssCodeSplit: false,
    assetsInlineLimit: 100_000_000,
    rollupOptions: {
      input: fileURLToPath(new URL("./mcp-app.html", import.meta.url)),
    },
  },
});
