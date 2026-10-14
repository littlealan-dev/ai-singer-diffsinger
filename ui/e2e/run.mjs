import { spawn } from "node:child_process";
import { mkdir, rm, symlink } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const uiDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const rootDir = path.resolve(uiDir, "..");
const outputDir = path.join(rootDir, "tests", "output", "ui-regression");
const runId = new Date().toISOString().replace(/[:.]/g, "-");
const projectId = "demo-sightsinger-e2e";
const children = [];

function start(command, args, options = {}) {
  const child = spawn(command, args, {
    stdio: "inherit",
    detached: process.platform !== "win32",
    ...options,
  });
  children.push(child);
  child.once("exit", (code) => {
    if (code && code !== 0) process.exitCode ||= code;
  });
  return child;
}

async function waitFor(url, label) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // The service is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`${label} did not become ready: ${url}`);
}

function cleanup() {
  for (const child of children.reverse()) {
    if (child.killed || !child.pid) continue;
    if (process.platform !== "win32") {
      try {
        process.kill(-child.pid, "SIGTERM");
        continue;
      } catch {
        // The group may have already exited; fall back to the direct child.
      }
    }
    child.kill("SIGTERM");
  }
}

process.once("SIGINT", () => {
  cleanup();
  process.exit(130);
});
process.once("SIGTERM", () => {
  cleanup();
  process.exit(143);
});

