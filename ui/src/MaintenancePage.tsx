import { Link, useLocation } from "react-router-dom";
import { Wrench } from "lucide-react";
import { logOut } from "./firebase";

type MaintenancePageProps = {
  message?: string | null;
  showAppLink?: boolean;
};

export default function MaintenancePage({ message, showAppLink = false }: MaintenancePageProps) {
  const location = useLocation();
  const marketingBaseUrl = resolveMarketingBaseUrl(
    import.meta.env.VITE_MARKETING_BASE_URL as string | undefined
  );
  const stateMessage =
    typeof location.state === "object" &&
    location.state &&
    "message" in location.state &&
    typeof location.state.message === "string"
      ? location.state.message
      : null;
  const displayMessage = message || stateMessage;

  const handleReturnToHome = async () => {
    try {
      await logOut();
    } finally {
      if (typeof window !== "undefined") {
        window.location.assign(marketingBaseUrl);
      }
    }
  };

  return (
    <main className="maintenance-page">
      <section className="maintenance-panel" aria-labelledby="maintenance-title">
        <div className="maintenance-icon" aria-hidden="true">
          <Wrench size={30} />
        </div>
        <h1 id="maintenance-title">Under maintenance</h1>
        <p>
          {displayMessage ||
            "SightSinger is temporarily under maintenance. Sorry for the inconvenience caused."}
        </p>
        <div className="maintenance-actions">
          {showAppLink ? (
            <Link className="maintenance-button primary" to="/app">
              Continue to app
            </Link>
          ) : null}
          <button className="maintenance-button" type="button" onClick={() => void handleReturnToHome()}>
            Return to home
          </button>
        </div>
      </section>
    </main>
  );
}

function resolveMarketingBaseUrl(value?: string): string {
  if (value) return value.replace(/\/$/, "");
  if (typeof window === "undefined") return "/";
  const baseHost = window.location.host.startsWith("app.")
    ? window.location.host.slice(4)
    : window.location.host;
  return `${window.location.protocol}//${baseHost}`;
}
