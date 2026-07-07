type StripeEmbeddedCheckout = {
  mount: (selector: string | HTMLElement) => void;
  destroy: () => void;
};

type StripeInstance = {
  createEmbeddedCheckoutPage?: (options: {
    fetchClientSecret: () => Promise<string>;
    onComplete?: () => void;
  }) => Promise<StripeEmbeddedCheckout>;
  initEmbeddedCheckout?: (options: {
    clientSecret: string;
    onComplete?: () => void;
  }) => Promise<StripeEmbeddedCheckout>;
};

type StripeFactory = (publishableKey: string) => StripeInstance;

declare global {
  interface Window {
    Stripe?: StripeFactory;
  }
}

let stripeScriptPromise: Promise<void> | null = null;

export async function initEmbeddedStripeCheckout(
  publishableKey: string,
  clientSecret: string,
  onComplete: () => void
): Promise<StripeEmbeddedCheckout> {
  if (!publishableKey.trim()) {
    throw new Error("Stripe checkout is not configured.");
  }
  if (!clientSecret.trim()) {
    throw new Error("Stripe checkout session is not configured.");
  }
  await loadStripeScript();
  const stripeFactory = window.Stripe;
  if (!stripeFactory) {
    throw new Error("Stripe checkout could not be loaded.");
  }
  const stripe = stripeFactory(publishableKey);
  if (stripe.createEmbeddedCheckoutPage) {
    return stripe.createEmbeddedCheckoutPage({
      fetchClientSecret: async () => clientSecret,
      onComplete,
    });
  }
  if (!stripe.initEmbeddedCheckout) {
    throw new Error("Stripe embedded checkout could not be initialized.");
  }
  return stripe.initEmbeddedCheckout({
    clientSecret,
    onComplete,
  });
}

function loadStripeScript(): Promise<void> {
  if (typeof window === "undefined") {
    return Promise.reject(new Error("Stripe checkout is not available outside the browser."));
  }
  if (window.Stripe) {
    return Promise.resolve();
  }
  if (stripeScriptPromise) {
    return stripeScriptPromise;
  }
  stripeScriptPromise = new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>('script[src="https://js.stripe.com/v3/"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error("Stripe checkout could not be loaded.")), {
        once: true,
      });
      return;
    }
    const script = document.createElement("script");
    script.src = "https://js.stripe.com/v3/";
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Stripe checkout could not be loaded."));
    document.head.appendChild(script);
  });
  return stripeScriptPromise;
}