try {
  await mkdir(outputDir, { recursive: true });
  const provenanceDir = path.join(outputDir, "backend", "e2e-synthesis");
  const workerProvenanceLink = path.join(outputDir, "synthesis-worker", "e2e-synthesis");
  await mkdir(provenanceDir, { recursive: true });
  await mkdir(path.dirname(workerProvenanceLink), { recursive: true });
  // Share only test provenance; the API and worker keep separate data folders.
  await rm(workerProvenanceLink, { recursive: true, force: true });
  await symlink(path.join("..", "backend", "e2e-synthesis"), workerProvenanceLink, "dir");
  const env = {
    ...process.env,
    APP_ENV: "test",
    BACKEND_E2E_TEST_MODE: "1",
    BACKEND_E2E_CONTROL_TOKEN: "local-ui-regression-control",
    BACKEND_DATA_DIR: "tests/output/ui-regression/backend",
    PLAYWRIGHT_OUTPUT_DIR: path.join(outputDir, "artifacts", runId),
    PLAYWRIGHT_HTML_REPORT: path.join(outputDir, "report"),
    BACKEND_USE_STORAGE: "true",
    BACKEND_REQUIRE_APP_CHECK: "false",
    BACKEND_AUDIO_FORMAT: "wav",
    BACKEND_DEFAULT_VOICEBANK:
      process.env.REGRESSION_VOICEBANK || "Qixuan_v2.7.0_DiffSinger_OpenUtau",
    CORS_ALLOW_ORIGINS: "http://127.0.0.1:5173",
    FIREBASE_AUTH_EMULATOR_HOST: "127.0.0.1:9099",
    FIRESTORE_EMULATOR_HOST: "127.0.0.1:8080",
    FIREBASE_STORAGE_EMULATOR_HOST: "127.0.0.1:9199",
    STORAGE_EMULATOR_HOST: "http://127.0.0.1:9199",
    GOOGLE_CLOUD_PROJECT: projectId,
    STORAGE_BUCKET: `${projectId}.appspot.com`,
    LLM_PROVIDER: "regression",
    MCP_CPU_DEVICE: "cpu",
    MCP_GPU_DEVICE: process.env.REGRESSION_SYNTHESIS_DEVICE || "gpu",
    MCP_GPU_ENABLED: "false",
    E2E_MANAGED_WEB_SERVER: "1",
    VITE_APP_ENV: "dev",
    VITE_E2E_AUTH: "1",
    VITE_API_BASE: "http://127.0.0.1:8000",
    VITE_FIREBASE_AUTH_EMULATOR_URL: "http://127.0.0.1:9099",
    VITE_FIREBASE_API_KEY: "fake-api-key",
    VITE_FIREBASE_AUTH_DOMAIN: "127.0.0.1",
    VITE_FIREBASE_PROJECT_ID: projectId,
    VITE_FIREBASE_STORAGE_BUCKET: `${projectId}.appspot.com`,
    VITE_FIREBASE_MESSAGING_SENDER_ID: "1234567890",
    VITE_FIREBASE_APP_ID: "1:1234567890:web:e2e",
    // Line preparation runs on a separate worker backend, as in Cloud Run;
    // the main backend calls it over HTTP in place of Cloud Tasks.
    PREPROCESS_DISPATCH_MODE: "http",
    PREPROCESS_WORKER_URL: "http://127.0.0.1:8002",
    // The API and workers must see the same sessions.
    SESSION_STORE: "firestore",
    PREPROCESS_WORKER_TOKEN: "local-ui-regression-preprocess-worker",
    SYNTHESIS_DISPATCH_MODE: "http",
    SYNTHESIS_WORKER_URL: "http://127.0.0.1:8003",
    SYNTHESIS_WORKER_TOKEN: "local-ui-regression-synthesis-worker",
  };
  const workerEnv = {
    ...env,
    // Its own data folder, as the worker service has its own disk.
    BACKEND_DATA_DIR: "tests/output/ui-regression/preprocess-worker",
    MCP_GPU_ENABLED: "false",
    PREPROCESS_WORKER_ENABLED: "true",
    PREPROCESS_DISPATCH_MODE: "inline",
  };
  const synthesisWorkerEnv = {
    ...env,
    BACKEND_DATA_DIR: "tests/output/ui-regression/synthesis-worker",
    SYNTHESIS_WORKER_ENABLED: "true",
    SYNTHESIS_DISPATCH_MODE: "inline",
    PREPROCESS_DISPATCH_MODE: "inline",
    MCP_GPU_ENABLED: "true",
    MCP_GPU_DEVICE: process.env.REGRESSION_SYNTHESIS_DEVICE || "gpu",
  };

  start("firebase", ["emulators:start", "--only", "auth,firestore,storage", "--project", projectId], {
    cwd: rootDir,
    env,
  });
  await waitFor("http://127.0.0.1:9099", "Firebase Auth Emulator");

  const python = process.env.REGRESSION_PYTHON || path.join(rootDir, ".venv310", "bin", "python");
  start(python, ["-m", "uvicorn", "src.backend.main:app", "--host", "127.0.0.1", "--port", "8002"], {
    cwd: rootDir,
    env: workerEnv,
  });
  start(python, ["-m", "uvicorn", "src.backend.main:app", "--host", "127.0.0.1", "--port", "8003"], {
    cwd: rootDir,
    env: synthesisWorkerEnv,
  });
  start(python, ["-m", "uvicorn", "src.backend.main:app", "--host", "127.0.0.1", "--port", "8000"], {
    cwd: rootDir,
    env,
  });
  await waitFor("http://127.0.0.1:8002/readyz", "line-preparation worker");
  await waitFor("http://127.0.0.1:8003/readyz", "synthesis worker");
  await waitFor("http://127.0.0.1:8000/readyz", "backend");

  start("npm", ["run", "dev", "--", "--host", "127.0.0.1", "--port", "5173", "--strictPort"], {
    cwd: uiDir,
    env,
  });
  await waitFor("http://127.0.0.1:5173/app", "UI");

  const test = start("npm", ["exec", "--", "playwright", "test", ...process.argv.slice(2)], {
    cwd: uiDir,
    env: { ...env, CI: "1" },
  });
  const exitCode = await new Promise((resolve) => test.once("exit", (code) => resolve(code ?? 1)));
  if (exitCode !== 0) process.exitCode = exitCode;
} finally {
  cleanup();
}
