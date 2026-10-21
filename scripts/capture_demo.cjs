"use strict";

const { chromium } = require("playwright");
const { resolve } = require("node:path");
const { pathToFileURL } = require("node:url");

(async () => {
  const [, , url, frames] = process.argv;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE_PATH });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    await page.route("**/*", (route) => {
      const target = new URL(route.request().url());
      return target.origin === new URL(url).origin ? route.continue() : route.abort();
    });
    await page.goto(url);
    await page.locator(".run-row").first().waitFor();
    await page.locator("#demo-banner").waitFor({ state: "visible" });
    await page.evaluate(() => document.fonts.ready);
    for (let frame = 0; frame < 50; frame++) {
      if (frame === 10) await page.locator('[data-view="waiting"]').click();
      if (frame === 17) await page.locator(".run-row").filter({ hasText: "Docs worker" }).click();
      if (frame === 29) await page.keyboard.press("Escape");
      if (frame === 32) await page.locator('[data-view="active"]').click();
      if (frame === 37) await page.locator("#search").fill("fixture");
      if (frame === 44) await page.locator("#search").fill("");
      await page.waitForTimeout(100);
      await page.screenshot({ path: resolve(frames, `frame-${String(frame).padStart(3, "0")}.png`) });
    }
    await page.unroute("**/*");
    await page.setViewportSize({ width: 1280, height: 640 });
    await page.goto(pathToFileURL(resolve("docs/media/social-preview.html")).href);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: resolve("docs/media/social-preview.png") });
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
