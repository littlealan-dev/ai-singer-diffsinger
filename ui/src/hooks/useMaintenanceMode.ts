import { useEffect, useMemo, useState } from "react";
import type { User } from "firebase/auth";
import { fetchMaintenanceStatus } from "../api";

export type MaintenanceConfig = {
  enabled: boolean;
  message: string | null;
  allowed: boolean;
};

export type MaintenanceState = {
  loading: boolean;
  enabled: boolean;
  allowed: boolean;
  message: string | null;
  error: string | null;
};

const DEFAULT_MESSAGE =
  "SightSinger is temporarily under maintenance. Sorry for the inconvenience caused.";

const DEFAULT_CONFIG: MaintenanceConfig = {
  enabled: false,
  message: null,
  allowed: true,
};

export function useMaintenanceMode(user: User | null): MaintenanceState {
  const [config, setConfig] = useState<MaintenanceConfig>(DEFAULT_CONFIG);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!user) {
      setConfig(DEFAULT_CONFIG);
      setLoading(false);
      setError(null);
      return;
    }

    let cancelled = false;
    setLoading(true);
    fetchMaintenanceStatus()
      .then((status) => {
        if (cancelled) return;
        setConfig({
          enabled: status.enabled === true,
          allowed: status.allowed !== false,
          message:
            typeof status.message === "string" && status.message.trim()
              ? status.message.trim()
              : null,
        });
        setError(null);
      })
      .catch((statusError) => {
        if (cancelled) return;
        console.error("Error loading maintenance settings:", statusError);
        if (import.meta.env.VITE_APP_ENV === "dev") {
          setConfig(DEFAULT_CONFIG);
          setError(null);
          return;
        }
        setConfig({ ...DEFAULT_CONFIG, enabled: true, allowed: false });
        setError("Could not verify service availability.");
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [user?.uid]);

  return useMemo(() => {
    return {
      loading,
      enabled: config.enabled,
      allowed: !config.enabled || config.allowed,
      message: config.message || DEFAULT_MESSAGE,
      error,
    };
  }, [config, error, loading]);
}
