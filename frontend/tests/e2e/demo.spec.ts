import { expect, test } from "@playwright/test";

test("renders the frontend-only dashboard", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Telco revenue assurance" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "Revenue at risk" })).toBeVisible();
  await expect(page.getByText("$2.4M").first()).toBeVisible();
  await expect(page.getByText("18.4%")).toBeVisible();
  await expect(page.getByText("6.7%")).toBeVisible();
  await expect(page.getByRole("table")).toBeVisible();
  await expect(page.getByText("Southeast")).toBeVisible();

  await page.getByRole("button", { name: "Ask AI" }).click();
  const questionInput = page.getByRole("textbox", { name: "Question" });
  await questionInput.fill("What is driving revenue risk?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(/The AI backend is not connected yet/i)).toBeVisible();
});
