import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const home = await fs.mkdtemp(path.join(os.tmpdir(), "rac-test-"));
process.env.RAC_HOME = home;

const { parseKeysText } = await import("../extension/lib/keys.js");
const { fullDomain, subdomainFrom } = await import("../extension/lib/duckdns.js");
const { parseSms } = await import("../extension/lib/phone.js");
const { classifyPhoneText } = await import("../cli/flow/phone.js");
const { buildPlan, statusModel } = await import("../cli/flow/plan.js");
const {
  cleanDomain,
  cleanSubdomain,
  fullDomain: flowFullDomain,
  randomSubdomain,
} = await import("../cli/flow/domain.js");
const { findChrome, launchContext } = await import("../cli/browser.js");
const { createLaneApi, installLaneApi } = await import("../cli/lane-api.js");
const { fillAccountStep } = await import("../cli/commands/signup.js");
const lane = await import("../extension/lib/lane.js");

test.after(async () => {
  await fs.rm(home, { recursive: true, force: true });
});

test("DuckDNS helpers normalise subdomains", () => {
  assert.equal(subdomainFrom("My-Tenant.duckdns.org"), "my-tenant");
  assert.equal(subdomainFrom("example.com"), null);
  assert.equal(fullDomain("My-Tenant"), "my-tenant.duckdns.org");
});

test("key parser accepts the shipped credential formats", () => {
  const parsed = parseKeysText(
    JSON.stringify({
      installed: {
        client_id: "123.apps.googleusercontent.com",
        client_secret: "GOCSPX-test",
      },
    })
  );
  assert.equal(parsed.keys.length, 1);
  assert.equal(parsed.keys[0].kind, "oauth");
  assert.equal(parsed.keys[0].needs_consent, true);

  const line = parseKeysText("example.duckdns.org cid secret refresh-token");
  assert.equal(line.keys[0].domain, "example.duckdns.org");
  assert.equal(line.keys[0].refresh_token, "refresh-token");
  assert.equal(line.keys[0].needs_consent, false);
});

test("phone gate parser separates the two directions", () => {
  const sms = parseSms("sms:+15551234567?body=RAC-1234");
  assert.equal(sms.number, "+15551234567");
  assert.equal(sms.body, "RAC-1234");

  assert.equal(
    classifyPhoneText("Enter a phone number to get a verification code"),
    "phone-entry"
  );
  assert.equal(classifyPhoneText("Enter the code we sent you"), "code-entry");
  assert.equal(classifyPhoneText("Scan the QR code with your phone"), "qr");
  assert.equal(
    classifyPhoneText("Send this text message to +15551234567 saying RAC-1234"),
    "send-sms"
  );
  assert.equal(classifyPhoneText("Solve this captcha"), "captcha");
  assert.equal(
    classifyPhoneText("This phone number cannot be used for verification"),
    "rejected"
  );
});

test("start plan follows the tenant -> duckdns -> setup order", () => {
  const empty = {
    duckdns: {},
    setup: { done: {}, auto: {} },
    keys: [],
    accounts: [],
  };
  assert.deepEqual(
    buildPlan(empty, { accounts: 1 }).map((step) => step.id),
    ["domain", "signup", "duckdns", "setup", "accounts"]
  );

  const tenant = {
    ...empty,
    duckdns: { subdomain: "rac-test" },
    setup: { done: { "tenant.signup": true }, auto: {} },
  };
  assert.deepEqual(
    buildPlan(tenant, { accounts: 1 }).map((step) => step.id),
    ["duckdns", "setup", "accounts"]
  );

  const ready = {
    ...tenant,
    duckdns: { subdomain: "rac-test", token: "token" },
    keys: [{ id: "key_1", refresh_token: "refresh", needs_consent: false }],
  };
  assert.deepEqual(
    buildPlan(ready, { accounts: 2 }).map((step) => step.id),
    ["accounts"]
  );
  assert.deepEqual(
    buildPlan(ready, { accounts: 2, tenantOnly: true }).map((step) => step.id),
    []
  );

  const model = statusModel(ready);
  assert.equal(model.domain, "rac-test.duckdns.org");
  assert.equal(model.rows.find((row) => row.id === "setup").done, true);
});

test("domain helpers normalise and generate safe names", () => {
  assert.equal(cleanSubdomain("My-Tenant.duckdns.org"), "my-tenant");
  assert.equal(cleanDomain("https://My-Tenant.duckdns.org/foo"), "my-tenant.duckdns.org");
  assert.equal(flowFullDomain("My-Tenant"), "my-tenant.duckdns.org");
  assert.match(randomSubdomain(), /^[a-z]+-[a-z]+-\d{3}$/);
});

test(
  "signup account step ignores the readonly username preview",
  { skip: !findChrome(), timeout: 60_000 },
  async () => {
    const context = await launchContext({ profile: "account-fill-test", headless: true });
    try {
      const page = context.pages()[0] || (await context.newPage());
      await page.setContent(`
        <input readonly class="vphrPc" type="text" autocomplete="username" value="">
        <input id="ucc-14" type="text" maxlength="64">
        <input id="ucc-15" type="password">
        <input id="ucc-16" type="password">
      `);
      const result = await fillAccountStep(page, "admin", "Sup3r-secret", { fast: true });
      assert.equal(result.username, "admin");
      assert.equal(await page.locator("#ucc-14").inputValue(), "admin");
      assert.equal(await page.locator("#ucc-15").inputValue(), "Sup3r-secret");
      assert.equal(await page.locator("#ucc-16").inputValue(), "Sup3r-secret");
      assert.equal(await page.locator("input.vphrPc").inputValue(), "");
    } finally {
      await context.close().catch(() => {});
    }
  }
);

test("lane adapter bridges tabs, scripts and cookies", { skip: !findChrome(), timeout: 120_000 }, async () => {
  const laneApi = await createLaneApi({
    headless: true,
    settings: {},
    onMessage: () => {},
  });
  try {
    installLaneApi(laneApi.api);
    const container = await lane.ensureContainer(0);
    assert.match(container.storeId, /^rac-lane-\d+$/);

    const tab = await laneApi.api.tabs.create({
      url: "data:text/html,<title>lane</title><h1>lane</h1>",
      cookieStoreId: container.storeId,
    });
    const scripted = await laneApi.api.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => document.querySelector("h1").innerText,
    });
    assert.equal(scripted[0].result, "lane");

    const sent = await laneApi.api.tabs.sendMessage(tab.id, {
      type: "google-ping",
    });
    assert.equal(sent.ok, true);

    const context = laneApi.contexts.get(container.storeId).context;
    await context.addCookies([
      {
        name: "rac-test",
        value: "1",
        domain: "example.com",
        path: "/",
        secure: false,
        httpOnly: false,
        sameSite: "Lax",
      },
    ]);
    assert.equal((await laneApi.api.cookies.getAll({ storeId: container.storeId })).length, 1);
    await laneApi.api.cookies.remove({
      storeId: container.storeId,
      url: "http://example.com/",
      name: "rac-test",
    });
    assert.equal((await laneApi.api.cookies.getAll({ storeId: container.storeId })).length, 0);
  } finally {
    await laneApi.close();
  }
});
