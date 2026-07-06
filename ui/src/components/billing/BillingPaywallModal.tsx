import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import clsx from "clsx";
import { Check, Loader2, X } from "lucide-react";
import {
  cancelTopupCheckoutSession,
  startBillingPortal,
  startEmbeddedPlanCheckout,
  startEmbeddedTopupCheckout,
  syncCheckoutSession,
  syncTopupCheckoutSession,
} from "../../billing/api";
import type { EmbeddedCheckoutResponse } from "../../api";
import { initEmbeddedStripeCheckout } from "../../billing/embeddedStripe";
import {
  getDisplayPlans,
  INCLUDED_IN_EVERY_PLAN_FEATURES,
  isCurrentPlanCard,
  isPaidPlanKey,
  type BillingInterval,
  type BillingPlanKey,
  type DisplayPlan,
} from "../../billing/plans";
import { logAnalyticsEvent } from "../../firebase";
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
  onConfirmed?: (message: string) => void;
};

const paidStatuses = new Set(["active", "trialing", "past_due"]);
const STRIPE_PUBLISHABLE_KEY = import.meta.env.VITE_STRIPE_PUBLISHABLE_KEY?.trim() || "";

type CheckoutViewState = "plans" | "creating_checkout" | "embedded_checkout" | "confirming" | "failed";

type ActiveEmbeddedCheckout = EmbeddedCheckoutResponse & {
  planKey?: BillingPlanKey;
  packKey?: "topup_15";
  previousAvailableCredits: number;
  previousTopupActivePackCount: number;
  previousActivePlanKey: BillingPlanKey;
};

