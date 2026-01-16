import type { ReactNode } from "react";
import { useState } from "react";
import { useAuth } from "../hooks/useAuth.tsx";
import { AuthModal } from "./AuthModal";
import { Loader2 } from "lucide-react";
import "./ProtectedRoute.css";

interface ProtectedRouteProps {
    children: ReactNode;
}

/**
 * Route wrapper that requires authentication.
 * Shows AuthModal if user is not authenticated.
 */
export function ProtectedRoute({ children }: ProtectedRouteProps) {
    const { user, loading, isAuthenticated, error } = useAuth();
    const [showAuthModal, setShowAuthModal] = useState(false);

    // Show loading while checking auth state
    if (loading) {
        return (
            <div className="protected-route-loading">
                <Loader2 className="protected-route-spinner" size={32} />
                <p>Loading...</p>
            </div>
        );
    }

    // If not authenticated, show prompt to sign in
    if (!isAuthenticated) {
        return (
            <div className="protected-route-unauthenticated">
                <div className="protected-route-card">
                    <h2>Sign in to continue</h2>
                    <p>You need to sign in to access the SightSinger.ai studio.</p>
                    {error && <p className="protected-route-error">{error}</p>}
                    <button
                        className="protected-route-signin-btn"
                        onClick={() => setShowAuthModal(true)}
                    >
                        Sign In / Start Free Trial
                    </button>
                </div>
                <AuthModal
                    isOpen={showAuthModal}
                    onClose={() => setShowAuthModal(false)}
                />
            </div>
        );
    }

    // User is authenticated, render children
    return <>{children}</>;
}
