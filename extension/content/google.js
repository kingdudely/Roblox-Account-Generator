// language: JavaScript, file: content/google.js
// Fills Google's own sign-in pages. Runs as a document-level content script so
// it survives the SPA transitions identifier -> password -> challenge.
//
// It never starts on its own: the runner hands it credentials for one identity.

(() => {
  const api = globalThis.browser ?? globalThis.chrome;

  const state = {
    creds: null,
    timer: null,
    lastAction: "",
    lastAt: 0,
    repeats: 0,
    triedAnotherWay: false,
    codeSubmitted: false,
    reported: new Set(),
  };

  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 1 && rect.height > 1;
  };

  const bodyText = () => (document.body ? document.body.innerText || "" : "").toLowerCase();

  function note(text, level = "info") {
    try {
      api.runtime.sendMessage({ type: "log", level, text: `google: ${text}` });
    } catch (_) {
      // extension context gone
    }
  }

  function once(key, text) {
    if (state.reported.has(key)) return;
    state.reported.add(key);
    note(text);
  }

  function fill(element, value) {
    if (!element) return false;
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
      return true;
    } catch (_) {
      return false;
    }
  }

  function clickByText(needles, scopeSelector) {
    const nodes = [
      ...document.querySelectorAll(
        scopeSelector || 'button,[role="button"],a,[role="link"],span,div'
      ),
    ].filter(visible);
    const lowered = needles.map((needle) => needle.toLowerCase());
    for (const node of nodes) {
      const label = `${node.getAttribute("aria-label") || ""} ${node.innerText || ""}`
        .replace(/\s+/g, " ")
        .trim()
        .toLowerCase();
      if (!label) continue;
      if (lowered.some((needle) => label === needle || label.includes(needle))) {
        try {
          node.click();
          return label.slice(0, 60);
        } catch (_) {
          // try the next match
        }
      }
    }
    return null;
  }

  function pressNext() {
    const direct =
      document.querySelector("#identifierNext button") ||
      document.querySelector("#passwordNext button") ||
      document.querySelector("#submit button") ||
      document.querySelector('button[jsname="LgbsSe"]');
    if (direct && visible(direct)) {
      direct.click();
      return "button";
    }
    const byText = clickByText(["next", "continue", "verify"], "button,[role=button]");
    if (byText) return byText;
    const form = document.querySelector("form");
    if (form) {
      try {
        form.requestSubmit();
        return "forms";
      } catch (_) {
        try {
          form.submit();
          return "form";
        } catch (_) {
          // fall through
        }
      }
    }
    return null;
  }

  function codeField() {
    const inputs = [...document.querySelectorAll("input")].filter(visible);
    for (const input of inputs) {
      const type = (input.type || "text").toLowerCase();
      if (type === "password" || type === "email" || type === "checkbox") continue;
      const hints = `${input.getAttribute("aria-label") || ""} ${input.name || ""} ${
        input.placeholder || ""
      }`.toLowerCase();
      if (/code|backup|verify|otp/.test(hints)) return input;
      if (type === "tel") return input;
      if (input.maxLength > 0 && input.maxLength <= 12 && input.autocomplete !== "username") {
        return input;
      }
    }
    return document.querySelector("#backupCodePin");
  }

  // Debounce: the same action may look needed for several ticks while the SPA
  // rerenders. Repeating a click is how a flow gets two submissions in.
  function act(key, run) {
    const now = Date.now();
    if (state.lastAction === key && now - state.lastAt < 2600 && state.repeats < 3) {
      state.repeats += 1;
      return true;
    }
    if (state.lastAction !== key) state.repeats = 0;
    state.lastAction = key;
    state.lastAt = now;
    const outcome = run();
    return Boolean(outcome);
  }

  function tick() {
    if (!state.creds) return;
    if (!location.hostname.endsWith("google.com")) return;
    if (!document.body) return;

    const body = bodyText();
    const { email, password, code } = state.creds;

    if (state.fillUrl !== location.href) {
      state.fillUrl = location.href;
      state.fills = { identifier: 0, password: 0 };
    }

    // identifier
    const identifier = document.querySelector('input[type="email"], #identifierId');
    if (identifier && visible(identifier) && !state.codeSubmitted) {
      if (identifier.value !== email) {
        if (state.fills.identifier >= 3) {
          once("identifier-stuck", "identifier keeps clearing — check the address on the tenant");
          return;
        }
        act("identifier", () => {
          state.fills.identifier += 1;
          fill(identifier, email);
          once("identifier", `identifier for ${email}`);
          return true;
        });
        return;
      }
      act("identifier-next", () => pressNext());
      return;
    }

    // account chooser
    if (body.includes("use another account")) {
      act("chooser", () => {
        const hit = clickByText(["use another account"]);
        once("chooser", "chooser: use another account");
        return hit;
      });
      return;
    }

    // password
    const passwordInput = document.querySelector('input[type="password"]');
    if (passwordInput && visible(passwordInput)) {
      if (passwordInput.value !== password) {
        if (state.fills.password >= 3) {
          once("password-stuck", "password rejected repeatedly — the identity password does not match");
          return;
        }
        act("password", () => {
          state.fills.password += 1;
          fill(passwordInput, password);
          once("password", "password entered");
          return true;
        });
        return;
      }
      act("password-next", () => pressNext());
      return;
    }

    // backup code entry
    if (
      !state.codeSubmitted &&
      code &&
      (body.includes("backup code") || body.includes("one of your backup codes"))
    ) {
      if (body.includes("enter one of your backup codes") || body.includes("use a backup code")) {
        act("pick-backup", () => {
          const hit = clickByText([
            "enter one of your backup codes",
            "use a backup code",
            "backup code",
          ]);
          if (hit) once("pick-backup", `method list -> ${hit}`);
          return hit;
        });
        return;
      }
      const field = codeField();
      if (field && visible(field)) {
        act("code", () => {
          fill(field, code);
          note("backup code entered");
          setTimeout(() => {
            const still = codeField();
            if (still && visible(still)) pressNext();
          }, 1800);
          state.codeSubmitted = true;
          return true;
        });
        return;
      }
    }

    // get from a phone challenge to the method list
    if (!state.triedAnotherWay && body.includes("try another way")) {
      act("another-way", () => {
        state.triedAnotherWay = true;
        const hit = clickByText([
          "try another way",
          "more ways to verify",
          "other ways to verify",
        ]);
        once("another-way", `challenge -> ${hit}`);
        return hit;
      });
      return;
    }

    // method list with backup codes
    if (!state.codeSubmitted && body.includes("backup")) {
      act("method", () => {
        const hit = clickByText([
          "enter one of your backup codes",
          "backup codes",
          "use a backup code",
          "backup code",
        ]);
        return hit;
      });
      return;
    }

    // consent / interstitial
    if (
      body.includes("wants to access") ||
      body.includes("continue to") ||
      location.pathname.includes("consent")
    ) {
      act("consent", () => clickByText(["continue", "allow", "i understand", "got it"]));
      return;
    }

    if (/verify your device|scan the qr|qr code|scan this/i.test(body)) {
      once("phone-gate", "phone gate detected — open Setup and press Scan Google tab");
      return;
    }

    // unknown screen: wait before calling it stuck, most screens are transient
    if (state.stuckUrl !== location.href) {
      state.stuckUrl = location.href;
      state.stuckSince = Date.now();
    }
    if (Date.now() - state.stuckSince > 8000) {
      once("stuck", `unrecognised screen at ${location.pathname}`);
    }
  }

  function start() {
    tick();
    if (state.timer) return;
    state.timer = setInterval(tick, 900);
  }

  api.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message || typeof message !== "object") return false;

    if (message.type === "google-fill") {
      state.creds = {
        email: message.email,
        password: message.password,
        code: message.code,
      };
      // A new identity means a new flow, even in the same document.
      if (state.lastEmail !== message.email) {
        state.lastEmail = message.email;
        state.triedAnotherWay = false;
        state.codeSubmitted = false;
        state.reported = new Set();
        state.lastAction = "";
        note(`handed credentials for ${message.email}`);
      }
      start();
      sendResponse({ ok: true, url: location.href });
      return true;
    }

    if (message.type === "google-ping") {
      sendResponse({ ok: true, url: location.href, active: Boolean(state.creds) });
      return true;
    }

    return false;
  });

  start();
})();
