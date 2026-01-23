import { initializeApp } from "firebase/app";
import {
  initializeAppCheck,
  ReCaptchaV3Provider,
  getToken,
  type AppCheck,
} from "firebase/app-check";
import {
  getAuth,
  signInAnonymously,
  signInWithRedirect,
  getRedirectResult,
  GoogleAuthProvider,
  sendSignInLinkToEmail,
  isSignInWithEmailLink,
  signInWithEmailLink,
  onAuthStateChanged,
  signOut,
  connectAuthEmulator,
  type Auth,
  type User,
} from "firebase/auth";
import { getFirestore, connectFirestoreEmulator } from "firebase/firestore";

const isDev = import.meta.env.VITE_APP_ENV === "dev";
const fallbackAuthDomain = typeof window !== "undefined" ? window.location.host : "";
const authDomain = isDev
  ? fallbackAuthDomain
  : (import.meta.env.VITE_FIREBASE_AUTH_DOMAIN as string);
const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY as string,
  authDomain,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID as string,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET as string,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID as string,
  appId: import.meta.env.VITE_FIREBASE_APP_ID as string,
};

const requiredConfigKeys = Object.entries(firebaseConfig).filter(
  ([, value]) => !value
);
let app: any = null;
let auth: Auth | null = null;
let db: any = null;
let appCheckEnabled = false;

try {
  if (requiredConfigKeys.length === 0) {
    app = initializeApp(firebaseConfig);
    auth = getAuth(app);
    db = getFirestore(app);

    if (import.meta.env.VITE_APP_ENV === "dev") {
      const emulatorOrigin = typeof window !== "undefined" ? window.location.origin : "http://127.0.0.1:5173";
      connectAuthEmulator(auth, emulatorOrigin, { disableWarnings: true });
      connectFirestoreEmulator(db, "127.0.0.1", 8080);
      console.log("Connected to Auth and Firestore emulators");
    }
  }
} catch (error) {
  console.error("Firebase initialization failed", error);
}

const appCheckSiteKey = import.meta.env.VITE_FIREBASE_APP_CHECK_KEY as string | undefined;

let appCheckInstance: AppCheck | null = null;
if (app && appCheckSiteKey && !isDev) {
  try {
    appCheckInstance = initializeAppCheck(app, {
      provider: new ReCaptchaV3Provider(appCheckSiteKey),
      isTokenAutoRefreshEnabled: true,
    });
    appCheckEnabled = true;
    console.log("[firebase] App Check initialized");
  } catch (error) {
    console.error("App Check failed", error);
  }
}

export { auth, app, db };

export async function getAppCheckToken(): Promise<string | null> {
  if (!appCheckEnabled || !appCheckInstance) return null;
  const token = await getToken(appCheckInstance, false);
  return token.token || null;
}

export async function getIdToken(): Promise<string | null> {
  if (!auth) {
    return null;
  }
  const user: User | null = auth.currentUser;
  if (!user) {
    return null;
  }
  return user.getIdToken();
}

// ============================================================================
// Google Sign-In (redirect flow)
// ============================================================================

const googleProvider = new GoogleAuthProvider();

/**
 * Initiate Google sign-in via full-page redirect.
 * After sign-in, user returns to the app and completeGoogleRedirect() finalizes.
 */
export async function signInWithGoogleRedirect(): Promise<void> {
  if (!auth) {
    throw new Error("Firebase Auth not initialized");
  }
  if (typeof window !== "undefined") {
    window.localStorage.setItem("postSignInRedirect", "/app");
  }
  await signInWithRedirect(auth, googleProvider);
}

/**
 * Complete Google sign-in after redirect.
 * Call this on app load to check if user is returning from Google OAuth.
 * @returns User if sign-in completed, null if no redirect result
 */
export async function completeGoogleRedirect(): Promise<User | null> {
  if (!auth) {
    return null;
  }
  try {
    const result = await getRedirectResult(auth);
    console.log("[firebase] getRedirectResult returned:", result ? (result.user ? result.user.email : "user without email") : "null");
    if (result?.user && typeof window !== "undefined") {
      const redirectPath = window.localStorage.getItem("postSignInRedirect");
      if (redirectPath) {
        window.localStorage.removeItem("postSignInRedirect");
        window.location.replace(redirectPath);
      }
    }
    return result?.user ?? null;
  } catch (error) {
    console.error("Google redirect sign-in failed:", error);
    throw error;
  }
}

// ============================================================================
// Email Magic Link Sign-In
// ============================================================================

const EMAIL_STORAGE_KEY = "emailForSignIn";

/**
 * Send a magic link to the user's email for passwordless sign-in.
 * @param email User's email address
 */
export async function sendMagicLink(email: string): Promise<void> {
  if (!auth) {
    throw new Error("Firebase Auth not initialized");
  }
  const actionCodeSettings = {
    url: `${window.location.origin}/app?finishSignIn=true`,
    handleCodeInApp: true,
  };
  await sendSignInLinkToEmail(auth, email, actionCodeSettings);
  window.localStorage.setItem(EMAIL_STORAGE_KEY, email);
}

/**
 * Check if current URL is a magic link sign-in link.
 */
export function isMagicLinkSignIn(): boolean {
  if (!auth) {
    return false;
  }
  return isSignInWithEmailLink(auth, window.location.href);
}

/**
 * Complete magic link sign-in.
 * Call this when URL contains finishSignIn=true query param.
 * @param emailOverride Optional email if not found in localStorage
 * @returns User if sign-in completed, null if not a magic link
 */
export async function completeMagicLinkSignIn(emailOverride?: string): Promise<User | null> {
  if (!auth) {
    return null;
  }
  if (!isSignInWithEmailLink(auth, window.location.href)) {
    return null;
  }

  let email = emailOverride ?? window.localStorage.getItem(EMAIL_STORAGE_KEY);
  if (!email) {
    // If email is missing (e.g. cross-port or cross-browser), ask the user
    email = window.prompt("Please enter your email to complete sign-in:");
  }

  if (!email) {
    throw new Error("Email is required to complete sign-in.");
  }

  try {
    const result = await signInWithEmailLink(auth, email, window.location.href);
    window.localStorage.removeItem(EMAIL_STORAGE_KEY);
    // Clean up URL
    const url = new URL(window.location.href);
    url.searchParams.delete("finishSignIn");
    window.history.replaceState({}, "", url.toString());
    return result.user;
  } catch (error) {
    console.error("Magic link sign-in failed:", error);
    throw error;
  }
}

/**
 * Get stored email for magic link completion (if any).
 */
export function getStoredEmailForSignIn(): string | null {
  return window.localStorage.getItem(EMAIL_STORAGE_KEY);
}

// ============================================================================
// Auth State Management
// ============================================================================

/**
 * Subscribe to auth state changes.
 * @param callback Called with user on sign-in, null on sign-out
 * @returns Unsubscribe function
 */
export function onAuthChange(callback: (user: User | null) => void): () => void {
  if (!auth) {
    // Call immediately with null if auth not initialized
    callback(null);
    return () => { };
  }
  return onAuthStateChanged(auth, callback);
}

/**
 * Get current user synchronously (may be null if not yet loaded).
 */
export function getCurrentUser(): User | null {
  return auth?.currentUser ?? null;
}

/**
 * Sign out the current user.
 */
export async function logOut(): Promise<void> {
  if (!auth) {
    return;
  }
  await signOut(auth);
}
