"use strict";

const assert = require("node:assert/strict");
const { chromium } = require("playwright");
const { resolve } = require("node:path");

(async () => {
  const [, , url, frames] = process.argv;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE_PATH });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    await page.route("**/*", (route) => {
      const target = new URL(route.request().url());
      return target.origin === new URL(url).origin ? route.continue() : route.abort();
    });
    await page.goto(url, { timeout: 15000 });
    await page.locator(".run-row").first().waitFor();
    await page.locator("#demo-banner").waitFor({ state: "visible" });
    await page.evaluate(() => document.fonts.ready);
    assert.equal(await page.locator(".run-row").count(), 48);
    assert.equal(await page.locator(".harness").count(), 8);
    assert.ok(await page.locator("#flux .col").count() >= 50);
    for (let frame = 0; frame < 100; frame++) {
      if (frame === 25) {
        await page.locator('[data-view="waiting"]').click();
        assert.equal(await page.locator(".run-row.needs").count(), 6);
      }
      if (frame === 33) await page.locator(".run-row.needs").first().click();
      if (frame === 48) await page.keyboard.press("Escape");
      if (frame === 52) await page.locator('[data-view="active"]').click();
      if (frame === 65) await page.locator("#harness-filter").selectOption({ label: "Codex · personal" });
      if (frame === 79) {
        await page.locator("#harness-filter").selectOption("");
        await page.evaluate(() => window.scrollTo(0, 0));
      }
      await page.waitForTimeout(65);
      await page.screenshot({ path: resolve(frames, `frame-${String(frame).padStart(3, "0")}.png`) });
    }
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
