import { useEffect, useState } from "react";
import { initAnalytics } from "../firebase";

type ConsentState = "unknown" | "granted" | "denied";

const CONSENT_KEY = "analytics_consent";

export default function CookieBanner() {
  const [consent, setConsent] = useState<ConsentState>("unknown");
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = window.localStorage.getItem(CONSENT_KEY);
    if (stored === "granted") {
      setConsent("granted");
      initAnalytics();
      return;
    }
    if (stored === "denied") {
      setConsent("denied");
    }
  }, []);

  if (consent !== "unknown" || dismissed) return null;

  const handleAccept = () => {
    window.localStorage.setItem(CONSENT_KEY, "granted");
    setConsent("granted");
    initAnalytics();
  };

  const handleDecline = () => {
    window.localStorage.setItem(CONSENT_KEY, "denied");
    setConsent("denied");
  };

  return (
    <div className="cookie-banner" role="dialog" aria-live="polite" aria-label="Cookie consent">
      <button
        className="cookie-close"
        type="button"
        aria-label="Close cookie banner"
        onClick={() => setDismissed(true)}
      >
        ×
      </button>
      <div className="cookie-banner__content">
        <div className="cookie-banner__title">We use analytics cookies</div>
        <div className="cookie-banner__text">
          We use analytics cookies to understand how the site is used. You can accept or decline.
          See our{" "}
          <a className="cookie-banner__link" href="/legal/privacy">
            Privacy Policy
          </a>
          .
        </div>
      </div>
      <div className="cookie-banner__actions">
        <button className="cookie-button cookie-button--secondary" onClick={handleDecline}>
          Decline
        </button>
        <button className="cookie-button cookie-button--primary" onClick={handleAccept}>
          Accept
        </button>
      </div>
    </div>
  );
}
