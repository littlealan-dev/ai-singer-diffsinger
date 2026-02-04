import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, LogOut, Users } from "lucide-react";
import { useAuth } from "../hooks/useAuth.tsx";
import { logOut } from "../firebase";

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

type UserMenuProps = {
  onJoinWaitlist?: () => void;
};

export function UserMenu({ onJoinWaitlist }: UserMenuProps) {
  const { user, isAuthenticated } = useAuth();
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const displayName = user?.displayName?.trim() || "Signed in";
  const email = user?.email?.trim() || "";
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
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onClick);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

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
        onClick={() => setOpen((prev) => !prev)}
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
            <div className="user-menu-name">{displayName}</div>
            {email && <div className="user-menu-email">{email}</div>}
          </div>
          {onJoinWaitlist && (
            <button
              className="user-menu-item user-menu-item-mobile-only"
              type="button"
              onClick={() => {
                onJoinWaitlist();
                setOpen(false);
              }}
            >
              <Users size={16} />
              Join Waiting List
            </button>
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
