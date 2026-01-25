import { useState, type FormEvent } from "react";
import { X, Mail, Loader2 } from "lucide-react";
import {
    signInWithGoogleRedirect,
    sendMagicLink,
} from "../firebase";
import "./AuthModal.css";
import { TRIAL_EXPIRY_DAYS } from "../constants";

export interface AuthModalProps {
    isOpen: boolean;
    onClose: () => void;
    onSuccess?: () => void;
}

type AuthModalState =
    | "idle"
    | "signingInGoogle"
    | "sendingEmail"
    | "emailSent"
    | "error";

/**
 * Modal for Google and Email magic link authentication.
 */
export function AuthModal({ isOpen, onClose, onSuccess }: AuthModalProps) {
    const [state, setState] = useState<AuthModalState>("idle");
    const [email, setEmail] = useState("");
    const [error, setError] = useState<string | null>(null);
    const [sentEmail, setSentEmail] = useState<string | null>(null);

    if (!isOpen) return null;

    const handleGoogleSignIn = async () => {
        setState("signingInGoogle");
        setError(null);
        try {
            await signInWithGoogleRedirect();
            // Page will redirect to Google, so we don't need to do anything else
        } catch (err) {
            setState("error");
            setError(err instanceof Error ? err.message : "Google sign-in failed");
        }
    };

    const handleEmailSubmit = async (e: FormEvent) => {
        e.preventDefault();
        if (!email.trim()) return;

        setState("sendingEmail");
        setError(null);
        try {
            await sendMagicLink(email.trim());
            setSentEmail(email.trim());
            setState("emailSent");
        } catch (err) {
            setState("error");
            setError(err instanceof Error ? err.message : "Failed to send magic link");
        }
    };

    const handleResend = () => {
        setState("idle");
        setSentEmail(null);
    };

    const handleTryDifferentEmail = () => {
        setState("idle");
        setEmail("");
        setSentEmail(null);
    };

    const handleClose = () => {
        // Reset state on close
        setState("idle");
        setEmail("");
        setError(null);
        setSentEmail(null);
        onClose();
    };

    // Email sent confirmation screen
    if (state === "emailSent" && sentEmail) {
        return (
            <div className="auth-modal-overlay" onClick={handleClose}>
                <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
                    <button className="auth-modal-close" onClick={handleClose} aria-label="Close">
                        <X size={20} />
                    </button>

                    <div className="auth-modal-content email-sent">
                        <div className="auth-modal-icon">📬</div>
                        <h2 className="auth-modal-title">Check Your Email</h2>
                        <p className="auth-modal-subtitle">We sent a sign-in link to:</p>
                        <p className="auth-modal-email">{sentEmail}</p>
                        <p className="auth-modal-instruction">
                            Click the link in your email to complete sign-in.
                        </p>

                        <div className="auth-modal-actions">
                            <button className="auth-btn-secondary" onClick={handleResend}>
                                Resend Link
                            </button>
                            <button className="auth-btn-secondary" onClick={handleTryDifferentEmail}>
                                Try Different Email
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        );
    }

    // Main auth modal
    return (
        <div className="auth-modal-overlay" onClick={handleClose}>
            <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
                <button className="auth-modal-close" onClick={handleClose} aria-label="Close">
                    <X size={20} />
                </button>

                <div className="auth-modal-content">
                    <h2 className="auth-modal-title">✨ Start Your Free Trial</h2>
                    <p className="auth-modal-subtitle">
                        Get 10 credits (5 minutes of audio)
                        <br />
                        Valid for {TRIAL_EXPIRY_DAYS} days
                    </p>
                    <p className="auth-modal-subtitle auth-modal-note">
                        No password required. No marketing spam.
                    </p>
                    {error && (
                        <div className="auth-modal-error" role="alert">
                            {error}
                        </div>
                    )}

                    <button
                        className="auth-btn-google"
                        onClick={handleGoogleSignIn}
                        disabled={state === "signingInGoogle" || state === "sendingEmail"}
                    >
                        {state === "signingInGoogle" ? (
                            <>
                                <Loader2 className="auth-btn-spinner" size={20} />
                                Redirecting to Google...
                            </>
                        ) : (
                            <>
                                <svg className="google-icon" viewBox="0 0 24 24" width="20" height="20">
                                    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
                                    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
                                    <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
                                    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
                                </svg>
                                Continue with Google
                            </>
                        )}
                    </button>

                    <div className="auth-modal-divider">
                        <span>or</span>
                    </div>

                    <form onSubmit={handleEmailSubmit} className="auth-email-form">
                        <input
                            type="email"
                            className="auth-input"
                            placeholder="Email address"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            disabled={state === "signingInGoogle" || state === "sendingEmail"}
                            required
                        />
                        <button
                            type="submit"
                            className="auth-btn-email"
                            disabled={state === "signingInGoogle" || state === "sendingEmail" || !email.trim()}
                        >
                            {state === "sendingEmail" ? (
                                <>
                                    <Loader2 className="auth-btn-spinner" size={18} />
                                    Sending...
                                </>
                            ) : (
                                <>
                                    <Mail size={18} />
                                    Send Magic Link
                                </>
                            )}
                        </button>
                    </form>

                    <p className="auth-modal-terms">
                        By continuing, you agree to our{" "}
                        <a href="/legal/terms" target="_blank" rel="noopener noreferrer">Terms of Service</a>
                        {" "}and{" "}
                        <a href="/legal/privacy" target="_blank" rel="noopener noreferrer">Privacy Policy</a>
                    </p>
                </div>
            </div>
        </div>
    );
}
