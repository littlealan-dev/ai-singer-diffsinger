import type { ReactNode } from "react";
import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
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
    const navigate = useNavigate();
    const searchParams = typeof window !== "undefined" ? new URLSearchParams(window.location.search) : null;
    const hasAuthLinkParams =
        typeof window !== "undefined" &&
        (() => {
            if (!searchParams) return false;
            return (
                searchParams.has("oobCode") ||
                searchParams.has("mode") ||
                searchParams.has("apiKey") ||
                searchParams.has("finishSignIn")
            );
        })();
    const hasStartParam = useRef(
        typeof window !== "undefined" && searchParams ? searchParams.has("start") : false
    ).current;
    const checkoutPlan = searchParams?.get("checkoutPlan") ?? null;
    const hasCheckoutPlan = Boolean(checkoutPlan);
    const currentRedirectPath =
        typeof window !== "undefined"
            ? `${window.location.pathname}${window.location.search}${window.location.hash}`
            : "/app";

    useEffect(() => {
        if (!loading && !isAuthenticated && !hasAuthLinkParams && !hasStartParam && !hasCheckoutPlan) {
            navigate("/", { replace: true });
        }
    }, [loading, isAuthenticated, navigate, hasAuthLinkParams, hasStartParam, hasCheckoutPlan]);

    // Show loading while checking auth state
    if (loading) {
        return (
            <div className="protected-route-loading">
                <Loader2 className="protected-route-spinner" size={32} />
                <p>Loading...</p>
            </div>
        );
    }

    if (!isAuthenticated) {
        if (hasStartParam || hasCheckoutPlan) {
            return (
                <>
                    <div className="protected-route-loading">
                        <Loader2 className="protected-route-spinner" size={32} />
                        <p>Starting sign-in...</p>
                    </div>
                    <AuthModal
                        isOpen={true}
                        onClose={() => { }}
                        onSuccess={() => navigate(hasCheckoutPlan ? `/app?checkoutPlan=${checkoutPlan}` : "/app")}
                        redirectPath={currentRedirectPath}
                    />
                </>
            );
        }
        return (
            <div className="protected-route-loading">
                <Loader2 className="protected-route-spinner" size={32} />
                <p>
                    {hasAuthLinkParams
                        ? "Completing sign-in..."
                        : error
                            ? "Redirecting..."
                            : "Redirecting..."}
                </p>
            </div>
        );
    }

    // User is authenticated, render children
    return <>{children}</>;
}
