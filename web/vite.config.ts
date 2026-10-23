import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时前端跑在 5173，后端 FastAPI 在 8000。
// 前端所有请求统一带 /api 前缀，由这里的代理转发并剥掉前缀，
// 浏览器看到的始终是同源请求，不会触发跨域拦截。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
