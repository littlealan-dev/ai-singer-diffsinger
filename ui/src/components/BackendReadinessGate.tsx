import { ReactNode, useEffect, useMemo, useState } from "react";
import { Loader2 } from "lucide-react";
import { fetchBackendReadiness } from "../api";

type BackendReadinessGateProps = {
  children: ReactNode;
};

type ReadinessState = "checking" | "ready" | "timeout";

const POLL_INTERVAL_MS = 2000;
const BACKEND_WAITING_MESSAGE = "Please wait...";
const BACKEND_TIMEOUT_MESSAGE =
  "SightSinger is taking longer than expected to start. Please try again later.";

function backendReadyTimeoutMs(): number {
  const raw = import.meta.env.VITE_BACKEND_READY_TIMEOUT_SECONDS;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed * 1000 : 240000;
}

export function BackendReadinessGate({ children }: BackendReadinessGateProps) {
  const timeoutMs = useMemo(() => backendReadyTimeoutMs(), []);
  const [state, setState] = useState<ReadinessState>("checking");

  useEffect(() => {
    let cancelled = false;
    let timeoutId: number | undefined;
    let pollId: number | undefined;

    const checkReady = async () => {
      try {
        const readiness = await fetchBackendReadiness();
        if (cancelled) return;
        if (readiness.ready) {
          setState("ready");
          if (timeoutId !== undefined) window.clearTimeout(timeoutId);
          if (pollId !== undefined) window.clearInterval(pollId);
        } else {
          setState("checking");
        }
      } catch {
        if (!cancelled) {
          setState("checking");
        }
      }
    };

    timeoutId = window.setTimeout(() => {
      if (!cancelled) {
        setState((current) => (current === "ready" ? current : "timeout"));
        if (pollId !== undefined) window.clearInterval(pollId);
      }
    }, timeoutMs);
    void checkReady();
    pollId = window.setInterval(() => {
      void checkReady();
    }, POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
      if (pollId !== undefined) window.clearInterval(pollId);
    };
  }, [timeoutMs]);

  if (state === "ready") {
    return <>{children}</>;
  }

  return (
    <div className="backend-readiness-page">
      <div className="backend-readiness-modal" role="alertdialog" aria-modal="true">
        <Loader2 className="backend-readiness-spinner" size={28} aria-hidden="true" />
        <h2>{state === "timeout" ? "Still starting up" : "Bootstrapping studio"}</h2>
        <p>{state === "timeout" ? BACKEND_TIMEOUT_MESSAGE : BACKEND_WAITING_MESSAGE}</p>
        {state === "timeout" ? (
          <button
            type="button"
            className="backend-readiness-action"
            onClick={() => window.location.reload()}
          >
            Try again
          </button>
        ) : null}
      </div>
    </div>
  );
}
