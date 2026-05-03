import { useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { Check, Loader2, X } from "lucide-react";
import {
  startBillingPortal,
  startCheckout,
} from "../../billing/api";
import {
  getDisplayPlans,
  INCLUDED_IN_EVERY_PLAN_FEATURES,
  isCurrentPlanCard,
  isPaidPlanKey,
  type BillingInterval,
  type BillingPlanKey,
  type DisplayPlan,
} from "../../billing/plans";
import type { BillingState } from "../../hooks/useBillingState";
import "./BillingPaywallModal.css";

export type PaywallTrigger =
  | "credits_exhausted"
  | "overdrafted"
  | "trial_migrated"
  | "insufficient_credits"
  | "upload_blocked"
  | "chat_blocked"
  | "selection_blocked"
  | "drag_blocked"
  | "credits_pill"
  | "billing_menu"
  | "checkout_sync";

type BillingPaywallModalProps = {
  isOpen: boolean;
  trigger: PaywallTrigger;
  billing: BillingState;
  detail?: string | null;
  onClose: () => void;
};

const paidStatuses = new Set(["active", "trialing", "past_due"]);

export function BillingPaywallModal({
  isOpen,
  trigger,
  billing,
  detail,
  onClose,
}: BillingPaywallModalProps) {
  const [interval, setInterval] = useState<BillingInterval>("annual");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const plans = useMemo(() => getDisplayPlans(interval), [interval]);
  const activePaid = billing.activePlanKey !== "free" && paidStatuses.has(billing.stripeSubscriptionStatus || "active");
  const copy = getTriggerCopy(trigger, billing);
  const hardBlock = isHardBlockTrigger(trigger);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
  }, [isOpen, trigger]);

  if (!isOpen) return null;

  const handleCheckout = async (planKey: BillingPlanKey) => {
    if (!isPaidPlanKey(planKey)) return;
    setBusyAction(planKey);
    setError(null);
    try {
      const url = await startCheckout(planKey);
      window.location.assign(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start Checkout.");
      setBusyAction(null);
    }
  };

  const handlePortal = async () => {
    setBusyAction("portal");
    setError(null);
    try {
      const url = await startBillingPortal();
      window.location.assign(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open billing.");
      setBusyAction(null);
    }
  };

  const handleBackdropClose = () => {
    if (!hardBlock) onClose();
  };

  return (
    <div className="billing-modal-overlay" role="presentation" onClick={handleBackdropClose}>
      <section
        className="billing-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="billing-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <button className="billing-modal-close" type="button" onClick={onClose} aria-label="Close">
          <X size={18} />
        </button>
        <header className="billing-modal-header">
          <h2 id="billing-modal-title">{copy.title}</h2>
          <p>{copy.subtitle}</p>
          {detail ? <p className="billing-modal-detail">{detail}</p> : null}
          {hasBillingPaymentIssue(billing) ? (
            <div className="billing-status-warning" role="alert">
              Payment issue. Manage Billing to avoid service interruption.
            </div>
          ) : null}
          {billing.cancelAtPeriodEnd ? (
            <div className="billing-status-note">Cancels at period end.</div>
          ) : null}
          {billing.error ? (
            <div className="billing-status-warning" role="alert">
              {billing.error} Pricing is still visible, but checkout is paused until billing loads.
            </div>
          ) : null}
          {error ? (
            <div className="billing-status-warning" role="alert">
              {error}
            </div>
          ) : null}
          {trigger === "checkout_sync" && (
            <div className="billing-sync">
              {billing.activePlanKey === "free" ? (
                <>
                  <Loader2 size={16} className="billing-spinner" />
                  <span>Completing your upgrade...</span>
                </>
              ) : (
                <span>Your {billing.activePlanKey.replace("_", " ")} plan is active.</span>
              )}
            </div>
          )}
        </header>

        <div className="billing-interval-toggle" role="group" aria-label="Billing interval">
          <button
            type="button"
            aria-pressed={interval === "annual"}
            className={clsx(interval === "annual" && "active")}
            onClick={() => setInterval("annual")}
          >
            Annual
          </button>
          <button
            type="button"
            aria-pressed={interval === "monthly"}
            className={clsx(interval === "monthly" && "active")}
            onClick={() => setInterval("monthly")}
          >
            Monthly
          </button>
        </div>

        <div className="billing-plan-grid">
          {plans.map((plan) => (
            <PlanCard
              key={plan.cardKey}
              plan={plan}
              billing={billing}
              activePaid={activePaid}
              busyAction={busyAction}
              checkoutDisabled={billing.loading || Boolean(billing.error)}
              onCheckout={handleCheckout}
              onPortal={handlePortal}
            />
          ))}
        </div>
        <div className="billing-shared-features" aria-label="Included in every plan">
          <h3>Included in every plan</h3>
          <ul>
            {INCLUDED_IN_EVERY_PLAN_FEATURES.map((feature) => (
              <li key={feature}>
                <Check size={14} aria-hidden="true" />
                <span>{feature}</span>
              </li>
            ))}
          </ul>
        </div>

        <footer className="billing-modal-footer">
          {billing.stripeCustomerId || activePaid ? (
            <button type="button" onClick={handlePortal} disabled={busyAction === "portal"}>
              {busyAction === "portal" ? "Opening Billing..." : "Manage Billing"}
            </button>
          ) : null}
          <a href="/legal/terms" target="_blank" rel="noopener noreferrer">
            Terms
          </a>
          <a href="/legal/privacy" target="_blank" rel="noopener noreferrer">
            Privacy
          </a>
        </footer>
      </section>
    </div>
  );
}

type PlanCardProps = {
  plan: DisplayPlan;
  billing: BillingState;
  activePaid: boolean;
  busyAction: string | null;
  checkoutDisabled: boolean;
  onCheckout: (planKey: BillingPlanKey) => void;
  onPortal: () => void;
};

function PlanCard({
  plan,
  billing,
  activePaid,
  busyAction,
  checkoutDisabled,
  onCheckout,
  onPortal,
}: PlanCardProps) {
  const current = isCurrentPlanCard(billing.activePlanKey, plan);
  const isPaidCard = plan.cardKey !== "free";
  const busy = busyAction === plan.planKey || (busyAction === "portal" && activePaid && isPaidCard && !current);

  return (
    <article className={clsx("billing-plan-card", plan.cardKey, current && "current")}>
      {plan.badge ? <div className={clsx("billing-plan-badge", plan.cardKey)}>{plan.badge}</div> : null}
      <div className="billing-plan-head">
        <h3>{plan.name}</h3>
        <p>{plan.subtitle}</p>
      </div>
      <div className="billing-plan-price">
        {plan.originalPriceLabel ? (
          <del className="billing-original-price">{plan.originalPriceLabel}</del>
        ) : null}
        <span className="billing-price-main">{plan.priceLabel}</span>
        <span className="billing-price-suffix">{plan.priceSuffix}</span>
        {plan.savingsLabel ? <span className="billing-savings">{plan.savingsLabel}</span> : null}
      </div>
      {plan.secondaryPrice ? (
        <p className="billing-secondary-price">
          {plan.originalSecondaryPrice ? <del>{plan.originalSecondaryPrice}</del> : null}
          <span>{plan.secondaryPrice}</span>
        </p>
      ) : null}
      <div className="billing-credit-line">
        <div>
          <strong>{plan.creditsAmountLabel}</strong>
          <span> reset every month</span>
        </div>
        <span>{plan.audioLabel}</span>
      </div>
      <ul className="billing-feature-list">
        {plan.features.map((feature) => (
          <li key={feature}>
            <Check size={15} aria-hidden="true" />
            <span>{feature}</span>
          </li>
        ))}
      </ul>
      <div className="billing-plan-action">
        {current ? (
          <span className="billing-current-plan">Current Plan</span>
        ) : activePaid && isPaidCard ? (
          <button type="button" className="billing-plan-button secondary" onClick={onPortal} disabled={busy}>
            {busy ? "Opening Billing..." : "Manage Billing"}
          </button>
        ) : isPaidCard ? (
          <button
            type="button"
            className="billing-plan-button"
            onClick={() => onCheckout(plan.planKey)}
            disabled={checkoutDisabled || busy}
          >
            {busy ? "Redirecting to Checkout..." : `Upgrade to ${plan.name}`}
          </button>
        ) : activePaid ? null : (
          <span className="billing-current-plan">Current Plan</span>
        )}
      </div>
    </article>
  );
}

function getTriggerCopy(trigger: PaywallTrigger, billing: BillingState): { title: string; subtitle: string } {
  switch (trigger) {
    case "credits_exhausted":
      return {
        title: "You're out of credits",
        subtitle: "Upgrade to keep generating. Credits refresh monthly.",
      };
    case "overdrafted":
      return {
        title: "Your account needs attention",
        subtitle: "Resolve billing or choose a plan before more audio can be generated.",
      };
    case "trial_migrated":
      return {
        title: "Your old trial has been upgraded to the permanent free plan",
        subtitle: "You now receive 8 credits every month. Upgrade any time for more monthly credits.",
      };
    case "insufficient_credits":
      return {
        title: "This take needs more credits",
        subtitle: "Upgrade to continue with more monthly generation time.",
      };
    case "upload_blocked":
    case "drag_blocked":
      return {
        title: "Upgrade to upload more scores",
        subtitle: "Choose a plan to prepare more scores and generate more singing.",
      };
    case "chat_blocked":
      return {
        title: "Upgrade to generate more singing",
        subtitle: "Choose a plan with more monthly credits.",
      };
    case "selection_blocked":
      return {
        title: "Upgrade to render this selected part",
        subtitle: "Choose a plan to continue this studio workflow.",
      };
    case "checkout_sync":
      return {
        title: "Completing your upgrade",
        subtitle: "We are waiting for Stripe to sync your plan and monthly credits.",
      };
    case "credits_pill":
      if (billing.activePlanKey !== "free") {
        return {
          title: "Your billing plan",
          subtitle: "Review your current plan or manage billing.",
        };
      }
      if (billing.availableCredits <= 2) {
        return {
          title: "Running low on credits",
          subtitle: "Upgrade anytime for more monthly credits.",
        };
      }
      return {
        title: "Compare SightSinger plans",
        subtitle: "A plan for every singer, creator, and choir leader.",
      };
    case "billing_menu":
    default:
      return {
        title: "Upgrade your studio",
        subtitle: "Get more monthly credits for demos and full commercial rights.",
      };
  }
}

function hasBillingPaymentIssue(billing: BillingState): boolean {
  if (["past_due", "unpaid", "paused"].includes(billing.stripeSubscriptionStatus || "")) {
    return true;
  }
  if (billing.latestPaymentFailureCode || billing.latestPaymentFailureMessage) {
    return true;
  }
  return billing.latestInvoiceStatus === "open" && billing.latestPaymentIntentStatus === "requires_payment_method";
}

function isHardBlockTrigger(trigger: PaywallTrigger): boolean {
  return (
    trigger === "credits_exhausted" ||
    trigger === "overdrafted" ||
    trigger === "insufficient_credits" ||
    trigger === "upload_blocked" ||
    trigger === "chat_blocked" ||
    trigger === "selection_blocked" ||
    trigger === "drag_blocked"
  );
}
