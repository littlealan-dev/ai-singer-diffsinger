import { useEffect, useMemo, useState } from "react";
import { useAuth } from "../hooks/useAuth.tsx";
import { subscribeToWaitlist } from "../api";
import "./WaitingListForm.css";

export type WaitlistSource =
  | "landing"
  | "hero_footer"
  | "menu"
  | "demo_menu"
  | "studio_menu"
  | "credits_exhausted"
  | "trial_expired";

const CONSENT_TEXT =
  "I agree to receive product updates, marketing emails, and announcements about SightSinger.ai paid plans. I can unsubscribe at any time.";

interface WaitingListFormProps {
  source: WaitlistSource;
}

export function WaitingListForm({ source }: WaitingListFormProps) {
  const { user } = useAuth();
  const initialEmail = useMemo(() => user?.email ?? "", [user?.email]);
  const initialFirstName = useMemo(() => {
    const displayName = user?.displayName?.trim();
    return displayName ? displayName.split(/\s+/)[0] : "";
  }, [user?.displayName]);
  const isEmailLocked = Boolean(user?.email);
  const isNameLocked = Boolean(user?.displayName);
  const [email, setEmail] = useState(initialEmail);
  const [firstName, setFirstName] = useState(initialFirstName);
  const [consent, setConsent] = useState(false);
  const [status, setStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (user?.email) {
      setEmail(user.email);
    }
    if (user?.displayName) {
      setFirstName(initialFirstName);
    }
  }, [initialFirstName, user?.displayName, user?.email]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!consent) {
      setMessage("Please agree to receive updates to continue.");
      setStatus("error");
      return;
    }
    setStatus("loading");
    setMessage(null);
    try {
      const result = await subscribeToWaitlist({
        email: email.trim(),
        first_name: firstName.trim() || undefined,
        gdpr_consent: consent,
        consent_text: CONSENT_TEXT,
        source,
      });
      setStatus("success");
      setMessage(result.message);
    } catch (error) {
      setStatus("error");
      setMessage("Something went wrong. Please try again later.");
    }
  };

  if (status === "success") {
    return (
      <div className="waitlist-success">
        <h3>Check Your Email</h3>
        <p>We've sent a confirmation email to:</p>
        <strong>{email}</strong>
        <p>Click the link in the email to confirm your subscription.</p>
      </div>
    );
  }

  return (
    <form className="waitlist-form" onSubmit={handleSubmit}>
      <div className="waitlist-header">
        <h3>Join the Waiting List</h3>
        <p>Get notified when paid plans are available.</p>
      </div>
      <label className="waitlist-field">
        Email
        <input
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder="you@example.com"
          required
          readOnly={isEmailLocked}
        />
      </label>
      <label className="waitlist-field">
        First name (optional)
        <input
          type="text"
          value={firstName}
          onChange={(event) => setFirstName(event.target.value)}
          placeholder="First name"
          readOnly={isNameLocked}
        />
      </label>
      <label className="waitlist-consent">
        <input
          type="checkbox"
          checked={consent}
          onChange={(event) => setConsent(event.target.checked)}
        />
        <span>{CONSENT_TEXT}</span>
      </label>
      {message && <div className={`waitlist-message ${status}`}>{message}</div>}
      <button type="submit" disabled={status === "loading"}>
        {status === "loading" ? "Joining..." : "Join Waiting List"}
      </button>
      <span className="waitlist-protection">Protected by Firebase App Check</span>
    </form>
  );
}
