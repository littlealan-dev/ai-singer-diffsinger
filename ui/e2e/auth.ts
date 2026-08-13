import { expect, type Page } from "@playwright/test";

export async function signInAsE2EUser(page: Page, testId: string): Promise<void> {
  const uid = `e2e-${testId.replace(/[^a-z0-9-]/gi, "-")}`;
  const email = `${uid}@example.test`;
  await page.goto("/app");
  await page.evaluate(async ({ uid: identity, email: address }) => {
    const firebase = await import("/src/firebase.ts");
    await firebase.signInForE2E(identity, address);
    const token = await firebase.getIdToken();
    if (!token) throw new Error("E2E Firebase identity token is missing after sign-in.");
    const response = await fetch("http://127.0.0.1:8000/credits", {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!response.ok) throw new Error(`Unable to seed E2E credits: ${response.status}`);
  }, { uid, email });
  await page.reload();
  await expect(page.getByRole("heading", { name: "Studio Chat" })).toBeVisible();
}

export async function firebaseIdToken(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const firebase = await import("/src/firebase.ts");
    const token = await firebase.getIdToken();
    if (!token) throw new Error("E2E Firebase identity token is missing.");
    return token;
  });
}
