import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";

const home = await fs.mkdtemp(path.join(os.tmpdir(), "rac-test-"));
process.env.RAC_HOME = home;

const { parseKeysText } = await import("../extension/lib/keys.js");
const { fullDomain, subdomainFrom } = await import("../extension/lib/duckdns.js");
const { findChrome } = await import("../cli/browser.js");
const { createLaneApi, installLaneApi } = await import("../cli/lane-api.js");
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
