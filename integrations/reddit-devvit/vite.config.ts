import { defineConfig } from "vite";

export default defineConfig({
  build: {
    lib: {
      entry: "src/server/index.ts",
      formats: ["cjs"],
      fileName: () => "index.cjs",
    },
    outDir: "dist/server",
    emptyOutDir: true,
    rollupOptions: {
      external: [
        "@devvit/redis",
        "@devvit/reddit",
        "@devvit/web/server",
      ],
    },
  },
});
