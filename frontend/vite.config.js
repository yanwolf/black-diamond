import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 開發時前端跑在 5173，透過 proxy 轉發 /api 到本機的 FastAPI (8000)
// 正式打包後 (npm run build) 產出的 dist 會被 FastAPI 直接 serve，屆時 /api 走同源不需要 proxy。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
