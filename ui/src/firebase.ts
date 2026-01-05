import { initializeApp } from "firebase/app";
import {
  initializeAppCheck,
  ReCaptchaV3Provider,
  getToken,
  type AppCheck,
} from "firebase/app-check";

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY as string,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN as string,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID as string,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET as string,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID as string,
  appId: import.meta.env.VITE_FIREBASE_APP_ID as string,
};

const requiredConfigKeys = Object.entries(firebaseConfig).filter(
  ([, value]) => !value
);
let appCheckEnabled = false;

let app;
let appCheck: AppCheck | null = null;
try {
  if (requiredConfigKeys.length === 0) {
    app = initializeApp(firebaseConfig);
  } else {
    console.warn(
      "Firebase config is missing values; App Check disabled.",
      requiredConfigKeys.map(([key]) => key)
    );
  }
} catch (error) {
  console.error("Firebase initialization failed; App Check disabled.", error);
}

const appCheckSiteKey = import.meta.env.VITE_FIREBASE_APP_CHECK_KEY as
  | string
  | undefined;

if (app && appCheckSiteKey) {
  const debugToken = import.meta.env.VITE_FIREBASE_APP_CHECK_DEBUG_TOKEN as
    | string
    | undefined;
  if (debugToken) {
    (self as unknown as { FIREBASE_APPCHECK_DEBUG_TOKEN?: string }).FIREBASE_APPCHECK_DEBUG_TOKEN =
      debugToken === "true" ? true : debugToken;
  }
  try {
    appCheck = initializeAppCheck(app, {
      provider: new ReCaptchaV3Provider(appCheckSiteKey),
      isTokenAutoRefreshEnabled: true,
    });
    appCheckEnabled = true;
  } catch (error) {
    console.error("App Check initialization failed; continuing without it.", error);
  }
}

export async function getAppCheckToken(): Promise<string | null> {
  if (!appCheckEnabled || !appCheck) {
    return null;
  }
  const token = await getToken(appCheck, false);
  return token.token || null;
}
