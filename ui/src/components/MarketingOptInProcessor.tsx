import { useEffect, useRef, useState } from "react";
import { doc, onSnapshot } from "firebase/firestore";
import { db } from "../firebase";
import { useAuth } from "../hooks/useAuth";
import "./MarketingOptInProcessor.css";
import {
  processPendingMarketingOptIn,
  type MarketingOptInState,
} from "../marketingOptIn";

type FirestoreTimestampLike = {
  toDate?: () => Date;
};

const DEFAULT_STATE: MarketingOptInState = {
  emailOptInRequested: null,
  emailOptInBrevoStatus: null,
  emailOptInRequestedAt: null,
  loading: true,
};

export function MarketingOptInProcessor() {
  const { user, authReady, isAuthenticated } = useAuth();
  const [marketingState, setMarketingState] = useState<MarketingOptInState>(DEFAULT_STATE);
  const [notice, setNotice] = useState<string | null>(null);
  const processedUserRef = useRef<string | null>(null);

  useEffect(() => {
    if (!user || !isAuthenticated) {
      setMarketingState({ ...DEFAULT_STATE, loading: false });
      processedUserRef.current = null;
      return;
    }

    setMarketingState({ ...DEFAULT_STATE, loading: true });
    const unsubscribe = onSnapshot(
      doc(db, "users", user.uid),
      (snapshot) => {
        if (!snapshot.exists()) {
          setMarketingState({ ...DEFAULT_STATE, loading: false });
          return;
        }
        const data = snapshot.data();
        const marketing = data.marketing && typeof data.marketing === "object" ? data.marketing : {};
        const requested =
          typeof marketing.emailOptInRequested === "boolean"
            ? marketing.emailOptInRequested
            : null;
        setMarketingState({
          emailOptInRequested: requested,
          emailOptInBrevoStatus:
            typeof marketing.emailOptInBrevoStatus === "string"
              ? marketing.emailOptInBrevoStatus
              : null,
          emailOptInRequestedAt: toDate(marketing.emailOptInRequestedAt),
          loading: false,
        });
      },
      (error) => {
        console.error("Error listening to marketing opt-in state:", error);
        setMarketingState({ ...DEFAULT_STATE, loading: false });
      }
    );

    return () => unsubscribe();
  }, [user, isAuthenticated]);

  useEffect(() => {
    if (!authReady || !user || !isAuthenticated || marketingState.loading) return;
    const processingKey = `${user.uid}:${marketingState.emailOptInRequested ?? "unknown"}`;
    if (processedUserRef.current === processingKey) return;
    processedUserRef.current = processingKey;
    void processPendingMarketingOptIn(user, marketingState).then((result) => {
      if (result.status === "processed" && result.backendStatus === "doi_requested") {
        setNotice("Please check your email to confirm SightSinger updates.");
      }
    });
  }, [authReady, user, isAuthenticated, marketingState]);

  if (!notice) return null;

  return (
    <div className="marketing-opt-in-notice" role="status" aria-live="polite">
      <span>{notice}</span>
      <button
        type="button"
        className="marketing-opt-in-notice-close"
        onClick={() => setNotice(null)}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  );
}

function toDate(value: unknown): Date | null {
  if (!value) return null;
  if (value instanceof Date) return value;
  if (typeof value === "string") {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }
  const timestamp = value as FirestoreTimestampLike;
  return typeof timestamp.toDate === "function" ? timestamp.toDate() : null;
}
