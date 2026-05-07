import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useAuth } from "../hooks/useAuth";
import { useMaintenanceMode } from "../hooks/useMaintenanceMode";

type MaintenanceGateProps = {
  children: ReactNode;
};

export function MaintenanceGate({ children }: MaintenanceGateProps) {
  const { user } = useAuth();
  const maintenance = useMaintenanceMode(user);

  if (maintenance.loading) {
    return (
      <div className="protected-route-loading">
        <Loader2 className="protected-route-spinner" size={32} />
        <p>Checking service status...</p>
      </div>
    );
  }

  if (maintenance.enabled && !maintenance.allowed) {
    return (
      <Navigate
        to="/maintenance"
        replace
        state={{ message: maintenance.error || maintenance.message }}
      />
    );
  }

  return <>{children}</>;
}
