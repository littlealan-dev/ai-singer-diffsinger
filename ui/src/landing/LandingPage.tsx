import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Loader2, Send, FileUp } from "lucide-react";
import { AuthModal } from "../components/AuthModal";
import { useAuth } from "../hooks/useAuth";
import "./LandingPage.css";

export default function LandingPage() {
    const navigate = useNavigate();
    const { isAuthenticated, authReady } = useAuth();
    const [showAuthModal, setShowAuthModal] = useState(false);
    const [prompt, setPrompt] = useState("");

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
            setShowAuthModal(false);
        } else if (hasAuthLinkParams) {
            setShowAuthModal(false);
        }
    }, [authReady, isAuthenticated, hasAuthLinkParams]);

    const startFlow = () => {
        if (isAuthenticated) {
            navigate("/app");
        } else {
            setShowAuthModal(true);
        }
    };

    const handleSubmit = () => {
        if (!prompt.trim()) return;
        startFlow();
    };

    const handleSuggestion = (text: string) => {
        setPrompt(text);
        startFlow();
    };
    if (!authReady) {
        return (
            <div className="landing-loading">
                <div className="landing-loading-card">
                    <Loader2 className="landing-spinner" size={32} />
                    <p>Signing you in...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="landing-dark">
            <header className="landing-dark-nav">
                <div className="landing-dark-logo">
                    <img src="/logo-hackaton.png" alt="AI Singer Studio" />
                    <span>AI Singer Studio</span>
                </div>
                <button className="landing-dark-btn" onClick={startFlow}>
                    {isAuthenticated ? "Open Studio" : "Login"}
                </button>
            </header>

            <main className="landing-dark-main">
                <section className="landing-hero-card">
                    <div className="landing-logo-large">
                        <img src="/logo-hackaton.png" alt="AI Singer Studio" />
                    </div>
                    <h1>AI SINGER STUDIO</h1>
                    <p className="landing-hero-copy">Upload a score. Describe the vibe. We sing it back.</p>

                    <div className="landing-upload-row">
                        <button className="landing-upload-btn" onClick={startFlow}>
                            <FileUp size={18} />
                            Upload Score
                        </button>
                    </div>

                    <div className="landing-input-row">
                        <input
                            type="text"
                            placeholder="Ask the singer to interpret or render..."
                            value={prompt}
                            onChange={(e) => setPrompt(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
                        />
                        <button onClick={handleSubmit} disabled={!prompt.trim()}>
                            <Send size={18} />
                        </button>
                    </div>
                    <p className="landing-input-note">Sign in required • Your first render is on us</p>
                </section>
            </main>

            <AuthModal
                isOpen={showAuthModal}
                onClose={() => setShowAuthModal(false)}
                onSuccess={() => navigate("/app")}
            />
        </div>
    );
}
