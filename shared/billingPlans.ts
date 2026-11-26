export type BillingInterval = "annual" | "monthly";

export type BillingPlanKey =
  | "free"
  | "solo_monthly"
  | "solo_annual"
  | "choir_early_monthly"
  | "choir_early_annual"
  | "choir_monthly"
  | "choir_annual"
  | "max_early_monthly"
  | "max_early_annual";

export type PlanFamily = "free" | "solo" | "choir" | "max";

export type PlanCardKey = "free" | "solo" | "choir" | "max";

type PaidPrice = {
  monthlyCents: number;
  annualCents: number;
  monthlyPlanKey: BillingPlanKey;
  annualPlanKey: BillingPlanKey;
  originalMonthlyCents?: number;
  originalAnnualCents?: number;
};

type PlanBase = {
  cardKey: PlanCardKey;
  family: PlanFamily;
  name: string;
  subtitle: string;
  monthlyCredits: number;
  audioMinutes: number;
  features: string[];
  badge?: string;
  price?: PaidPrice;
};

export type DisplayPlan = PlanBase & {
  planKey: BillingPlanKey;
  priceLabel: string;
  priceSuffix: string;
  originalPriceLabel?: string;
  secondaryPrice?: string;
  originalSecondaryPrice?: string;
  savingsLabel?: string;
  creditsAmountLabel: string;
  creditsLabel: string;
  audioLabel: string;
};

type DisplayPlanOptions = {
  choirEarlySupporterEnabled?: boolean;
  /** Max is sold only at its founding price: with the offer off, its card is not shown. */
  maxEarlySupporterEnabled?: boolean;
};

export const INCLUDED_IN_EVERY_PLAN_FEATURES = [
  "MusicXML upload",
  "Live score preview",
  "Intelligent SATB splitting",
  "Verse and part selection",
  "AI voice and style controls",
];

const BASE_PLANS: PlanBase[] = [
  {
    cardKey: "free",
    family: "free",
    name: "Free",
    subtitle: "Try it out",
    monthlyCredits: 8,
    audioMinutes: 4,
    features: ["About 1 full song per month", "Access to limited AI voices"],
  },
  {
    cardKey: "solo",
    family: "solo",
    name: "Solo",
    subtitle: "For independent singers and creators",
    monthlyCredits: 30,
    audioMinutes: 15,
    features: [
      "Roughly 3-4 full songs per month",
      "Full commercial rights",
      "Access to all AI voices",
      "Sing All: every vocal part sung in one go",
    ],
    badge: "Most Popular",
    price: {
      monthlyCents: 699,
      annualCents: 6900,
      monthlyPlanKey: "solo_monthly",
      annualPlanKey: "solo_annual",
    },
  },
  {
    cardKey: "choir",
    family: "choir",
    name: "Pro",
    subtitle: "For choirs and studios",
    monthlyCredits: 120,
    audioMinutes: 60,
    features: [
      "All features in Solo",
      "Enough for 3-4 full choir songs with all 4 SATB parts",
      "Or 1 demo pack: a 4-part song in lyrics and solfège, with full and part-dominant mixes",
    ],
    badge: "Founding Offer",
  },
  {
    cardKey: "max",
    family: "max",
    name: "Max",
    subtitle: "For busy choirs, teachers and studios",
    monthlyCredits: 400,
    audioMinutes: 200,
    features: [
      "All features in Solo",
      "Enough for 10+ full choir songs with all 4 SATB parts",
      "Or 5-6 demo packs: a 4-part song in lyrics and solfège, with full and part-dominant mixes",
    ],
    badge: "Founding Offer",
    price: {
      monthlyCents: 3999,
      annualCents: 39900,
      monthlyPlanKey: "max_early_monthly",
      annualPlanKey: "max_early_annual",
      originalMonthlyCents: 4999,
      originalAnnualCents: 49900,
    },
  },
];

const paidPlanKeys = new Set<BillingPlanKey>([
  "solo_monthly",
  "solo_annual",
  "choir_early_monthly",
  "choir_early_annual",
  "choir_monthly",
  "choir_annual",
  "max_early_monthly",
  "max_early_annual",
]);

