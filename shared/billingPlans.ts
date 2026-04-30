export type BillingInterval = "annual" | "monthly";

export type BillingPlanKey =
  | "free"
  | "starter_monthly"
  | "starter_annual"
  | "solo_monthly"
  | "solo_annual"
  | "choir_early_monthly"
  | "choir_early_annual"
  | "choir_monthly"
  | "choir_annual";

export type PlanFamily = "free" | "starter" | "solo" | "choir";

export type PlanCardKey = "free" | "starter" | "solo" | "choir";

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
  savingsLabel?: string;
  creditsAmountLabel: string;
  creditsLabel: string;
  audioLabel: string;
};

type DisplayPlanOptions = {
  choirEarlySupporterEnabled?: boolean;
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
    cardKey: "starter",
    family: "starter",
    name: "Starter",
    subtitle: "For casual singers",
    monthlyCredits: 15,
    audioMinutes: 7.5,
    features: ["About 2 full songs per month", "Full commercial rights", "Access to all AI voices"],
    price: {
      monthlyCents: 499,
      annualCents: 4900,
      monthlyPlanKey: "starter_monthly",
      annualPlanKey: "starter_annual",
    },
  },
  {
    cardKey: "solo",
    family: "solo",
    name: "Solo",
    subtitle: "For independent singers and creators",
    monthlyCredits: 30,
    audioMinutes: 15,
    features: ["Roughly 3-4 full songs per month", "Full commercial rights", "Access to all AI voices"],
    badge: "Most Popular",
    price: {
      monthlyCents: 999,
      annualCents: 9900,
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
      "Enough for 3-4 full choir songs with all 4 SATB parts",
      "Full commercial rights",
      "Access to all AI voices",
    ],
    badge: "Founding Offer",
  },
];

const paidPlanKeys = new Set<BillingPlanKey>([
  "starter_monthly",
  "starter_annual",
  "solo_monthly",
  "solo_annual",
  "choir_early_monthly",
  "choir_early_annual",
  "choir_monthly",
  "choir_annual",
]);

export function getDisplayPlans(
  interval: BillingInterval = "annual",
  options: DisplayPlanOptions = {}
): DisplayPlan[] {
  const choirEarlySupporterEnabled = options.choirEarlySupporterEnabled ?? true;
  return BASE_PLANS.map((plan) => {
    const price = plan.cardKey === "choir" ? getChoirPrice(choirEarlySupporterEnabled) : plan.price;
    if (!price) {
      return {
        ...plan,
        planKey: "free",
        priceLabel: "$0",
        priceSuffix: "/mo",
        creditsAmountLabel: `${plan.monthlyCredits} credits`,
        creditsLabel: `${plan.monthlyCredits} credits reset every month`,
        audioLabel: `About ${plan.audioMinutes} minutes of audio monthly`,
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
    const savings = Math.round(
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
      secondaryPrice: isAnnual ? `${formatPrice(price.annualCents)} billed yearly` : undefined,
      savingsLabel: isAnnual ? `Save ${savings}%` : undefined,
      creditsAmountLabel: `${plan.monthlyCredits} credits`,
      creditsLabel: `${plan.monthlyCredits} credits reset every month`,
      audioLabel: `About ${plan.audioMinutes} minutes of audio monthly`,
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
  if (plan.cardKey === "starter") {
    return activePlanKey === "starter_monthly" || activePlanKey === "starter_annual";
  }
  if (plan.cardKey === "solo") return activePlanKey === "solo_monthly" || activePlanKey === "solo_annual";
  return (
    activePlanKey === "choir_early_monthly" ||
    activePlanKey === "choir_early_annual" ||
    activePlanKey === "choir_monthly" ||
    activePlanKey === "choir_annual"
  );
}

export function formatPlanName(planKey: BillingPlanKey | null | undefined): string {
  switch (planKey) {
    case "starter_monthly":
      return "Starter monthly";
    case "starter_annual":
      return "Starter annual";
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
    default:
      return "Free";
  }
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

function formatPrice(cents: number, options: { wholeDollars?: boolean } = {}): string {
  const amount = cents / 100;
  if (options.wholeDollars) {
    return `$${Math.round(amount)}`;
  }
  return `$${amount.toFixed(Number.isInteger(amount) ? 0 : 2)}`;
}
