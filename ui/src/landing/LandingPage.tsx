import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { AuthModal } from "../components/AuthModal";
import { useAuth } from "../hooks/useAuth.tsx";
import "./LandingPage.css";

export default function LandingPage() {
    const navigate = useNavigate();
    const { isAuthenticated } = useAuth();
    const [showAuthModal, setShowAuthModal] = useState(true);

    useEffect(() => {
        if (isAuthenticated) {
            navigate("/app", { replace: true });
        }
    }, [isAuthenticated, navigate]);

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
