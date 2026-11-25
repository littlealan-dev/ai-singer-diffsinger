import type { SingAllDecision, SingAllMixes, SingAllMode, SingAllStage, SingAllTakeMode, SongLicense } from "./api";
import type { BillingPlanKey } from "./billing/plans";
import { logAnalyticsEvent } from "./firebase";

export function countRange(n: number) {
  if (n <= 0) return "0";
  if (n === 1) return "1";
  if (n <= 4) return "2-4";
  if (n <= 8) return "5-8";
  if (n <= 16) return "9-16";
  return "17+";
}

export function creditsRange(n: number) {
  const credits = Math.ceil(n);
  if (credits <= 0) return "0";
  if (credits <= 5) return "1-5";
  if (credits <= 10) return "6-10";
  if (credits <= 20) return "11-20";
  if (credits <= 50) return "21-50";
  if (credits <= 100) return "51-100";
  return "101+";
}

export function durationRange(seconds: number) {
  if (seconds < 60) return "<1m";
  if (seconds < 300) return "1-5m";
  if (seconds < 900) return "5-15m";
  if (seconds < 3600) return "15-60m";
  return "60m+";
}

type QuoteCounts = {
  parts: number;
  splits: number;
  takes: number;
  mode: SingAllMode;
  mixes: SingAllMixes;
  credits: number;
};

function quoteParams({ parts, splits, takes, mode, mixes, credits }: QuoteCounts) {
  return {
    parts: countRange(parts),
    splits: countRange(splits),
    takes: countRange(takes),
    mode,
    mixes,
    credits_range: creditsRange(credits),
  };
}

export function singAllCardOpen(demoSong: boolean, planKey: BillingPlanKey) {
  logAnalyticsEvent("sing_all_card_open", { demo_song: demoSong, plan_key: planKey });
}

export function singAllQuoteShown(
  quote: QuoteCounts,
  openStaffs: number,
  repeats: boolean,
  affordable: boolean,
) {
  logAnalyticsEvent("sing_all_quote_shown", {
    ...quoteParams(quote),
    open_staffs: countRange(openStaffs),
    repeats,
    affordable,
  });
}

export type SingAllQuoteField =
  | "voice" | "mode" | "verse" | "repeats" | "mixes"
  | "language" | "include_part" | "style" | "staff_layout";

export function singAllQuoteChanged(field: SingAllQuoteField) {
  logAnalyticsEvent("sing_all_quote_changed", { field });
}

export function singAllStart(quote: QuoteCounts, voiceChanged: boolean) {
  logAnalyticsEvent("sing_all_start", { ...quoteParams(quote), voice_changed: voiceChanged });
}

export function singAllStartRefused(code?: string) {
  const reason = code === "sing_all_already_running" ? "song_busy" : code;
  logAnalyticsEvent("sing_all_start_refused", {
    code: reason === "quote_changed" || reason === "not_enough_credits" || reason === "song_busy" ? reason : "other",
  });
}

export function singAllUpgradeClick(shortfall: number, planKey: BillingPlanKey) {
  logAnalyticsEvent("sing_all_upgrade_click", { shortfall_range: creditsRange(shortfall), plan_key: planKey });
}

export function singAllTopupClick(shortfall: number, planKey: BillingPlanKey) {
  logAnalyticsEvent("sing_all_topup_click", { shortfall_range: creditsRange(shortfall), plan_key: planKey });
}

export function singAllStop(stage: Exclude<SingAllStage, "done">, elapsedSeconds: number) {
  logAnalyticsEvent("sing_all_stop", { stage, elapsed_range: durationRange(elapsedSeconds) });
}

export function singAllDecision(
  action: "continue" | "cancel",
  pausedParts: number,
  reviewsAccepted: number,
  reason: SingAllDecision["parts"][number]["reason"],
) {
  logAnalyticsEvent("sing_all_decision", {
    action,
    paused_parts: countRange(pausedParts),
    reviews_accepted: countRange(reviewsAccepted),
    reason,
  });
}

export function singAllRetryStart(takes: number, credits: number) {
  logAnalyticsEvent("sing_all_retry_start", { takes: countRange(takes), credits_range: creditsRange(credits) });
}

export function singAllZipDownload(from: "card" | "score_bar") {
  logAnalyticsEvent("sing_all_zip_download", { from });
}

export function singAllZipDownloadFailed(code?: string) {
  logAnalyticsEvent("sing_all_zip_download_failed", {
    code: code === "zip_unavailable" || code === "job_not_found" ? code : "other",
  });
}

export function singAllTabReturn(awaySeconds: number) {
  logAnalyticsEvent("sing_all_tab_return", { away_range: durationRange(awaySeconds) });
}

export function songInfoOpened(from: "title" | "share_dialog") {
  logAnalyticsEvent("song_info_opened", { from });
}

export function songInfoSaved(fieldsChanged: number, license: SongLicense, titleChanged: boolean, descriptionSet: boolean) {
  logAnalyticsEvent("song_info_saved", {
    fields_changed: fieldsChanged,
    license,
    title_changed: titleChanged,
    description_set: descriptionSet,
  });
}

export function shareDialogOpen(from: "song_bar" | "my_songs") {
  logAnalyticsEvent("share_dialog_open", { from });
}

export function shareLinkCreated(firstTime: boolean) {
  logAnalyticsEvent("share_link_created", { first_time: firstTime });
}

export function shareLinkCopied(from: "dialog" | "share_page") {
  logAnalyticsEvent("share_link_copied", { from });
}

export function shareTurnedOff(linkAgeSeconds?: number) {
  logAnalyticsEvent("share_turned_off", linkAgeSeconds === undefined ? {} : {
    link_age_range: durationRange(linkAgeSeconds),
  });
}

type SharePageStats = {
  signedIn: boolean;
  isOwner: boolean;
  layout: "laptop" | "portrait";
  parts: number;
  bothModes: boolean;
  hasInstruments: boolean;
  infoFields: number;
  license: SongLicense;
};

export function sharePageView(stats: SharePageStats) {
  logAnalyticsEvent("share_page_view", {
    signed_in: stats.signedIn,
    is_owner: stats.isOwner,
    layout: stats.layout,
    parts: countRange(stats.parts),
    both_modes: stats.bothModes,
    has_instruments: stats.hasInstruments,
    info_fields: stats.infoFields,
    license: stats.license,
  });
}

export function sharePageUnavailable(reason: "off" | "busy" | "error") {
  logAnalyticsEvent("share_page_unavailable", { reason });
}

export function sharePlay(mode: SingAllTakeMode, partsOn: number, instrumentsOn: boolean) {
  logAnalyticsEvent("share_play", { mode, parts_on: countRange(partsOn), instruments_on: instrumentsOn });
}

export function shareListenProgress(percent: 25 | 50 | 75 | 100) {
  logAnalyticsEvent("share_listen_progress", { percent });
}

export type ShareControl =
  | "mode_switch" | "part_toggle" | "zoom" | "view_toggle"
  | "seek" | "volume" | "parts_fold" | "info_fold";

export function shareControlUse(control: ShareControl) {
  logAnalyticsEvent("share_control_use", { control });
}

export function shareCtaClick(target: "sign_in" | "open_in_app" | "credits_page" | "license" | "brand") {
  logAnalyticsEvent("share_cta_click", { target });
}
