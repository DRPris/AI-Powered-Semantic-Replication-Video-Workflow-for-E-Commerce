import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时通过 /api 代理到后端，避免跨域配置的麻烦；
// 生产构建后可由任意静态服务器托管，届时走后端 CORS 白名单。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        // 后端跑在 8010（宿主机 8000 被本机其他服务占用，见 .env 的 SERVICE_PORT）
        target: "http://localhost:8010",
        changeOrigin: true,
      },
    },
  },
});
