import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const rootDir = fileURLToPath(new URL("..", import.meta.url));
const packageJson = JSON.parse(readFileSync(new URL("./package.json", import.meta.url), "utf8"));

function resolveBuildNumber(): string {
  try {
    return execSync("git rev-parse --short=8 HEAD", { cwd: rootDir, encoding: "utf8" }).trim();
  } catch {
    return "local";
  }
}

process.env.VITE_APP_VERSION ||= String(packageJson.version || "dev");
process.env.VITE_APP_BUILD_NUMBER ||= resolveBuildNumber();

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
      "/sessions": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/credits": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/maintenance/status": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/api/voicebanks": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/feedback": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/marketing/opt-in": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/auth": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/billing": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
      },
      "/waitlist/subscribe": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/healthz": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/readyz": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
