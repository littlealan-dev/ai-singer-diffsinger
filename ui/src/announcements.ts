/**
 * Announcement configuration for the announcement/news modal system.
 */
export interface Announcement {
  id: string;
  title: string;
  content: string; // Markdown supported
  effect?: 'fireworks' | 'none';
  requiresPending?: boolean;
  date: string;
  effectiveFrom: string; // ISO date string, e.g. "2026-04-07"
  effectiveTo?: string;  // ISO date string, optional. If blank, no end date.
}

export const ANNOUNCEMENTS: Announcement[] = [
  {
    id: 'version_1_0_1',
    title: 'SightSinger 1.0.1 Update',
    content: '- Added dynamic model switching: simple songs now use Gemini 3.1 Flash Lite for quicker responses, while complex part splitting uses a more advanced thinking model for more careful score splitting.\n\n- Shorter and more musician-friendly AI responses.\n\n**Bug Fixes**\n\n- Fixed missing dots on dotted notes in derived score previews.\n- Fixed several edge cases in part splitting.',
    effect: 'none',
    date: '2026-05-26',
    effectiveFrom: '2026-05-26',
  },
  {
    id: 'trial_reset_v1',
    title: 'Trial Reset & Extension!',
    content: 'We\'ve updated our free trial policy! Your account has been reset to **20 credits** (10 minutes of audio) and your expiry has been extended to **30 days**. Enjoy the new Singer!',
    effect: 'fireworks',
    requiresPending: true,
    date: '2026-04-07',
    effectiveFrom: '2026-04-07',
  }
];

export const LATEST_ANNOUNCEMENT_ID = ANNOUNCEMENTS[0].id;
