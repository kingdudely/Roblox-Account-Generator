// language: JavaScript, file: content/setup.js
// Finishes a tenant in the Admin console and the Google Cloud console. It is
// deliberately one-way: the runner owns the phase and supplies the profile, and
// this script only fills/clicks the page in front of it. The Workspace signup is
// manual; this script starts once the user lands in the Admin console.
//
// It runs everywhere the setup tab can land, but does nothing until the runner
// sends `setup-fill`. That keeps it out of the lane tabs.

(() => {
  if (globalThis.__racSetup) return;
  globalThis.__racSetup = true;

  const api = globalThis.browser ?? globalThis.chrome;
  const DEBOUNCE_MS = 2600;

  const state = {
    profile: null,
    phase: "",
    timer: null,
    lastAction: "",
    lastAt: 0,
    lastUrl: "",
    reported: new Set(),
    captchaSent: false,
    domainToken: "",
    domainReady: false,
    billingSent: false,
    domainVerifiedSent: false,
    policySent: false,
    clientSent: false,
    projectId: "",
    projectSent: false,
    apiSent: false,
    consentSent: false,
    consentStarted: false,
    paused: false,
    lastHandoff: 0,
    adminSent: "",
    domainStatusAt: 0,
  };

  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };

  const bodyText = () => (document.body ? document.body.innerText || "" : "").toLowerCase();

  function send(message) {
    try {
      const sent = api.runtime.sendMessage(message);
      if (sent && typeof sent.catch === "function") sent.catch(() => {});
    } catch (_) {
      // extension context gone
    }
  }

  function note(text, level = "info") {
    send({ type: "setup-report", level, text });
  }

  function once(key, text, level = "info") {
    if (state.reported.has(key)) return;
    state.reported.add(key);
    note(text, level);
  }

  function fill(element, value) {
    if (!element || value === undefined || value === null) return false;
    try {
      element.focus();
      const proto =
        element instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(element, value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
      element.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "a" }));
      element.blur();
      return true;
    } catch (_) {
      return false;
    }
  }

  function labelOf(el) {
    return `${el.getAttribute("aria-label") || ""} ${el.value || ""} ${
      el.innerText || ""
    }`
      .replace(/\s+/g, " ")
      .trim();
  }

  const CONTROLS =
    'button,[role="button"],[role="link"],[role="menuitem"],[role="radio"],a,input[type="submit"],input[type="button"]';

  function clickByText(needles, scope = CONTROLS, exact = false) {
    const nodes = [...document.querySelectorAll(scope)].filter(visible);
    const lowered = needles.map((n) => n.toLowerCase());
    for (const node of nodes) {
      const label = labelOf(node).toLowerCase();
      if (!label) continue;
      const hit = exact
        ? lowered.includes(label)
        : lowered.some((needle) => label === needle || label.includes(needle));
      if (!hit) continue;
      try {
        node.click();
        return label.slice(0, 70);
      } catch (_) {
        // next candidate
      }
    }
    return null;
  }

  function clickExact(labels, scope = CONTROLS) {
    const wanted = labels.map((label) => label.toLowerCase());
    const nodes = [...document.querySelectorAll(scope)].filter(visible);
    for (const node of nodes) {
      const aria = String(node.getAttribute("aria-label") || "")
        .replace(/\s+/g, " ")
        .trim()
        .toLowerCase();
      const text = String(node.innerText || "")
        .replace(/\s+/g, " ")
        .trim()
        .toLowerCase();
      if (!wanted.some((want) => aria === want || text === want)) continue;
      try {
        node.click();
        return (text || aria).slice(0, 70);
      } catch (_) {
        // next candidate
      }
    }
    return null;
  }

  function act(key, run) {
    const now = Date.now();
    if (state.lastAction === key && now - state.lastAt < DEBOUNCE_MS) return true;
    state.lastAction = key;
    state.lastAt = now;
    return Boolean(run());
  }

  function textInputs() {
    return [...document.querySelectorAll('input[type="text"], input:not([type])')].filter(
      (input) => visible(input) && !input.readOnly && !input.disabled
    );
  }

  function fillIfEmpty(input, value) {
    if (!input || !value) return false;
    if (String(input.value || "").trim()) return false;
    return fill(input, value);
  }

  function maybeCaptcha(body) {
    const field = [...document.querySelectorAll('input[name="ca"], input[aria-label*="text you hear" i]')]
      .filter(visible)[0];
    if (!field) return false;
    if (String(field.value || "").trim()) return false;
    if (!state.captchaSent) {
      state.captchaSent = true;
      note("Google wants a captcha — solve it in the setup tab; the run continues after that", "warn");
      send({ type: "setup-captcha", url: location.href });
    }
    return true;
  }

  function maybeDomainToken() {
    const raw = document.body ? document.body.innerText || "" : "";
    const match = /google-site-verification=[A-Za-z0-9_-]+/i.exec(raw);
    if (!match) return false;
    const token = match[0];
    if (token !== state.domainToken) {
      state.domainToken = token;
      send({
        type: "setup-domain",
        token,
        domain: state.profile?.domain || "",
      });
      note(`domain TXT token found (${token.length} chars) — writing it through DuckDNS`);
    }
    return true;
  }

  // ------------------------------------------------------------------ accounts

  function accountsTick(body) {
    // The automatic branch never has the admin password. Do not touch the
    // identifier, account chooser or password fields: the operator signs in.
    // The one safe action is clearing a terms/speed-bump interstitial so the
    // worker tab can reach the Admin console.
    if (/speedbump|terms/i.test(location.href) || /i understand/i.test(body)) {
      act("speedbump", () =>
        clickByText(["i understand", "continue", "accept"], CONTROLS)
      );
    }
  }

  // ------------------------------------------------------------- account/domain

  function emailsOnPage() {
    const values = [];
    for (const el of document.querySelectorAll(
      "[aria-label],[title],[data-email],[data-identifier],[data-authuser]"
    )) {
      values.push(
        el.getAttribute("aria-label") || "",
        el.getAttribute("title") || "",
        el.getAttribute("data-email") || "",
        el.getAttribute("data-identifier") || "",
        el.getAttribute("data-authuser") || ""
      );
    }
    values.push(document.body ? document.body.innerText || "" : "");
    const emails = new Set();
    for (const value of values) {
      for (const match of String(value).matchAll(
        /[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}/gi
      )) {
        emails.add(match[0].toLowerCase());
      }
    }
    return [...emails];
  }

  function detectAdmin() {
    const domain = String(state.profile?.domain || "").toLowerCase();
    if (!domain) return;
    const expected = emailsOnPage().find((email) => email.endsWith(`@${domain}`));
    if (!expected || state.adminSent === expected) return;
    state.adminSent = expected;
    note(`admin account detected: ${expected}`, "ok");
    send({ type: "setup-admin", email: expected, domain, url: location.href });
  }

  function reportDomainStatus(body) {
    const domain = String(state.profile?.domain || "").toLowerCase();
    if (!domain) return false;
    const now = Date.now();
    if (state.domainStatusAt && now - state.domainStatusAt < 3500) return false;
    state.domainStatusAt = now;

    const found = body.includes(domain);
    const notVerified =
      /not verified|unverified|verification (?:is )?(?:pending|required)|verify (?:your )?domain/i.test(
        body
      );
    const verified =
      /domain is verified|domain verified|verification successful|your domain is verified/i.test(
        body
      ) ||
      (found && /\bverified\b/i.test(body) && !notVerified);
    send({
      type: "setup-domain-status",
      domain,
      found,
      verified,
      url: location.href,
    });
    if (verified && !state.domainVerifiedSent) {
      state.domainVerifiedSent = true;
      note("domain verified — moving on", "ok");
      send({ type: "setup-domain-verified", domain });
    }
    return true;
  }

  function adminTick(body) {
    detectAdmin();

    if (maybeDomainToken()) {
      if (state.domainReady) {
        act("admin-verify-domain", () =>
          clickByText(["verify domain", "verify", "continue"], CONTROLS)
        );
      }
      if (
        /domain is verified|domain verified|verification successful|your domain is verified/i.test(
          body
        )
      ) {
        if (!state.domainVerifiedSent) {
          state.domainVerifiedSent = true;
          note("domain verified — moving on", "ok");
          send({ type: "setup-domain-verified", domain: state.profile?.domain || "" });
        }
      }
      return;
    }

    if (/\/ac\/domains\/manage/i.test(location.pathname)) {
      reportDomainStatus(body);
      if (
        /domain is verified|domain verified|verification successful|your domain is verified/i.test(
          body
        )
      ) {
        return;
      }
      act("admin-domains-open", () =>
        clickByText(["verify domain", "verify", "continue"], CONTROLS)
      );
      return;
    }

    if (/\/ac\/billing\/catalog/i.test(location.pathname)) {
      const active =
        /cloud identity free/i.test(body) &&
        /(active|subscribed|manage|your subscription|you.re subscribed)/i.test(body);
      if (active) {
        if (!state.billingSent) {
          state.billingSent = true;
          note("Cloud Identity Free is active", "ok");
          send({ type: "setup-billing-done" });
        }
        return;
      }
      act("billing-open", () => {
        return (
          clickExact(["cloud identity"], CONTROLS) ||
          clickByText(
            ["cloud identity free", "get started", "start", "subscribe"],
            CONTROLS
          ) ||
          clickByText(["cloud identity"], CONTROLS)
        );
      });
      return;
    }

    if (/\/ac\/security\/2sv/i.test(location.pathname)) {
      if (
        /any except verification codes via text, phone call/i.test(body) &&
        /enforcement/i.test(body)
      ) {
        if (!state.policySent) {
          state.policySent = true;
          note("2SV policy looks set (enforcement + methods)", "ok");
          send({ type: "setup-policy-done" });
        }
        return;
      }
      act("policy-edit", () =>
        clickByText(["enforcement", "edit", "methods", "configure"], CONTROLS)
      );
      act("policy-on", () => clickExact(["on", "enforce", "enforced"], CONTROLS));
      act("policy-methods", () =>
        clickByText(
          ["any except verification codes via text, phone call", "any except", "authenticator"],
          CONTROLS
        )
      );
      act("policy-save", () => clickByText(["save", "done", "continue"], CONTROLS));
      return;
    }
  }

  // ------------------------------------------------------------------ console

  function extractProjectId() {
    const fromUrl = /[?&]project=([a-z0-9-]+)/i.exec(location.href);
    if (fromUrl) return fromUrl[1];
    for (const node of document.querySelectorAll("[data-project-id], [data-project]")) {
      const value =
        node.getAttribute("data-project-id") || node.getAttribute("data-project") || "";
      if (/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(value)) return value;
    }
    return "";
  }

  function consoleTick(body) {
    detectAdmin();
    const project = extractProjectId();
    if (project) {
      state.projectId = project;
      if (!state.projectSent) {
        state.projectSent = true;
        send({ type: "setup-project", project });
        note(`Cloud project ${project}`);
      }
    }

    if (/\/projectcreate/i.test(location.pathname)) {
      if (state.projectId) return;
      const input =
        document.querySelector('input[aria-label*="Project name" i]') ||
        document.querySelector('input#project-name') ||
        textInputs()[0];
      fillIfEmpty(input, state.profile.projectName);
      if (input?.value) {
        act("project-create", () => clickExact(["create", "create project"], CONTROLS));
      }
      return;
    }

    if (/\/apis\/library\/admin\.googleapis\.com/i.test(location.pathname)) {
      if (/api enabled|disable/i.test(body)) {
        if (!state.apiSent) {
          state.apiSent = true;
          note("Admin SDK enabled", "ok");
          send({ type: "setup-api-enabled" });
        }
        return;
      }
      act("enable-admin-sdk", () => clickExact(["enable"], CONTROLS));
      return;
    }

    if (/\/apis\/credentials\/consent/i.test(location.pathname)) {
      const appNameInput =
        document.querySelector('input[aria-label*="App name" i]') ||
        document.querySelector('input[aria-label*="Application name" i]');
      const scopesPage =
        /add or remove scopes/i.test(body) && !/get started/i.test(body);
      if (scopesPage && (!appNameInput || state.consentStarted)) {
        if (!state.consentSent) {
          state.consentSent = true;
          note("consent screen ready — add the five scopes on this page", "ok");
          send({ type: "setup-consent-ready" });
        }
        return;
      }
      if (/get started/i.test(body)) {
        act("consent-start", () => {
          state.consentStarted = true;
          return clickExact(["get started"], CONTROLS);
        });
        return;
      }
      fillIfEmpty(appNameInput, state.profile.businessName);
      const emailInput =
        document.querySelector('input[type="email"]') || textInputs().find((i) => i.type === "email");
      fillIfEmpty(emailInput, state.profile.recoveryEmail);
      act("consent-next", () => {
        const hit = clickExact(
          ["next", "save and continue", "create", "save"],
          CONTROLS
        );
        if (hit) state.consentStarted = true;
        return hit;
      });
      act("consent-internal", () => clickExact(["internal"], CONTROLS));
      return;
    }

    if (/\/apis\/credentials/i.test(location.pathname)) {
      if (/client secret|client id/i.test(body)) {
        const text = `${document.body?.innerText || ""}\n${[
          ...document.querySelectorAll("input,textarea"),
        ]
          .map((node) => node.value || "")
          .join("\n")}`;
        const idMatch = /\b\d{8,}-[a-z0-9_-]+\.apps\.googleusercontent\.com\b/i.exec(text);
        const secretMatch = /\bGOCSPX-[A-Za-z0-9_-]{10,}\b/.exec(text);
        if (idMatch && secretMatch && !state.clientSent) {
          state.clientSent = true;
          note("OAuth client credentials found");
          send({
            type: "setup-client",
            client_id: idMatch[0],
            client_secret: secretMatch[0],
            project: state.projectId,
          });
          return;
        }
      }
      act("credentials-open", () =>
        clickExact(["create credentials", "create credentials"], CONTROLS)
      );
      act("credentials-oauth", () =>
        clickByText(["oauth client id", "oauth client"], CONTROLS)
      );
      act("credentials-desktop", () => clickExact(["desktop app", "desktop"], CONTROLS));
      if (/desktop app|application type/i.test(body)) {
        const nameInput =
          document.querySelector('input[aria-label*="Name" i]') || textInputs()[0];
        fillIfEmpty(nameInput, `${state.profile.businessName} desktop`);
        act("credentials-create", () => clickExact(["create"], CONTROLS));
      }
      return;
    }
  }

  // --------------------------------------------------------------------- tick

  function tick() {
    if (!state.profile || state.paused) return;
    if (state.lastHandoff && Date.now() - state.lastHandoff > 30_000) {
      state.paused = true;
      note("runner stopped sending handoffs — automatic setup paused", "warn");
      return;
    }
    if (!document.body) return;

    const url = location.href;
    if (url !== state.lastUrl) {
      state.lastUrl = url;
      state.reported = new Set();
      state.captchaSent = false;
      state.domainStatusAt = 0;
      state.lastAction = "";
      note(`page ${url.slice(0, 140)}`);
    }

    const body = bodyText();
    const host = location.hostname;

    if (maybeCaptcha(body)) return;

    if (host.endsWith("accounts.google.com")) {
      accountsTick(body);
      return;
    }
    if (host === "admin.google.com") {
      adminTick(body);
      return;
    }
    if (host === "console.cloud.google.com") {
      consoleTick(body);
      return;
    }
  }

  function start() {
    tick();
    if (state.timer) return;
    state.timer = setInterval(tick, 900);
  }

  api.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message !== "object") return false;

    if (message.type === "setup-fill") {
      state.profile = message.profile || state.profile;
      const phase = message.phase || "";
      if (phase !== state.phase) {
        // A redo of one sub-step must see its one-shot signals again even when
        // the worker tab stayed on the same document.
        if (phase === "tenant.verify") {
          state.domainToken = "";
          state.domainStatusAt = 0;
        }
        if (phase === "tenant.cloud-identity") state.billingSent = false;
        if (phase === "twostep.policy") state.policySent = false;
        if (phase === "key.project") {
          state.projectSent = false;
          state.projectId = "";
        }
        if (phase === "key.admin-sdk") state.apiSent = false;
        if (phase === "key.consent") {
          state.consentSent = false;
          state.consentStarted = false;
        }
        if (phase === "key.client") state.clientSent = false;
      }
      state.phase = phase;
      state.paused = false;
      state.lastHandoff = Date.now();
      if (message.profile?.adminEmail) state.profile.adminEmail = message.profile.adminEmail;
      start();
      sendResponse({ ok: true, url: location.href });
      return true;
    }

    if (message.type === "setup-stop") {
      state.paused = true;
      sendResponse({ ok: true });
      return true;
    }

    if (message.type === "setup-domain-ready") {
      state.domainReady = true;
      note("DuckDNS record is visible — telling Google to verify");
      sendResponse({ ok: true });
      return true;
    }

    if (message.type === "setup-ping") {
      sendResponse({ ok: true, url: location.href, active: Boolean(state.profile) });
      return true;
    }

    return false;
  });
})();