export function BillingPaywallModal({
  isOpen,
  trigger,
  billing,
  detail,
  onClose,
  onConfirmed,
}: BillingPaywallModalProps) {
  const [interval, setInterval] = useState<BillingInterval>("annual");
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkoutView, setCheckoutView] = useState<CheckoutViewState>("plans");
  const [activeEmbeddedCheckout, setActiveEmbeddedCheckout] = useState<ActiveEmbeddedCheckout | null>(null);
  const embeddedCheckoutContainerRef = useRef<HTMLDivElement>(null);
  const activeEmbeddedCheckoutRef = useRef<ActiveEmbeddedCheckout | null>(null);
  const embeddedCheckoutRef = useRef<{ destroy: () => void } | null>(null);
  const plans = useMemo(() => getDisplayPlans(interval), [interval]);
  const activePaid = billing.activePlanKey !== "free" && paidStatuses.has(billing.stripeSubscriptionStatus || "active");
  const copy = getTriggerCopy(trigger, billing);
  const hardBlock = isHardBlockTrigger(trigger);

  const destroyEmbeddedCheckout = useCallback(() => {
    embeddedCheckoutRef.current?.destroy();
    embeddedCheckoutRef.current = null;
  }, []);

  const confirmCheckout = useCallback(
    (checkout: ActiveEmbeddedCheckout) => {
      destroyEmbeddedCheckout();
      setActiveEmbeddedCheckout(null);
      setCheckoutView("plans");
      setBusyAction(null);
      setError(null);
      const message =
        checkout.checkoutType === "topup"
          ? "Credits added. You can continue rendering."
          : "Plan updated. You can continue rendering.";
      logAnalyticsEvent("billing_checkout_confirmed", {
        checkout_type: checkout.checkoutType,
        plan_key: checkout.planKey,
        pack_key: checkout.packKey,
        ui_mode: "embedded",
      });
      onConfirmed?.(message);
      onClose();
    },
    [destroyEmbeddedCheckout, onClose, onConfirmed]
  );

  const retrySync = useCallback(async (checkout: ActiveEmbeddedCheckout, surfaceError = true) => {
    try {
      if (checkout.checkoutType === "topup") {
        await syncTopupCheckoutSession(checkout.checkoutSessionId);
      } else {
        await syncCheckoutSession(checkout.checkoutSessionId);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not confirm checkout yet.";
      if (surfaceError && activeEmbeddedCheckoutRef.current?.checkoutSessionId === checkout.checkoutSessionId) {
        setError(message);
      }
    }
  }, []);

  const cancelPendingTopupCheckout = useCallback(async (checkout: ActiveEmbeddedCheckout | null) => {
    if (!checkout || checkout.checkoutType !== "topup") return;
    try {
      await cancelTopupCheckoutSession(checkout.checkoutSessionId);
      logAnalyticsEvent("billing_checkout_cancelled", {
        checkout_type: "topup",
        pack_key: checkout.packKey,
        ui_mode: "embedded",
      });
    } catch (err) {
      logAnalyticsEvent("billing_checkout_cancel_failed", {
        checkout_type: "topup",
        pack_key: checkout.packKey,
        ui_mode: "embedded",
        reason: err instanceof Error ? err.message : "unknown",
      });
    }
  }, []);

  const handleEmbeddedCheckoutComplete = useCallback(
    (checkout: ActiveEmbeddedCheckout) => {
      setCheckoutView("confirming");
      setError(null);
      logAnalyticsEvent("billing_checkout_complete_client", {
        checkout_type: checkout.checkoutType,
        plan_key: checkout.planKey,
        pack_key: checkout.packKey,
        ui_mode: "embedded",
      });
      void retrySync(checkout, false);
    },
    [retrySync]
  );

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    setCheckoutView("plans");
    setActiveEmbeddedCheckout(null);
    setBusyAction(null);
    destroyEmbeddedCheckout();
  }, [destroyEmbeddedCheckout, isOpen, trigger]);

  useEffect(() => {
    if (!isOpen) return;
    logAnalyticsEvent("billing_paywall_view", {
      trigger,
      layout_variant: "inline_topup",
    });
  }, [isOpen, trigger]);

  useEffect(() => {
    activeEmbeddedCheckoutRef.current = activeEmbeddedCheckout;
  }, [activeEmbeddedCheckout]);

  useEffect(() => {
    return () => {
      destroyEmbeddedCheckout();
    };
  }, [destroyEmbeddedCheckout]);

  useEffect(() => {
    if (checkoutView !== "embedded_checkout" || !activeEmbeddedCheckout) return;
    const container = embeddedCheckoutContainerRef.current;
    if (!container) return;
    let cancelled = false;
    destroyEmbeddedCheckout();
    void initEmbeddedStripeCheckout(
      STRIPE_PUBLISHABLE_KEY,
      activeEmbeddedCheckout.clientSecret,
      () => handleEmbeddedCheckoutComplete(activeEmbeddedCheckout)
    )
      .then((checkout) => {
        if (cancelled) {
          checkout.destroy();
          return;
        }
        embeddedCheckoutRef.current = checkout;
        checkout.mount(container);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Could not load Stripe checkout.");
        setCheckoutView("failed");
        setBusyAction(null);
        void cancelPendingTopupCheckout(activeEmbeddedCheckout);
        logAnalyticsEvent("billing_checkout_failed", {
          checkout_type: activeEmbeddedCheckout.checkoutType,
          plan_key: activeEmbeddedCheckout.planKey,
          pack_key: activeEmbeddedCheckout.packKey,
          reason: "stripe_mount_failed",
          ui_mode: "embedded",
        });
      });
    return () => {
      cancelled = true;
      destroyEmbeddedCheckout();
    };
  }, [activeEmbeddedCheckout, cancelPendingTopupCheckout, checkoutView, destroyEmbeddedCheckout, handleEmbeddedCheckoutComplete]);

  useEffect(() => {
    if (checkoutView !== "confirming" || !activeEmbeddedCheckout) return;
    if (
      activeEmbeddedCheckout.checkoutType === "topup" &&
      (billing.availableCredits > activeEmbeddedCheckout.previousAvailableCredits ||
        billing.topupActivePackCount > activeEmbeddedCheckout.previousTopupActivePackCount)
    ) {
      confirmCheckout(activeEmbeddedCheckout);
      return;
    }
    if (
      activeEmbeddedCheckout.checkoutType === "subscription" &&
      (billing.activePlanKey === activeEmbeddedCheckout.planKey ||
        (billing.activePlanKey !== activeEmbeddedCheckout.previousActivePlanKey &&
          paidStatuses.has(billing.stripeSubscriptionStatus || "")))
    ) {
      confirmCheckout(activeEmbeddedCheckout);
    }
  }, [activeEmbeddedCheckout, billing, checkoutView, confirmCheckout]);

  useEffect(() => {
    if (checkoutView !== "confirming" || !activeEmbeddedCheckout) return;
    const retryDelays = [1000, 2000, 4000, 8000, 15000];
    let cancelled = false;
    const runRetries = async () => {
      for (const delay of retryDelays) {
        await sleep(delay);
        if (cancelled || activeEmbeddedCheckoutRef.current?.checkoutSessionId !== activeEmbeddedCheckout.checkoutSessionId) {
          return;
        }
        await retrySync(activeEmbeddedCheckout, false);
      }
      if (!cancelled && activeEmbeddedCheckoutRef.current?.checkoutSessionId === activeEmbeddedCheckout.checkoutSessionId) {
        setError(
          activeEmbeddedCheckout.checkoutType === "topup"
            ? "Payment received. We are still confirming your credits. Keep this window open or try again in a moment."
            : "Payment received. We are still activating your plan. Keep this window open or try again in a moment."
        );
        logAnalyticsEvent("billing_checkout_timeout", {
          checkout_type: activeEmbeddedCheckout.checkoutType,
          plan_key: activeEmbeddedCheckout.planKey,
          pack_key: activeEmbeddedCheckout.packKey,
          ui_mode: "embedded",
        });
      }
    };
    void runRetries();
    return () => {
      cancelled = true;
    };
  }, [activeEmbeddedCheckout, checkoutView, retrySync]);

  if (!isOpen) return null;

  const handleCheckout = async (planKey: BillingPlanKey) => {
    if (!isPaidPlanKey(planKey)) return;
    setBusyAction(planKey);
    setError(null);
    setCheckoutView("creating_checkout");
    try {
      logAnalyticsEvent("billing_checkout_start", {
        checkout_type: "subscription",
        plan_key: planKey,
        trigger,
        ui_mode: "embedded",
      });
      const checkout = await startEmbeddedPlanCheckout(planKey);
      setActiveEmbeddedCheckout({
        ...checkout,
        planKey,
        previousAvailableCredits: billing.availableCredits,
        previousTopupActivePackCount: billing.topupActivePackCount,
        previousActivePlanKey: billing.activePlanKey,
      });
      setCheckoutView("embedded_checkout");
      logAnalyticsEvent("billing_checkout_embedded_mount", {
        checkout_type: "subscription",
        plan_key: planKey,
        trigger,
        ui_mode: "embedded",
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start Checkout.");
      setCheckoutView("failed");
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

  const handleTopupCheckout = async () => {
    setBusyAction("topup");
    setError(null);
    setCheckoutView("creating_checkout");
    try {
      logAnalyticsEvent("billing_checkout_start", {
        checkout_type: "topup",
        pack_key: "topup_15",
        trigger,
        ui_mode: "embedded",
      });
      const checkout = await startEmbeddedTopupCheckout();
      setActiveEmbeddedCheckout({
        ...checkout,
        packKey: "topup_15",
        previousAvailableCredits: billing.availableCredits,
        previousTopupActivePackCount: billing.topupActivePackCount,
        previousActivePlanKey: billing.activePlanKey,
      });
      setCheckoutView("embedded_checkout");
      logAnalyticsEvent("billing_checkout_embedded_mount", {
        checkout_type: "topup",
        pack_key: "topup_15",
        trigger,
        ui_mode: "embedded",
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start credit pack checkout.");
      setCheckoutView("failed");
      setBusyAction(null);
    }
  };

  const releasePendingTopupIfNeeded = () => {
    if (checkoutView === "confirming") return;
    void cancelPendingTopupCheckout(activeEmbeddedCheckoutRef.current);
  };

  const handleClose = () => {
    releasePendingTopupIfNeeded();
    onClose();
  };

  const handleBackdropClose = () => {
    if (!hardBlock) handleClose();
  };

  return (
    <div className="billing-modal-overlay" role="presentation" onClick={handleBackdropClose}>
      <section
        className={clsx(
          "billing-modal",
          (checkoutView === "creating_checkout" ||
            checkoutView === "embedded_checkout" ||
            checkoutView === "confirming") &&
            "checkout-expanded"
        )}
        role="dialog"
        aria-modal="true"
        aria-labelledby="billing-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <button className="billing-modal-close" type="button" onClick={handleClose} aria-label="Close">
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

        {checkoutView === "creating_checkout" || checkoutView === "embedded_checkout" || checkoutView === "confirming" ? (
          <EmbeddedCheckoutPanel
            view={checkoutView}
            checkout={activeEmbeddedCheckout}
            containerRef={embeddedCheckoutContainerRef}
            onRetry={() => {
              if (activeEmbeddedCheckout) void retrySync(activeEmbeddedCheckout, true);
            }}
            onBack={() => {
              releasePendingTopupIfNeeded();
              destroyEmbeddedCheckout();
              setCheckoutView("plans");
              setActiveEmbeddedCheckout(null);
              setBusyAction(null);
              setError(null);
            }}
          />
        ) : (
          <>
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

            <div className="billing-plan-grid with-topup">
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
              <TopupPlanCard
                billing={billing}
                busy={busyAction === "topup"}
                emphasized={trigger === "credits_exhausted" || trigger === "insufficient_credits"}
                disabled={billing.loading || Boolean(billing.error) || billing.topupActivePackCount >= 3}
                onCheckout={handleTopupCheckout}
              />
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
          </>
        )}
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
            {busy ? "Opening secure checkout..." : `Upgrade to ${plan.name}`}
          </button>
        ) : activePaid ? null : (
          <span className="billing-current-plan">Current Plan</span>
        )}
      </div>
    </article>
  );
}

type TopupCardProps = {
  billing: BillingState;
  busy: boolean;
  emphasized: boolean;
  disabled: boolean;
  onCheckout: () => void;
};

function TopupPlanCard({ billing, busy, emphasized, disabled, onCheckout }: TopupCardProps) {
  const remainingSlots = Math.max(0, 3 - billing.topupActivePackCount);
  return (
    <article className={clsx("billing-plan-card", "topup", "billing-topup-plan-card", emphasized && "emphasized")}>
      <div className="billing-plan-badge topup">One-off</div>
      <div className="billing-plan-head">
        <h3>Credit Pack</h3>
        <p>Add credits without a monthly commitment.</p>
      </div>
      <div className="billing-plan-price">
        <span className="billing-price-main">$5</span>
        <span className="billing-price-suffix">/ pack</span>
      </div>
      <p className="billing-secondary-price">
        <span>One-time purchase</span>
      </p>
      <div className="billing-credit-line">
        <div>
          <strong>15 credits</strong>
          <span> per pack</span>
        </div>
        <span>Expires in 180 days. Non-refundable.</span>
      </div>
      <ul className="billing-feature-list">
        <li>
          <Check size={15} aria-hidden="true" />
          <span>Use alongside your monthly plan</span>
        </li>
        <li>
          <Check size={15} aria-hidden="true" />
          <span>Good for one-off exports or extra renders</span>
        </li>
        <li>
          <Check size={15} aria-hidden="true" />
          <span>
            {remainingSlots > 0
              ? `You can buy up to ${remainingSlots} more active pack${remainingSlots === 1 ? "" : "s"}`
              : "Maximum 3 active packs reached"}
          </span>
        </li>
      </ul>
      <div className="billing-plan-action">
        <button
          type="button"
          className="billing-plan-button"
          onClick={onCheckout}
          disabled={disabled || busy}
          title={billing.topupActivePackCount >= 3 ? "Maximum 3 active packs" : undefined}
        >
          {busy ? "Opening Checkout..." : "Buy Credit Pack"}
        </button>
      </div>
    </article>
  );
}

type EmbeddedCheckoutPanelProps = {
  view: Exclude<CheckoutViewState, "plans" | "failed">;
  checkout: ActiveEmbeddedCheckout | null;
  containerRef: RefObject<HTMLDivElement>;
  onRetry: () => void;
  onBack: () => void;
};

function EmbeddedCheckoutPanel({
  view,
  checkout,
  containerRef,
  onRetry,
  onBack,
}: EmbeddedCheckoutPanelProps) {
  const isTopup = checkout?.checkoutType === "topup";
  if (view === "creating_checkout") {
    return (
      <section className="billing-embedded-panel" aria-live="polite">
        <Loader2 size={24} className="billing-spinner" />
        <h3>Opening secure checkout...</h3>
      </section>
    );
  }
  if (view === "confirming") {
    return (
      <section className="billing-embedded-panel" aria-live="polite">
        <Loader2 size={24} className="billing-spinner" />
        <h3>{isTopup ? "Confirming your credits..." : "Activating your plan..."}</h3>
        <p>Keep this window open while Stripe and SightSinger finish syncing.</p>
        <div className="billing-embedded-actions">
          <button type="button" onClick={onRetry}>
            Retry sync
          </button>
          <button type="button" className="secondary" onClick={onBack}>
            Back to plans
          </button>
        </div>
      </section>
    );
  }
  return (
    <section className="billing-embedded-panel embedded" aria-label="Secure Stripe checkout">
      <h3>Complete checkout</h3>
      <div className="billing-embedded-checkout" ref={containerRef} />
    </section>
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
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
