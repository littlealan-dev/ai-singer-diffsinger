import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e/specs",
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? "../tests/output/ui-regression/artifacts/local",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [
    ["list"],
    [
      "html",
      {
        outputFolder: process.env.PLAYWRIGHT_HTML_REPORT ?? "../tests/output/ui-regression/report",
        open: "never",
      },
    ],
  ],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  timeout: 600_000,
  expect: { timeout: 30_000 },
  webServer: process.env.E2E_MANAGED_WEB_SERVER
    ? undefined
    : {
        command: "npm run dev -- --host 127.0.0.1 --port 5173 --strictPort",
        url: "http://127.0.0.1:5173/app",
        reuseExistingServer: !process.env.CI,
        env: {
          VITE_APP_ENV: "dev",
          VITE_E2E_AUTH: "1",
          VITE_API_BASE: "http://127.0.0.1:8000",
          VITE_FIREBASE_AUTH_EMULATOR_URL: "http://127.0.0.1:9099",
          VITE_FIREBASE_API_KEY: "fake-api-key",
          VITE_FIREBASE_AUTH_DOMAIN: "127.0.0.1",
          VITE_FIREBASE_PROJECT_ID: "demo-sightsinger-e2e",
          VITE_FIREBASE_STORAGE_BUCKET: "demo-sightsinger-e2e.appspot.com",
          VITE_FIREBASE_MESSAGING_SENDER_ID: "1234567890",
          VITE_FIREBASE_APP_ID: "1:1234567890:web:e2e",
        },
      },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
