import { useEffect, useMemo, useRef, useState } from "react";
import { Bell, ChevronDown, CreditCard, LogOut } from "lucide-react";
import { useAuth } from "../hooks/useAuth.tsx";
import { logOut } from "../firebase";
import { formatPlanBadge, type BillingPlanKey } from "../billing/plans";

const getInitials = (name: string) => {
  const parts = name.trim().split(/\s+/);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0][0]?.toUpperCase() ?? "?";
  return `${parts[0][0] ?? ""}${parts[1][0] ?? ""}`.toUpperCase() || "?";
};

const resolveMarketingBaseUrl = (value?: string): string => {
  if (value) return value.replace(/\/$/, "");
  if (typeof window === "undefined") return "/";
  const baseHost = window.location.host.startsWith("app.")
    ? window.location.host.slice(4)
    : window.location.host;
  return `${window.location.protocol}//${baseHost}`;
};

const GET_UPDATES_PROMPT_STORAGE_PREFIX = "sightsinger.getUpdatesPromptLastShown";
const DEFAULT_GET_UPDATES_PROMPT_INTERVAL_DAYS = 1;

function getGetUpdatesPromptIntervalDays(): number {
  const raw = import.meta.env.VITE_GET_UPDATES_PROMPT_INTERVAL_DAYS as string | undefined;
  if (!raw) return DEFAULT_GET_UPDATES_PROMPT_INTERVAL_DAYS;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed >= 0
    ? parsed
    : DEFAULT_GET_UPDATES_PROMPT_INTERVAL_DAYS;
}

function shouldShowGetUpdatesPrompt(uid: string): boolean {
  if (typeof window === "undefined") return false;
  const intervalDays = getGetUpdatesPromptIntervalDays();
  const storageKey = `${GET_UPDATES_PROMPT_STORAGE_PREFIX}:${uid}`;
  const rawLastShown = window.localStorage.getItem(storageKey);
  if (!rawLastShown) return true;
  const lastShown = Number.parseInt(rawLastShown, 10);
  if (!Number.isFinite(lastShown)) return true;
  return Date.now() - lastShown >= intervalDays * 24 * 60 * 60 * 1000;
}

function recordGetUpdatesPromptShown(uid: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(
    `${GET_UPDATES_PROMPT_STORAGE_PREFIX}:${uid}`,
    String(Date.now())
  );
}

type UserMenuProps = {
  onJoinWaitlist?: () => void;
  onBilling?: () => void;
  activePlanKey?: BillingPlanKey;
  stripeCustomerId?: string | null;
};

export function UserMenu({
  onJoinWaitlist,
  onBilling,
  activePlanKey = "free",
  stripeCustomerId = null,
}: UserMenuProps) {
  const { user, isAuthenticated } = useAuth();
  const [open, setOpen] = useState(false);
  const [showGetUpdatesPrompt, setShowGetUpdatesPrompt] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const promptedUserRef = useRef<string | null>(null);

  const displayName = user?.displayName?.trim() || "Signed in";
  const email = user?.email?.trim() || "";
  const planBadge = formatPlanBadge(activePlanKey);
  const hasJoinWaitlist = Boolean(onJoinWaitlist);
  const userUid = user?.uid ?? null;
  const resolvedMarketingBaseUrl = resolveMarketingBaseUrl(
    import.meta.env.VITE_MARKETING_BASE_URL as string | undefined
  );
  const initials = useMemo(() => {
    const base = user?.displayName || user?.email || "";
    return getInitials(base);
  }, [user?.displayName, user?.email]);

  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setOpen(false);
        setShowGetUpdatesPrompt(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        setShowGetUpdatesPrompt(false);
      }
    };
    window.addEventListener("mousedown", onClick);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!isAuthenticated || !userUid || !hasJoinWaitlist) return;
    if (promptedUserRef.current === userUid) return;
    promptedUserRef.current = userUid;
    if (!shouldShowGetUpdatesPrompt(userUid)) return;

    const timer = window.setTimeout(() => {
      setOpen(true);
      setShowGetUpdatesPrompt(true);
      recordGetUpdatesPromptShown(userUid);
    }, 450);
    return () => window.clearTimeout(timer);
  }, [hasJoinWaitlist, isAuthenticated, userUid]);

  if (!isAuthenticated || !user) return null;

  const handleSignOut = async () => {
    try {
      await logOut();
    } finally {
      if (typeof window !== "undefined") {
        window.location.assign(resolvedMarketingBaseUrl);
      }
    }
  };

  return (
    <div className="user-menu" ref={menuRef}>
      <button
        className="user-menu-button"
        type="button"
        onClick={() => {
          setOpen((prev) => {
            const next = !prev;
            if (!next) setShowGetUpdatesPrompt(false);
            return next;
          });
        }}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {user.photoURL ? (
          <img className="user-menu-avatar" src={user.photoURL} alt={displayName} />
        ) : (
          <span className="user-menu-avatar user-menu-initials">{initials}</span>
        )}
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="user-menu-dropdown" role="menu">
          <div className="user-menu-profile">
            <div className="user-menu-name-row">
              <div className="user-menu-name">{displayName}</div>
              <span className="user-menu-plan-badge">{planBadge}</span>
            </div>
            {email && <div className="user-menu-email">{email}</div>}
          </div>
          {onBilling && stripeCustomerId && (
            <button
              className="user-menu-item"
              type="button"
              onClick={() => {
                onBilling();
                setOpen(false);
              }}
            >
              <CreditCard size={16} />
              Billing
            </button>
          )}
          {onJoinWaitlist && (
            <div className="user-menu-guided-item">
              <button
                className={`user-menu-item${showGetUpdatesPrompt ? " user-menu-item-highlighted" : ""}`}
                type="button"
                onClick={() => {
                  onJoinWaitlist();
                  setShowGetUpdatesPrompt(false);
                  setOpen(false);
                }}
              >
                <Bell size={16} />
                Get Updates
              </button>
              {showGetUpdatesPrompt && (
                <div className="user-menu-update-bubble" role="status" aria-live="polite">
                  Click here to subscribe to SightSinger updates.
                </div>
              )}
            </div>
          )}
          <button className="user-menu-item" type="button" onClick={handleSignOut}>
            <LogOut size={16} />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
