import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import clsx from "clsx";
import { Check, Loader2, X } from "lucide-react";
import {
  cancelTopupCheckoutSession,
  clearPendingPlanChange,
  getPendingPlanChange,
  startBillingPortal,
  startEmbeddedPlanCheckout,
  startEmbeddedTopupCheckout,
  startPlanChange,
  syncCheckoutSession,
  syncTopupCheckoutSession,
  undoScheduledPlanChange,
} from "../../billing/api";
import type { EmbeddedCheckoutResponse } from "../../api";
import { initEmbeddedStripeCheckout } from "../../billing/embeddedStripe";
import {
  formatPlanBadge,
  getDisplayPlans,
  getPlanChangeKind,
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
  | "checkout_sync"
  // Back from the Stripe portal's plan change page: a status panel until the change shows.
  | "plan_change_return"
  // The Sing All quote card's Top up and Upgrade: the same paywall as insufficient_credits and
  // billing_menu, named so the billing events say the purchase started from Sing All.
  | "sing_all_topup"
  | "sing_all_upgrade";

type BillingPaywallModalProps = {
  isOpen: boolean;
  trigger: PaywallTrigger;
  billing: BillingState;
  detail?: string | null;
  onClose: () => void;
  onConfirmed?: (message: string) => void;
};

const paidStatuses = new Set(["active", "trialing", "past_due"]);
// Stripe changes a plan only while the subscription is paid up (see the backend's change-plan).
const changeableStatuses = new Set(["active", "trialing"]);
const PLAN_CHANGE_RETURN_TIMEOUT_MS = 30000;
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
  // The paywall offers upgrades only: Free is left out, so the paid plans and the Credit Pack fit in a row.
  const plans = useMemo(() => getDisplayPlans(interval).filter((plan) => plan.cardKey !== "free"), [interval]);
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

  const handlePlanChange = async (planKey: BillingPlanKey) => {
    setBusyAction(planKey);
    setError(null);
    try {
      logAnalyticsEvent("billing_plan_change_start", {
        from_plan_key: billing.activePlanKey,
        plan_key: planKey,
        trigger,
      });
      const url = await startPlanChange(planKey);
      window.location.assign(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open the plan change.");
      setBusyAction(null);
    }
  };

  const handleUndoScheduledChange = async () => {
    setBusyAction("undo_scheduled_change");
    setError(null);
    try {
      await undoScheduledPlanChange();
      logAnalyticsEvent("billing_plan_change_undo", { plan_key: billing.scheduledChange?.planKey });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not undo the plan change.");
    } finally {
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
          {billing.scheduledChange ? (
            <div className="billing-status-note">
              <span>
                Changes to {planLabel(billing.scheduledChange.planKey)} on{" "}
                {formatDate(billing.scheduledChange.at)}.
              </span>
              <button
                type="button"
                onClick={handleUndoScheduledChange}
                disabled={busyAction === "undo_scheduled_change"}
              >
                {busyAction === "undo_scheduled_change"
                  ? "Undoing..."
                  : `Keep ${planLabel(billing.activePlanKey)}`}
              </button>
            </div>
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

        {trigger === "plan_change_return" ? (
          <PlanChangeReturnPanel billing={billing} onDone={onConfirmed ?? (() => onClose())} />
        ) : checkoutView === "creating_checkout" || checkoutView === "embedded_checkout" || checkoutView === "confirming" ? (
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

            <div className={clsx("billing-plan-grid", plans.length === 3 && "four-cards")}>
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
                  onChangePlan={handlePlanChange}
                />
              ))}
              <TopupPlanCard
                billing={billing}
                busy={busyAction === "topup"}
                emphasized={trigger === "credits_exhausted" || trigger === "insufficient_credits" || trigger === "sing_all_topup"}
                disabled={billing.loading || Boolean(billing.error) || billing.topupActivePackCount >= 3}
                onCheckout={handleTopupCheckout}
              />
            </div>
            <CreditRatesTable />
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
              {billing.scheduledChange ? (
                <span>To cancel instead, keep your plan first, then cancel in Manage Billing.</span>
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
  onChangePlan: (planKey: BillingPlanKey) => void;
};

function PlanCard({
  plan,
  billing,
  activePaid,
  busyAction,
  checkoutDisabled,
  onCheckout,
  onPortal,
  onChangePlan,
}: PlanCardProps) {
  // A paid user's cards are plan changes: "current" means the same plan and billing interval.
  const changeKind = activePaid ? getPlanChangeKind(billing.activePlanKey, plan.planKey) : null;
  const current = changeKind ? changeKind === "current" : isCurrentPlanCard(billing.activePlanKey, plan);
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
        ) : changeKind && changeKind !== "current" ? (
          <PlanChangeAction
            plan={plan}
            billing={billing}
            kind={changeKind}
            busy={busyAction === plan.planKey}
            disabled={checkoutDisabled || busyAction !== null}
            onChangePlan={onChangePlan}
          />
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

type PlanChangeActionProps = {
  plan: DisplayPlan;
  billing: BillingState;
  kind: Exclude<NonNullable<ReturnType<typeof getPlanChangeKind>>, "current">;
  busy: boolean;
  disabled: boolean;
  onChangePlan: (planKey: BillingPlanKey) => void;
};

function PlanChangeAction({ plan, billing, kind, busy, disabled, onChangePlan }: PlanChangeActionProps) {
  const label =
    kind === "upgrade"
      ? `Upgrade to ${plan.name}`
      : kind === "switch_interval"
        ? `Switch to ${plan.planKey.endsWith("_annual") ? "annual" : "monthly"}`
        : `Switch to ${plan.name}`;
  const renewal = billing.currentPeriodEnd ? formatDate(billing.currentPeriodEnd) : "your renewal date";
  const blockedNote = !changeableStatuses.has(billing.stripeSubscriptionStatus || "")
    ? "Fix your payment in Manage Billing to change plan"
    : billing.cancelAtPeriodEnd
      ? "Renew in Manage Billing to change plan"
      : billing.scheduledChange
        ? "Keep your plan above to choose another change"
        : null;
  const note = blockedNote ?? (kind === "upgrade" ? "Pay the difference today" : `From ${renewal}`);
  return (
    <div className="billing-plan-change">
      <button
        type="button"
        className="billing-plan-button"
        onClick={() => onChangePlan(plan.planKey)}
        disabled={disabled || busy || blockedNote !== null}
      >
        {busy ? "Opening Billing..." : label}
      </button>
      <span className="billing-plan-change-note">{note}</span>
    </div>
  );
}

type PlanChangeReturnPanelProps = {
  billing: BillingState;
  onDone: (message: string) => void;
};

/** After the Stripe portal: wait for the change to show on the billing doc, then close. */
function PlanChangeReturnPanel({ billing, onDone }: PlanChangeReturnPanelProps) {
  const [pending] = useState(getPendingPlanChange);
  const doneRef = useRef(false);

  const finish = useCallback(
    (message: string) => {
      if (doneRef.current) return;
      doneRef.current = true;
      clearPendingPlanChange();
      onDone(message);
    },
    [onDone]
  );

  useEffect(() => {
    if (!pending) {
      finish("Your billing is up to date.");
      return;
    }
    const name = planLabel(pending.planKey);
    const startedAt = pending.startedAt;
    if (billing.activePlanKey === pending.planKey && (billing.lastGrantAt?.getTime() ?? 0) >= startedAt) {
      finish(`You're on ${name}: ${billing.monthlyAllowance} credits.`);
    } else if (billing.scheduledChange?.planKey === pending.planKey) {
      finish(`Your plan changes to ${name} on ${formatDate(billing.scheduledChange.at)}.`);
    } else if ((billing.latestInvoicePaymentFailedAt?.getTime() ?? 0) >= startedAt) {
      finish("Your payment didn't go through, so your plan hasn't changed. Check your card in Manage Billing.");
    }
  }, [billing, finish, pending]);

  useEffect(() => {
    const timer = window.setTimeout(
      () => finish("Still confirming with Stripe. Your plan will update here shortly."),
      PLAN_CHANGE_RETURN_TIMEOUT_MS
    );
    return () => window.clearTimeout(timer);
  }, [finish]);

  return (
    <section className="billing-embedded-panel" aria-live="polite">
      <Loader2 size={24} className="billing-spinner" />
      <h3>Updating your plan...</h3>
      <p>Stripe is confirming your change. This usually takes a few seconds.</p>
    </section>
  );
}

/** "Pro monthly", "Max annual": the card name the user knows, with its billing interval. */
function planLabel(planKey: BillingPlanKey): string {
  return `${formatPlanBadge(planKey)} ${planKey.endsWith("_annual") ? "annual" : "monthly"}`;
}

function formatDate(date: Date): string {
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

type TopupCardProps = {
  billing: BillingState;
  busy: boolean;
  emphasized: boolean;
  disabled: boolean;
  onCheckout: () => void;
};

/** Credit usage per kind of generation, as on the marketing site's pricing page. */
function CreditRatesTable() {
  return (
    <div className="billing-credit-rates">
      <h3>How credits are used</h3>
      <table>
        <thead>
          <tr>
            <th scope="col">What you generate</th>
            <th scope="col">Credit usage</th>
            <th scope="col">When it&apos;s charged</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">Vocal audio</th>
            <td>1 credit per 30 seconds</td>
            <td>For each vocal part, every time you render it</td>
          </tr>
          <tr>
            <th scope="row">Instrumental accompaniment (MIDI)</th>
            <td>1 credit per 2 minutes</td>
            <td>Once per uploaded score, regardless of the number of instrument tracks</td>
          </tr>
          <tr>
            <th scope="row">Sing All mixes</th>
            <td>1 credit per mix file</td>
            <td>For each full mix and part-dominant mix you choose, whatever its length</td>
          </tr>
        </tbody>
      </table>
      <p>
        Vocal and instrumental time is rounded up, and its length follows your With repeats
        setting. The quote shows the exact credits before you confirm, and the final charge uses
        the actual audio length. Downloading the real-time mix is free.
      </p>
    </div>
  );
}

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
          <span>Good for extra renders</span>
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
    case "sing_all_topup":
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
    case "plan_change_return":
      return {
        title: "Changing your plan",
        subtitle: "Your plan and monthly credits update here once Stripe confirms.",
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
    case "sing_all_upgrade":
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
    trigger === "sing_all_topup" ||
    trigger === "upload_blocked" ||
    trigger === "chat_blocked" ||
    trigger === "selection_blocked" ||
    trigger === "drag_blocked"
  );
}
