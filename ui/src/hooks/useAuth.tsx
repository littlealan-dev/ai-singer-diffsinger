import { useEffect, useState, createContext, useContext, ReactNode } from "react";
import type { User } from "firebase/auth";
import {
    onAuthChange,
    completeGoogleRedirect,
    completeMagicLinkSignIn,
    isMagicLinkSignIn,
    getStoredEmailForSignIn,
} from "../firebase";

export interface AuthState {
    user: User | null;
    loading: boolean;
    authReady: boolean;
    error: string | null;
    isAuthenticated: boolean;
    /** True if user signed in for the first time (new account) */
    isNewUser: boolean;
}

const AuthContext = createContext<AuthState | undefined>(undefined);

/**
 * React hook for managing Firebase Auth state.
 * Handles both Google redirect completion and email magic link completion.
 */
/**
 * AuthProvider component that provides a single shared auth state to the whole app.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
    const [user, setUser] = useState<User | null>(null);
    const [loading, setLoading] = useState(true);
    const [authReady, setAuthReady] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [isNewUser, setIsNewUser] = useState(false);

    useEffect(() => {
        let mounted = true;
        let redirectResolved = false;
        let authResolved = false;
        let redirectUser: User | null = null;

        console.log("[useAuth] Initializing authentication hooks (singleton)...");

        const checkResolution = () => {
            console.log(`[useAuth] Checking resolution: redirectResolved=${redirectResolved}, authResolved=${authResolved}`);
            if (mounted && redirectResolved && authResolved) {
                console.log("[useAuth] All resolved. Setting loading to false.");
                setLoading(false);
                setAuthReady(true);
            }
        };

        // Handle redirect results (Google OAuth or magic link)
        const handleRedirectResults = async () => {
            try {
                // Check for magic link sign-in
                if (isMagicLinkSignIn()) {
                    console.log("[useAuth] Detected Magic Link sign-in link.");
                    const magicLinkUser = await completeMagicLinkSignIn();
                    if (magicLinkUser && mounted) {
                        console.log("[useAuth] Magic Link sign-in complete:", magicLinkUser.email);
                        setUser(magicLinkUser);
                        // Check if this is a new user (metadata.creationTime === metadata.lastSignInTime)
                        const creationTime = magicLinkUser.metadata.creationTime;
                        const lastSignIn = magicLinkUser.metadata.lastSignInTime;
                        if (creationTime && lastSignIn) {
                            const isNew = Math.abs(new Date(creationTime).getTime() - new Date(lastSignIn).getTime()) < 5000;
                            setIsNewUser(isNew);
                        }
                    }
                } else {
                    // Check for Google redirect result
                    console.log("[useAuth] Checking for Google redirect result...");
                    const googleUser = await completeGoogleRedirect();
                    if (googleUser && mounted) {
                        console.log("[useAuth] Google redirect sign-in complete:", googleUser.email);
                        redirectUser = googleUser;
                        setUser(googleUser);
                        const creationTime = googleUser.metadata.creationTime;
                        const lastSignIn = googleUser.metadata.lastSignInTime;
                        if (creationTime && lastSignIn) {
                            const isNew = Math.abs(new Date(creationTime).getTime() - new Date(lastSignIn).getTime()) < 5000;
                            setIsNewUser(isNew);
                        }
                    } else if (mounted) {
                        console.log("[useAuth] No Google redirect result found.");
                    }
                }
            } catch (err) {
                if (mounted) {
                    setError(err instanceof Error ? err.message : "Sign-in failed");
                }
            } finally {
                redirectResolved = true;
                checkResolution();
            }
        };

        handleRedirectResults();

        // Subscribe to auth state changes
        const unsubscribe = onAuthChange((authUser) => {
            if (mounted) {
                console.log("[useAuth] Auth state changed:", authUser ? authUser.email : "null");
                if (authUser) {
                    setUser(authUser);
                } else if (redirectUser) {
                    // Preserve redirect user if auth state is temporarily null.
                    setUser(redirectUser);
                } else {
                    setUser(null);
                }
                authResolved = true;
                checkResolution();
            }
        });

        return () => {
            mounted = false;
            unsubscribe();
        };
    }, []);

    const value = {
        user,
        loading,
        authReady,
        error,
        isAuthenticated: !!user && !user.isAnonymous,
        isNewUser,
    };

    return <AuthContext.Provider value={ value }> { children } </AuthContext.Provider>;
}

/**
 * Hook to consume the AuthContext.
 */
export function useAuth(): AuthState {
    const context = useContext(AuthContext);
    if (context === undefined) {
        throw new Error("useAuth must be used within an AuthProvider");
    }
    return context;
}

/**
 * Check if email is stored for magic link completion.
 */
export function useStoredEmail(): string | null {
    const [email, setEmail] = useState<string | null>(null);

    useEffect(() => {
        setEmail(getStoredEmailForSignIn());
    }, []);

    return email;
}