export function getDisplayPlans(
  interval: BillingInterval = "annual",
  options: DisplayPlanOptions = {}
): DisplayPlan[] {
  const choirEarlySupporterEnabled = options.choirEarlySupporterEnabled ?? true;
  const maxEarlySupporterEnabled = options.maxEarlySupporterEnabled ?? true;
  const plans = BASE_PLANS.filter((plan) => plan.cardKey !== "max" || maxEarlySupporterEnabled);
  return plans.map((plan) => {
    const price = plan.cardKey === "choir" ? getChoirPrice(choirEarlySupporterEnabled) : plan.price;
    if (!price) {
      return {
        ...plan,
        planKey: "free",
        priceLabel: "$0",
        priceSuffix: "/mo",
        creditsAmountLabel: `${plan.monthlyCredits} credits`,
        creditsLabel: `${plan.monthlyCredits} credits reset every month`,
        audioLabel: `About ${plan.audioMinutes} minutes of vocal audio monthly`,
      };
    }

    const isAnnual = interval === "annual";
    const monthlyEquivalentCents = isAnnual ? Math.round(price.annualCents / 12) : price.monthlyCents;
    const originalMonthlyEquivalentCents =
      price.originalMonthlyCents && price.originalAnnualCents
        ? isAnnual
          ? Math.round(price.originalAnnualCents / 12)
          : price.originalMonthlyCents
        : undefined;
    const discountPercent = calculateDiscountPercent(
      isAnnual ? price.originalAnnualCents : price.originalMonthlyCents,
      isAnnual ? price.annualCents : price.monthlyCents
    );
    const annualSavings = Math.round(
      ((price.monthlyCents * 12 - price.annualCents) / (price.monthlyCents * 12)) * 100
    );

    return {
      ...plan,
      price,
      planKey: isAnnual ? price.annualPlanKey : price.monthlyPlanKey,
      priceLabel: formatPrice(monthlyEquivalentCents, { wholeDollars: isAnnual }),
      priceSuffix: "/mo",
      originalPriceLabel: originalMonthlyEquivalentCents
        ? formatPrice(originalMonthlyEquivalentCents, { wholeDollars: isAnnual })
        : undefined,
      originalSecondaryPrice:
        isAnnual && price.originalAnnualCents ? formatPrice(price.originalAnnualCents) : undefined,
      secondaryPrice: isAnnual ? `${formatPrice(price.annualCents)} billed yearly` : undefined,
      savingsLabel: discountPercent
        ? `${discountPercent}% off`
        : isAnnual
          ? `Save ${annualSavings}%`
          : undefined,
      creditsAmountLabel: `${plan.monthlyCredits} credits`,
      creditsLabel: `${plan.monthlyCredits} credits reset every month`,
      audioLabel: `About ${plan.audioMinutes} minutes of vocal audio monthly`,
    };
  });
}

export function isPaidPlanKey(planKey: string | null | undefined): planKey is BillingPlanKey {
  return Boolean(planKey && paidPlanKeys.has(planKey as BillingPlanKey));
}

export function isBillingPlanKey(planKey: string | null | undefined): planKey is BillingPlanKey {
  return planKey === "free" || isPaidPlanKey(planKey);
}

export function isCurrentPlanCard(activePlanKey: BillingPlanKey | null | undefined, plan: DisplayPlan): boolean {
  if (!activePlanKey) return false;
  if (plan.cardKey === "free") return activePlanKey === "free";
  if (plan.cardKey === "solo") return activePlanKey === "solo_monthly" || activePlanKey === "solo_annual";
  if (plan.cardKey === "max") return activePlanKey === "max_early_monthly" || activePlanKey === "max_early_annual";
  return (
    activePlanKey === "choir_early_monthly" ||
    activePlanKey === "choir_early_annual" ||
    activePlanKey === "choir_monthly" ||
    activePlanKey === "choir_annual"
  );
}

export function formatPlanName(planKey: BillingPlanKey | null | undefined): string {
  switch (planKey) {
    case "solo_monthly":
      return "Solo monthly";
    case "solo_annual":
      return "Solo annual";
    case "choir_early_monthly":
    case "choir_monthly":
      return "Choir monthly";
    case "choir_early_annual":
    case "choir_annual":
      return "Choir annual";
    case "max_early_monthly":
      return "Max monthly";
    case "max_early_annual":
      return "Max annual";
    default:
      return "Free";
  }
}

export function formatPlanBadge(planKey: BillingPlanKey | null | undefined): "Free" | "Solo" | "Pro" | "Max" {
  if (planKey === "solo_monthly" || planKey === "solo_annual") {
    return "Solo";
  }
  if (
    planKey === "choir_early_monthly" ||
    planKey === "choir_early_annual" ||
    planKey === "choir_monthly" ||
    planKey === "choir_annual"
  ) {
    return "Pro";
  }
  if (planKey === "max_early_monthly" || planKey === "max_early_annual") {
    return "Max";
  }
  return "Free";
}

function getChoirPrice(earlySupporterEnabled: boolean): PaidPrice {
  if (earlySupporterEnabled) {
    return {
      monthlyCents: 1999,
      annualCents: 19900,
      monthlyPlanKey: "choir_early_monthly",
      annualPlanKey: "choir_early_annual",
      originalMonthlyCents: 2499,
      originalAnnualCents: 24900,
    };
  }
  return {
    monthlyCents: 2499,
    annualCents: 24900,
    monthlyPlanKey: "choir_monthly",
    annualPlanKey: "choir_annual",
  };
}

function calculateDiscountPercent(originalCents: number | undefined, currentCents: number): number | undefined {
  if (!originalCents || originalCents <= currentCents) {
    return undefined;
  }
  return Math.round(((originalCents - currentCents) / originalCents) * 100);
}

function formatPrice(cents: number, options: { wholeDollars?: boolean } = {}): string {
  const amount = cents / 100;
  if (options.wholeDollars) {
    return `$${Math.round(amount)}`;
  }
  return `$${amount.toFixed(Number.isInteger(amount) ? 0 : 2)}`;
}
