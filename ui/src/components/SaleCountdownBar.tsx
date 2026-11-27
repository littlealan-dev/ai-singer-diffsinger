import { useEffect, useState } from "react";
import type { AnnualPromo } from "../../../shared/billingPlans";
import "./SaleCountdownBar.css";

type SaleCountdownBarProps = {
  promo: AnnualPromo;
  onOpen: () => void;
};

/** The sale's bar above the header: a countdown to its end; it opens the paywall. Closing it lasts
 * until the page reloads, and it goes away by itself when the sale ends. */
export function SaleCountdownBar({ promo, onOpen }: SaleCountdownBarProps) {
  const [now, setNow] = useState(() => Date.now());
  const [closed, setClosed] = useState(false);
  const remaining = promo.endsAt.getTime() - now;

  useEffect(() => {
    if (closed || remaining <= 0) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [closed, remaining <= 0]);

  if (closed || remaining <= 0) return null;
  return (
    <div className="sale-countdown-bar" role="status">
      <button type="button" className="sale-countdown-link" onClick={onOpen}>
        <span>
          <strong>{promo.label}:</strong> {promo.percentOff}% off Pro &amp; Max annual plans and Credit Packs
        </span>
        <span className="sale-countdown-clock" aria-label="Time left">
          Ends in {formatCountdown(remaining)}
        </span>
      </button>
      <button
        type="button"
        className="sale-countdown-close"
        onClick={() => setClosed(true)}
        aria-label={`Hide the ${promo.label} offer`}
      >
        ×
      </button>
    </div>
  );
}

/** 6d 05h 12m 09s */
export function formatCountdown(milliseconds: number): string {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const days = Math.floor(total / 86400);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${days}d ${pad(Math.floor((total % 86400) / 3600))}h ${pad(Math.floor((total % 3600) / 60))}m ${pad(total % 60)}s`;
}
