import { useEffect, useState } from "react";
import type { AnnualPromo } from "../../../shared/billingPlans";
import "./SaleCountdownBar.css";

type SaleCountdownBarProps = {
  promo: AnnualPromo;
  onOpen: () => void;
};

/** The sale's bar across the top of the app: a countdown to its end; it opens the paywall. Closing
 * it lasts until the page reloads, and it goes away by itself when the sale ends. */
export function SaleCountdownBar({ promo, onOpen }: SaleCountdownBarProps) {
  const [now, setNow] = useState(() => Date.now());
  const [closed, setClosed] = useState(false);
  const remaining = promo.endsAt.getTime() - now;
  const ended = remaining <= 0;

  useEffect(() => {
    if (closed || ended) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [closed, ended]);

  if (closed || ended) return null;
  return (
    <div className="sale-bar" role="status">
      <button type="button" className="sale-bar-main" onClick={onOpen}>
        <SaleCountdown milliseconds={remaining} />
        <span className="sale-bar-headline">
          <span className="sale-bar-title">{promo.label} sale</span>
          <span className="sale-bar-pill">{promo.percentOff}% off</span>
        </span>
        <span className="sale-bar-detail">Pro &amp; Max annual plans and Credit Packs</span>
        <span className="sale-bar-cta">Claim offer →</span>
      </button>
      <button
        type="button"
        className="sale-bar-close"
        onClick={() => setClosed(true)}
        aria-label={`Hide the ${promo.label} offer`}
      >
        ×
      </button>
    </div>
  );
}

/** "Ends in [07 d] [04 h] [50 m] [52 s]", like a digital clock; days drop off on the last day. */
function SaleCountdown({ milliseconds }: { milliseconds: number }) {
  const parts = countdownParts(milliseconds);
  return (
    <span className="sale-bar-countdown" aria-label={`Ends in ${parts.map(([value, unit]) => `${value}${unit}`).join(" ")}`}>
      <span className="sale-bar-ends" aria-hidden="true">Ends in</span>
      {parts.map(([value, unit]) => (
        <span key={unit} className="sale-bar-tile" aria-hidden="true">
          <span className="sale-bar-digits">{value}</span>
          <span className="sale-bar-unit">{unit}</span>
        </span>
      ))}
    </span>
  );
}

export function countdownParts(milliseconds: number): Array<[string, string]> {
  const total = Math.max(0, Math.floor(milliseconds / 1000));
  const days = Math.floor(total / 86400);
  const pad = (value: number) => String(value).padStart(2, "0");
  const parts: Array<[string, string]> = [
    [pad(Math.floor((total % 86400) / 3600)), "h"],
    [pad(Math.floor((total % 3600) / 60)), "m"],
    [pad(total % 60), "s"],
  ];
  return days > 0 ? [[pad(days), "d"], ...parts] : parts;
}
