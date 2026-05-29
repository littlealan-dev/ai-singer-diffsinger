/// <reference types="vite/client" />

interface ImportMetaEnv {
    readonly VITE_FIREBASE_API_KEY: string
    readonly VITE_FIREBASE_AUTH_DOMAIN: string
    readonly VITE_FIREBASE_PROJECT_ID: string
    readonly VITE_FIREBASE_STORAGE_BUCKET: string
    readonly VITE_FIREBASE_MESSAGING_SENDER_ID: string
    readonly VITE_FIREBASE_APP_ID: string
    readonly VITE_FIREBASE_APP_CHECK_KEY: string
    readonly VITE_APP_ENV: string
    readonly VITE_APP_VERSION?: string
    readonly VITE_APP_BUILD_NUMBER?: string
    readonly VITE_GET_UPDATES_PROMPT_INTERVAL_DAYS?: string
    readonly VITE_BACKEND_READY_TIMEOUT_SECONDS?: string
    // more env variables...
}

interface ImportMeta {
    readonly env: ImportMetaEnv
}
