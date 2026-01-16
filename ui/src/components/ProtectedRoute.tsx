import type { ReactNode } from "react";
import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth.tsx";
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

    useEffect(() => {
        if (!loading && !isAuthenticated) {
            navigate("/", { replace: true });
        }
    }, [loading, isAuthenticated, navigate]);

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
        return (
            <div className="protected-route-loading">
                <Loader2 className="protected-route-spinner" size={32} />
                <p>{error ? "Redirecting..." : "Redirecting..."}</p>
            </div>
        );
    }

    // User is authenticated, render children
    return <>{children}</>;
}
