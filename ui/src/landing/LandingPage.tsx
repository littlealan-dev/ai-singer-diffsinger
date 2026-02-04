import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AuthModal } from "../components/AuthModal";
import { useAuth } from "../hooks/useAuth.tsx";
import "./LandingPage.css";

export default function LandingPage() {
    const navigate = useNavigate();
    const { isAuthenticated, authReady } = useAuth();
    const [showAuthModal, setShowAuthModal] = useState(false);
    const hasAuthLinkParams =
        typeof window !== "undefined" &&
        (() => {
            const params = new URLSearchParams(window.location.search);
            return (
                params.has("oobCode") ||
                params.has("mode") ||
                params.has("apiKey") ||
                params.has("finishSignIn")
            );
        })();

    useEffect(() => {
        if (!authReady) return;
        if (isAuthenticated) {
            navigate("/app", { replace: true });
            setShowAuthModal(false);
        } else if (!hasAuthLinkParams) {
            setShowAuthModal(true);
        } else {
            setShowAuthModal(false);
        }
    }, [authReady, isAuthenticated, navigate, hasAuthLinkParams]);

    return (
        <div className="landing-page landing-auth-only">
            <div className="landing-auth-card">
                <h1>Sign in required</h1>
                <p>Please sign in to continue to the app.</p>
                <button className="btn-primary" onClick={() => setShowAuthModal(true)}>
                    Sign in
                </button>
            </div>
            <AuthModal
                isOpen={showAuthModal}
                onClose={() => setShowAuthModal(false)}
                onSuccess={() => navigate("/app")}
            />
        </div>
    );
}
