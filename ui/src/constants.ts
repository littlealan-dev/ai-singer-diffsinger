export const TRIAL_EXPIRY_DAYS = 30;

/**
 * A page of the marketing site, such as "/credits-and-ai-voices". In prod the app and the
 * marketing pages share one domain; locally the marketing site runs on its own server, so the
 * link needs VITE_MARKETING_BASE_URL. A bare path would open the app's server instead.
 */
export function marketingUrl(path: string): string {
  const base = ((import.meta.env.VITE_MARKETING_BASE_URL as string | undefined) ?? "").replace(/\/$/, "");
  return `${base}${path}`;
}
