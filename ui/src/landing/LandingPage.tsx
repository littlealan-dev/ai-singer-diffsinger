import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Check, Loader2, Sparkles } from "lucide-react";
import { AuthModal } from "../components/AuthModal";
import { useAuth } from "../hooks/useAuth.tsx";
import {
    getDisplayPlans,
    INCLUDED_IN_EVERY_PLAN_FEATURES,
    storePendingCheckoutPlan,
    type BillingPlanKey,
} from "../billing/plans";
import "./LandingPage.css";

export default function LandingPage() {
    const navigate = useNavigate();
    const { isAuthenticated, authReady } = useAuth();
    const [showAuthModal, setShowAuthModal] = useState(false);
    const [selectedPlanKey, setSelectedPlanKey] = useState<BillingPlanKey | null>(null);
    const [billingInterval, setBillingInterval] = useState<"annual" | "monthly">("annual");
    const plans = getDisplayPlans(billingInterval);
    const hasAuthLinkParams =
        typeof window !== "undefined" &&
        (() => {
            const params = new URLSearchParams(window.location.search);
            return (
                params.has("oobCode") ||
                params.has("mode") ||
                params.has("apiKey") ||
                params.has("finishSignIn")
            );
        })();

    useEffect(() => {
        document.body.classList.add("landing-active");
        return () => document.body.classList.remove("landing-active");
    }, []);

    useEffect(() => {
        if (!authReady) return;
        if (isAuthenticated) {
            navigate("/app", { replace: true });
            setShowAuthModal(false);
        } else {
            setShowAuthModal(false);
        }
    }, [authReady, isAuthenticated, navigate, hasAuthLinkParams]);

    const handlePlanClick = (planKey: BillingPlanKey) => {
        const nextPath = planKey === "free" ? "/app" : `/app?checkoutPlan=${planKey}`;
        if (planKey !== "free") {
            storePendingCheckoutPlan(planKey);
        }
        setSelectedPlanKey(planKey);
        if (isAuthenticated) {
            navigate(nextPath);
            return;
        }
        setShowAuthModal(true);
    };

    if (!authReady) {
        return (
            <div className="landing-page landing-pricing-page">
                <div className="landing-auth-loading">
                    <Loader2 className="landing-auth-spinner" size={32} />
                    <p>Signing you in...</p>
                </div>
            </div>
        );
    }

    return (
        <div className="landing-page landing-pricing-page">
            <header className="landing-pricing-header">
                <div className="landing-pricing-brand">
                    <Sparkles size={24} />
                    <span>SightSinger.app</span>
                </div>
                <button className="btn-nav-secondary" onClick={() => setShowAuthModal(true)}>
                    Sign in
                </button>
            </header>
            <main className="landing-pricing-main">
                <section className="landing-pricing-copy">
                    <h1>Start creating singing tracks for free</h1>
                    <p>A plan for every singer, creator, and choir leader.</p>
                    <div className="landing-pricing-toggle" role="group" aria-label="Billing interval">
                        <button
                            type="button"
                            className={billingInterval === "annual" ? "active" : ""}
                            aria-pressed={billingInterval === "annual"}
                            onClick={() => setBillingInterval("annual")}
                        >
                            Annual
                        </button>
                        <button
                            type="button"
                            className={billingInterval === "monthly" ? "active" : ""}
                            aria-pressed={billingInterval === "monthly"}
                            onClick={() => setBillingInterval("monthly")}
                        >
                            Monthly
                        </button>
                    </div>
                </section>
                <section className="landing-pricing-grid" aria-label="SightSinger pricing plans">
                    {plans.map((plan) => (
                        <article key={plan.cardKey} className={`landing-pricing-card ${plan.cardKey}`}>
                            {plan.badge ? (
                                <div className={`landing-plan-badge ${plan.cardKey}`}>{plan.badge}</div>
                            ) : null}
                            <div>
                                <h2>{plan.name}</h2>
                                <p>{plan.subtitle}</p>
                            </div>
                            <div className="landing-plan-price">
                                {plan.originalPriceLabel ? (
                                    <del className="landing-plan-original-price">{plan.originalPriceLabel}</del>
                                ) : null}
                                <span>{plan.priceLabel}</span>
                                <strong>{plan.priceSuffix}</strong>
                                {plan.savingsLabel ? <em>{plan.savingsLabel}</em> : null}
                            </div>
                            {plan.secondaryPrice ? (
                                <p className="landing-plan-secondary">
                                    {plan.originalSecondaryPrice ? <del>{plan.originalSecondaryPrice}</del> : null}
                                    <span>{plan.secondaryPrice}</span>
                                </p>
                            ) : null}
                            <div className="landing-plan-credits">
                                <div>
                                    <strong>{plan.creditsAmountLabel}</strong>
                                    <span> reset every month</span>
                                </div>
                                <span>{plan.audioLabel}</span>
                            </div>
                            <ul>
                                {plan.features.map((feature) => (
                                    <li key={feature}>
                                        <Check size={15} />
                                        <span>{feature}</span>
                                    </li>
                                ))}
                            </ul>
                            <button
                                className="landing-plan-action"
                                onClick={() => handlePlanClick(plan.planKey)}
                            >
                                {plan.planKey === "free" ? "Start free" : `Upgrade to ${plan.name}`}
                            </button>
                        </article>
                    ))}
                </section>
                <section className="landing-shared-features" aria-label="Included in every plan">
                    <h2>Included in every plan</h2>
                    <ul>
                        {INCLUDED_IN_EVERY_PLAN_FEATURES.map((feature) => (
                            <li key={feature}>
                                <Check size={14} />
                                <span>{feature}</span>
                            </li>
                        ))}
                    </ul>
                </section>
            </main>
            <AuthModal
                isOpen={showAuthModal}
                onClose={() => setShowAuthModal(false)}
                onSuccess={() => navigate(selectedPlanKey && selectedPlanKey !== "free" ? `/app?checkoutPlan=${selectedPlanKey}` : "/app")}
                redirectPath={selectedPlanKey && selectedPlanKey !== "free" ? `/app?checkoutPlan=${selectedPlanKey}` : "/app"}
            />
        </div>
    );
}
