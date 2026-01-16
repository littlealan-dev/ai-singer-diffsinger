import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/__/auth/handler": {
        target: "http://127.0.0.1:9099",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/__\/auth\/handler/, "/emulator/auth/handler"),
      },
      "/emulator/auth/handler": {
        target: "http://127.0.0.1:9099",
        changeOrigin: true,
      },
      "/emulator/auth/iframe": {
        target: "http://127.0.0.1:9099",
        changeOrigin: true,
      },
      "/identitytoolkit.googleapis.com": {
        target: "http://127.0.0.1:9099",
        changeOrigin: true,
      },
      "/securetoken.googleapis.com": {
        target: "http://127.0.0.1:9099",
        changeOrigin: true,
      },
    },
  },
});
