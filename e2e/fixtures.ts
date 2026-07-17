import { test as base, expect } from "@playwright/test";

export const test = base.extend<{ networkLog: string[] }>({
  networkLog: async ({ page }, use, testInfo) => {
    const entries: string[] = [];
    page.on("request", request => {
      if (request.url().includes("/api/") || request.url().includes("/health")) {
        entries.push(`REQUEST ${request.method()} ${request.url()}`);
      }
    });
    page.on("response", response => {
      if (response.url().includes("/api/") || response.url().includes("/health")) {
        entries.push(`RESPONSE ${response.status()} ${response.url()}`);
      }
    });
    page.on("requestfailed", request => {
      entries.push(`FAILED ${request.method()} ${request.url()} ${request.failure()?.errorText ?? "unknown"}`);
    });
    page.on("console", message => entries.push(`CONSOLE ${message.type()} ${message.text()}`));
    await use(entries);
    await testInfo.attach("network-and-browser.log", {
      body: Buffer.from(entries.join("\n"), "utf-8"),
      contentType: "text/plain"
    });
  }
});

export { expect };
