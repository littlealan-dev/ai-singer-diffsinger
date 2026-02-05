import { Sparkles } from "lucide-react";
import "./styles.css";

export default function WaitlistConfirmed() {
  const marketingBaseUrl =
    (import.meta.env.VITE_MARKETING_BASE_URL as string | undefined) ?? "/";
  return (
    <div className="waitlist-confirmed">
      <div className="waitlist-confirmed-card">
        <Sparkles className="brand-icon" />
        <h1>Subscription Confirmed</h1>
        <p>Thanks for confirming. We'll keep you posted on paid plan updates.</p>
        <button onClick={() => window.location.assign(marketingBaseUrl)}>
          Back to Home
        </button>
      </div>
    </div>
  );
}
