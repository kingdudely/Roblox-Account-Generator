// language: JavaScript, file: content/setup.js
// Drives the Cloud Identity signup and the first half of the Google Cloud
// console. It is deliberately one-way: the runner owns the phase and supplies
// the profile, and this script only fills/clicks the page in front of it. The
// phone gate and any captcha are reported and left to the operator.
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
    phoneSent: false,
    captchaSent: false,
    tryAnotherWay: false,
    domainToken: "",
    domainReady: false,
    billingSent: false,
    domainVerifiedSent: false,
    policySent: false,
    clientSent: false,
    projectId: "",
    projectSent: false,
    paused: false,
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

  function passwordInputs() {
    return [...document.querySelectorAll('input[type="password"]')].filter(visible);
  }

  function fillIfEmpty(input, value) {
    if (!input || !value) return false;
    if (String(input.value || "").trim()) return false;
    return fill(input, value);
  }

  function selectOption(select, pattern) {
    if (!select) return false;
    const wanted = [...select.options].find((option) =>
      pattern.test(`${option.textContent || ""} ${option.value || ""}`)
    );
    if (!wanted) return false;
    select.value = wanted.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function maybePhoneGate(url, body) {
    if (
      /\/challenge\/iap\/qrcode/i.test(url) ||
      /scan the qr code with your phone/i.test(body)
    ) {
      if (!state.phoneSent) {
        state.phoneSent = true;
        note("phone gate reached — focus the setup tab and scan the QR / send the SMS");
        send({ type: "setup-phone", url, text: (document.body?.innerText || "").slice(0, 1200) });
      }
      return true;
    }
    return false;
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

  // -------------------------------------------------------------- signup funnel

  function signupTick(body) {
    const path = location.pathname.replace(/\/+$/, "");

    if (maybePhoneGate(location.href, body)) return;
    if (maybeCaptcha(body)) return;

    if (/\/gcpidentity\/signup$/i.test(path)) {
      const business =
        document.querySelector('input[aria-label*="Business name" i]') || textInputs()[0];
      if (business && business.value !== state.profile.businessName) {
        fill(business, state.profile.businessName);
      }

      const radios = [...document.querySelectorAll('input[type="radio"]')];
      if (radios.length && !radios.some((radio) => radio.checked)) {
        const radio = radios.find((item) => visible(item)) || radios[0];
        const label =
          radio.closest("label") ||
          (radio.id ? document.querySelector(`label[for="${radio.id}"]`) : null);
        (label || radio).click();
      }
      const select = [...document.querySelectorAll("select")].find(visible);
      if (select && !select.value) selectOption(select, /united states|us/i);

      if (business && String(business.value || "").trim()) {
        act("signup-start", () => clickByText(["next"], CONTROLS));
      }
      return;
    }

    if (/\/signup\/contact$/i.test(path)) {
      const inputs = textInputs();
      if (inputs[0] && inputs[0].value !== state.profile.firstName) {
        fill(inputs[0], state.profile.firstName);
      }
      if (inputs[1] && inputs[1].value !== state.profile.lastName) {
        fill(inputs[1], state.profile.lastName);
      }
      const email = document.querySelector('input[type="email"]');
      if (email && email.value !== state.profile.recoveryEmail) {
        fill(email, state.profile.recoveryEmail);
      }
      const ready = inputs[0]?.value && inputs[1]?.value && email?.value;
      if (ready) act("contact-next", () => clickByText(["next"], CONTROLS));
      return;
    }

    if (/\/signup\/bring$/i.test(path)) {
      const input = textInputs()[0];
      if (input && input.value !== state.profile.domain) {
        fill(input, state.profile.domain);
      }
      if (input?.value) act("bring-next", () => clickByText(["next"], CONTROLS));
      return;
    }

    if (/\/signup\/bringconfirm$/i.test(path)) {
      act("bringconfirm-next", () => clickByText(["next"], CONTROLS));
      return;
    }

    if (/\/signup\/account$/i.test(path)) {
      if (/creating your account|please wait/i.test(body)) return;
      const inputs = textInputs();
      const username = inputs[0];
      if (username && username.value !== state.profile.adminUser) {
        fill(username, state.profile.adminUser);
      }
      for (const input of passwordInputs()) {
        if (input.value !== state.profile.adminPassword) {
          fill(input, state.profile.adminPassword);
        }
      }
      const passwords = passwordInputs();
      const ready =
        username?.value &&
        passwords.length >= 2 &&
        passwords.every((input) => String(input.value || "").trim());
      if (ready) {
        act("account-create", () =>
          clickExact(["agree and continue", "agree & continue"], CONTROLS)
        );
      }
      return;
    }

    if (/\/verify\//i.test(path)) {
      if (maybeDomainToken()) {
        if (state.domainReady) {
          act("verify-domain", () => clickByText(["verify", "continue"], CONTROLS));
        }
        return;
      }
      act("verify-next", () =>
        clickByText(["next", "continue", "get started", "verify"], CONTROLS)
      );
      return;
    }

    // The signup flow can park on a plain accounts.google.com password page
    // after "Agree and continue". Handle that here too.
    act("signup-idle", () =>
      clickByText(["next", "continue", "get started", "i understand"], CONTROLS)
    );
  }

  // ------------------------------------------------------------------ accounts

  function accountsTick(body) {
    if (maybePhoneGate(location.href, body)) {
      if (state.tryAnotherWay) {
        act("try-another-way", () =>
          clickByText(["try another way", "more ways to verify"], CONTROLS)
        );
      }
      return;
    }

    if (state.tryAnotherWay) {
      act("pick-sms", () =>
        clickByText(
          ["text message", "send sms", "sms", "phone"],
          'button,[role="button"],[role="radio"],li,a'
        )
      );
      return;
    }

    if (/\/signin\/challenge\/pwd/i.test(location.href) || /enter your password/i.test(body)) {
      const field = document.querySelector('input[name="Passwd"], input[type="password"]');
      fillIfEmpty(field, state.profile.adminPassword);
      if (maybeCaptcha(body)) return;
      if (field?.value) act("admin-password", () => clickByText(["next"], CONTROLS));
      return;
    }

    if (/\/signin\/identifier/i.test(location.href)) {
      const email = document.querySelector('input[type="email"], input[name="identifier"]');
      fillIfEmpty(email, state.profile.adminEmail);
      if (email?.value) act("admin-identifier", () => clickByText(["next"], CONTROLS));
      return;
    }

    if (/speedbump|terms/i.test(location.href) || /i understand/i.test(body)) {
      act("speedbump", () => clickByText(["i understand", "continue", "accept"], CONTROLS));
      return;
    }

    // A consent screen in the same tab (the OAuth client authorization is
    // handled by the runner, but this covers the signup Continue page).
    act("accounts-idle", () =>
      clickByText(["continue", "allow", "next", "i understand", "got it"], CONTROLS)
    );
  }

  // ------------------------------------------------------------------- admin

  function adminTick(body) {
    if (maybeDomainToken()) {
      if (state.domainReady) {
        act("admin-verify-domain", () =>
          clickByText(["verify domain", "verify", "continue"], CONTROLS)
        );
      }
      if (/domain is verified|domain verified|verification successful|verified/i.test(body)) {
        if (!state.domainVerifiedSent) {
          state.domainVerifiedSent = true;
          note("domain verified — moving on");
          send({ type: "setup-domain-verified", domain: state.profile?.domain || "" });
        }
      }
      return;
    }

    if (/\/ac\/domains\/manage/i.test(location.pathname)) {
      if (/domain is verified|domain verified|verification successful/i.test(body)) {
        if (!state.domainVerifiedSent) {
          state.domainVerifiedSent = true;
          note("domain verified — moving on");
          send({ type: "setup-domain-verified", domain: state.profile?.domain || "" });
        }
        return;
      }
      act("admin-domains-open", () =>
        clickByText(["verify domain", "verify", "continue"], CONTROLS)
      );
      return;
    }

    if (/\/ac\/billing\/catalog/i.test(location.pathname)) {
      if (/cloud identity free/i.test(body) && /manage|active|subscribed|you.re subscribed/i.test(body)) {
        if (!state.billingSent) {
          state.billingSent = true;
          note("Cloud Identity Free is active");
          send({ type: "setup-billing-done" });
        }
        return;
      }
      act("billing-open", () => {
        return (
          clickExact(["cloud identity"], CONTROLS) ||
          clickByText(["cloud identity"], CONTROLS) ||
          clickByText(["cloud identity free", "get started", "start", "free"], CONTROLS)
        );
      });
      return;
    }

    if (/\/ac\/security\/2sv/i.test(location.pathname)) {
      if (/any except verification codes via text, phone call/i.test(body) && /enforcement/i.test(body)) {
        if (!state.policySent) {
          state.policySent = true;
          note("2SV policy looks set (enforcement + methods)");
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

    act("admin-idle", () => clickByText(["next", "continue", "get started"], CONTROLS));
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

  function setLocation(url) {
    if (!url || url === location.href) return false;
    note(`navigating to ${url.slice(0, 110)}`);
    location.href = url;
    return true;
  }

  function consoleTick(body) {
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
      const input =
        document.querySelector('input[aria-label*="Project name" i]') ||
        document.querySelector('input#project-name') ||
        textInputs()[0];
      fillIfEmpty(input, state.profile.projectName);
      if (input?.value) {
        act("project-create", () => clickExact(["create", "create project"], CONTROLS));
      }
      if (state.projectId) {
        setLocation(
          `https://console.cloud.google.com/apis/library/admin.googleapis.com?project=${state.projectId}`
        );
      }
      return;
    }

    if (/\/apis\/library\/admin\.googleapis\.com/i.test(location.pathname)) {
      if (/api enabled|disable/i.test(body)) {
        if (state.projectId) {
          setLocation(
            `https://console.cloud.google.com/apis/credentials/consent?project=${state.projectId}`
          );
        }
        return;
      }
      act("enable-admin-sdk", () => clickExact(["enable"], CONTROLS));
      return;
    }

    if (/\/apis\/credentials\/consent/i.test(location.pathname)) {
      if (/data access|scopes/i.test(body) && !/get started/i.test(body)) {
        act("consent-scopes", () =>
          clickByText(["add or remove scopes", "data access", "edit"], CONTROLS)
        );
        act("consent-update", () => clickByText(["update", "save", "confirm"], CONTROLS));
        once(
          "consent-manual",
          "consent screen reached — if it stops here, add the five scopes and create the OAuth client, then paste the JSON below",
          "warn"
        );
        return;
      }
      if (/get started/i.test(body)) {
        act("consent-start", () => clickExact(["get started"], CONTROLS));
        return;
      }
      const nameInput =
        document.querySelector('input[aria-label*="App name" i]') ||
        document.querySelector('input[aria-label*="Application name" i]');
      fillIfEmpty(nameInput, state.profile.businessName);
      const emailInput =
        document.querySelector('input[type="email"]') || textInputs().find((i) => i.type === "email");
      fillIfEmpty(emailInput, state.profile.recoveryEmail);
      act("consent-next", () =>
        clickExact(["next", "save and continue", "create", "save"], CONTROLS)
      );
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

    act("console-idle", () => clickByText(["next", "continue", "create", "enable"], CONTROLS));
  }

  // --------------------------------------------------------------------- tick

  function tick() {
    if (!state.profile || state.paused) return;
    if (!document.body) return;

    const url = location.href;
    if (url !== state.lastUrl) {
      state.lastUrl = url;
      state.reported = new Set();
      state.phoneSent = false;
      state.captchaSent = false;
      state.lastAction = "";
      note(`page ${url.slice(0, 140)}`);
    }

    const body = bodyText();
    const host = location.hostname;

    if (host.endsWith("accounts.google.com")) {
      accountsTick(body);
      return;
    }
    if (host === "workspace.google.com" || host.endsWith(".workspace.google.com")) {
      signupTick(body);
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
      state.phase = message.phase || "";
      state.paused = false;
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

    if (message.type === "setup-try-another-way") {
      state.tryAnotherWay = true;
      const hit = clickByText(["try another way", "more ways to verify"], CONTROLS);
      sendResponse({ ok: Boolean(hit), hit });
      return true;
    }

    if (message.type === "setup-ping") {
      sendResponse({ ok: true, url: location.href, active: Boolean(state.profile) });
      return true;
    }

    return false;
  });
})();
