import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, LogOut } from "lucide-react";
import { useAuth } from "../hooks/useAuth.tsx";
import { logOut } from "../firebase";

const getInitials = (name: string) => {
  const parts = name.trim().split(/\s+/);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0][0]?.toUpperCase() ?? "?";
  return `${parts[0][0] ?? ""}${parts[1][0] ?? ""}`.toUpperCase() || "?";
};

export function UserMenu() {
  const { user, isAuthenticated } = useAuth();
  const [open, setOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const displayName = user?.displayName?.trim() || "Signed in";
  const email = user?.email?.trim() || "";
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
          <button className="user-menu-item" type="button" onClick={() => logOut()}>
            <LogOut size={16} />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
