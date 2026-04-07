/**
 * Announcement configuration for the announcement/news modal system.
 */
export interface Announcement {
  id: string;
  title: string;
  content: string; // Markdown supported
  effect?: 'fireworks' | 'none';
  date: string;
  effectiveFrom: string; // ISO date string, e.g. "2026-04-07"
  effectiveTo?: string;  // ISO date string, optional. If blank, no end date.
}

export const ANNOUNCEMENTS: Announcement[] = [
  {
    id: 'trial_reset_v1',
    title: 'Trial Reset & Extension!',
    content: 'We\'ve updated our free trial policy! Your account has been reset to **20 credits** (10 minutes of audio) and your expiry has been extended to **30 days**. Enjoy the new Singer!',
    effect: 'fireworks',
    date: '2026-04-07',
    effectiveFrom: '2026-04-07',
  }
];

export const LATEST_ANNOUNCEMENT_ID = ANNOUNCEMENTS[0].id;
