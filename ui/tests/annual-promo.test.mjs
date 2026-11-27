// The Pro and Max annual cards during a sale: the first year's price, then the price they renew at.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../../shared/billingPlans.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const { annualPromoFromEnv, getDisplayPlans } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

const promo = annualPromoFromEnv("25", "2026-12-01T00:00:00Z", "Black Friday");
const during = new Date("2026-11-28T12:00:00Z");
const options = { maxEarlySupporterEnabled: true, annualPromo: promo, now: during };
const card = (plans, key) => plans.find((plan) => plan.cardKey === key);

test("Pro and Max annual show the first year at 25% off, then their founding price", () => {
  const plans = getDisplayPlans("annual", options);
  assert.deepEqual(
    ["choir", "max"].map((key) => {
      const { badge, priceLabel, originalPriceLabel, secondaryPrice, savingsLabel } = card(plans, key);
      return { badge, priceLabel, originalPriceLabel, secondaryPrice, savingsLabel };
    }),
    [
      {
        badge: "Black Friday",
        priceLabel: "$12",
        originalPriceLabel: "$17",
        secondaryPrice: "$149.25 for the first year, then $199/yr",
        savingsLabel: "25% off",
      },
      {
        badge: "Black Friday",
        priceLabel: "$25",
        originalPriceLabel: "$33",
        secondaryPrice: "$299.25 for the first year, then $399/yr",
        savingsLabel: "25% off",
      },
    ]
  );
});

test("Solo and the monthly cards are not in the sale", () => {
  const annual = getDisplayPlans("annual", options);
  const plain = getDisplayPlans("annual", { maxEarlySupporterEnabled: true });
  assert.deepEqual(card(annual, "solo"), card(plain, "solo"));
  assert.deepEqual(
    getDisplayPlans("monthly", options),
    getDisplayPlans("monthly", { maxEarlySupporterEnabled: true })
  );
});

test("the cards go back to normal when the sale ends", () => {
  const after = getDisplayPlans("annual", { ...options, now: new Date("2026-12-01T00:00:00Z") });
  assert.deepEqual(after, getDisplayPlans("annual", { maxEarlySupporterEnabled: true }));
});

test("missing or invalid sale settings mean no sale", () => {
  assert.equal(annualPromoFromEnv(undefined, "2026-12-01T00:00:00Z", "Black Friday"), undefined);
  assert.equal(annualPromoFromEnv("25", undefined, "Black Friday"), undefined);
  assert.equal(annualPromoFromEnv("25", "not a date", "Black Friday"), undefined);
  assert.equal(annualPromoFromEnv("0", "2026-12-01T00:00:00Z", "Black Friday"), undefined);
});
