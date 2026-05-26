const version = import.meta.env.VITE_APP_VERSION || "dev";
const buildNumber = import.meta.env.VITE_APP_BUILD_NUMBER || "local";

export const APP_VERSION = version;
export const APP_BUILD_NUMBER = buildNumber;
export const APP_VERSION_LABEL = `Version ${APP_VERSION}, build ${APP_BUILD_NUMBER}`;
