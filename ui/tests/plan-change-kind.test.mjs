// What a paid user's plan card does, per what Stripe's portal did in test mode: a dearer plan per
// month, or the same plan moving to annual, applies now; a cheaper plan, or the same plan moving to
// monthly, waits for the renewal date.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("../../shared/billingPlans.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
}).outputText;
const { getPlanChangeKind, planChangeAppliesNow } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`
);

const cases = [
  ["choir_early_monthly", "choir_early_monthly", "current"],
  ["choir_monthly", "choir_early_monthly", "current"],
  ["choir_early_monthly", "choir_early_annual", "switch_to_annual"],
  ["choir_early_annual", "choir_early_monthly", "switch_to_monthly"],
  ["solo_monthly", "choir_early_monthly", "upgrade"],
  ["choir_early_monthly", "max_early_monthly", "upgrade"],
  ["solo_annual", "choir_early_monthly", "upgrade"],
  ["solo_monthly", "choir_early_annual", "upgrade"],
  // $16.58 a month on Pro annual against $39.99 on Max monthly.
  ["choir_early_annual", "max_early_monthly", "upgrade"],
  ["max_early_monthly", "choir_early_monthly", "downgrade"],
  // Booked for renewal in test mode, though the annual price is more per invoice.
  ["max_early_monthly", "choir_early_annual", "downgrade"],
  ["choir_early_annual", "solo_monthly", "downgrade"],
  ["free", "choir_early_monthly", null],
];

for (const [from, to, expected] of cases) {
  test(`${from} → ${to} is ${expected}`, () => {
    assert.equal(getPlanChangeKind(from, to), expected);
  });
}

test("changes that apply now", () => {
  const now = ["upgrade", "downgrade", "switch_to_annual", "switch_to_monthly"].filter(planChangeAppliesNow);
  assert.deepEqual(now, ["upgrade", "switch_to_annual"]);
});
