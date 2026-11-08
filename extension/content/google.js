// language: JavaScript, file: content/google.js
// Fills Google's own sign-in pages. Runs as a document-level content script so
// it survives the SPA transitions identifier -> password -> challenge.
//
// It never starts on its own: the runner hands it credentials for one identity.

(() => {
  // Two copies of this file in one document are two state machines: two timers,
  // two fills, two clicks on the same button, two "backup code entered" lines.
  // That is how one code got submitted a dozen times. First copy wins.
  if (globalThis.__racGoogle) return;
  globalThis.__racGoogle = true;

  const api = globalThis.browser ?? globalThis.chrome;

  // How long the same action is held off for. Long enough to survive an SPA
  // rerender, short enough that a genuine retry is not delayed into the next
  // screen.
  const DEBOUNCE_MS = 2600;
  // Google locks a sign-in after a handful of bad codes, so the list is walked
  // three deep and then the lane stops with the reason instead of grinding.
  const MAX_CODES = 3;
  const CODE_REFUSED = /didn.?t work|wrong|not valid|invalid|incorrect|already (been )?used|expired/i;

  const state = {
    creds: null,
    timer: null,
    lastAction: "",
    lastAt: 0,
    triedAnotherWay: false,
    codeSubmitted: false,
    codes: [],
    codeIndex: 0,
    codeAt: 0,
    codeTried: -1,
    codeBody: "",
    otpSecret: "",
    otpSubmitted: false,
    enrolled: false,
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

  // Google's terms interstitial and consent buttons carry the whole label in a
  // single button, so an exact match is safer than a substring one here.
  function clickExact(labels, scopeSelector) {
    const wanted = labels.map((label) => label.toLowerCase());
    const nodes = [
      ...document.querySelectorAll(scopeSelector || 'button,[role="button"],a'),
    ].filter(visible);
    for (const node of nodes) {
      const label = `${node.getAttribute("aria-label") || ""} ${node.innerText || ""}`
        .replace(/\s+/g, " ")
        .trim()
        .toLowerCase();
      if (!label) continue;
      if (wanted.some((want) => label === want || label.startsWith(`${want} `))) {
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

  // ---------------------------------------------------------------- local TOTP
  // A content script is a classic script, so the RFC 6238 math is inlined
  // instead of imported from lib/totp.js. Same defaults: SHA-1, 6 digits, 30s.

  const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

  function base32Bytes(secret) {
    const clean = String(secret || "")
      .toUpperCase()
      .replace(/[\s-]/g, "")
      .replace(/=+$/, "");
    const out = [];
    let buffer = 0;
    let bits = 0;
    for (const char of clean) {
      const index = BASE32.indexOf(char);
      if (index < 0) return null;
      buffer = (buffer << 5) | index;
      bits += 5;
      if (bits >= 8) {
        bits -= 8;
        out.push((buffer >>> bits) & 0xff);
        buffer &= (1 << bits) - 1;
      }
    }
    return out.length ? Uint8Array.from(out) : null;
  }

  async function totpNow(secret, period = 30, digits = 6) {
    try {
      const key = base32Bytes(secret);
      if (!key || !globalThis.crypto || !crypto.subtle) return "";
      const message = new Uint8Array(8);
      let rest = Math.floor(Date.now() / 1000 / period);
      for (let index = 7; index >= 0; index -= 1) {
        message[index] = rest & 0xff;
        rest = Math.floor(rest / 256);
      }
      const hmac = await crypto.subtle.importKey(
        "raw",
        key,
        { name: "HMAC", hash: "SHA-1" },
        false,
        ["sign"]
      );
      const mac = new Uint8Array(await crypto.subtle.sign("HMAC", hmac, message));
      const offset = mac[mac.length - 1] & 0x0f;
      const binary =
        ((mac[offset] & 0x7f) << 24) |
        ((mac[offset + 1] & 0xff) << 16) |
        ((mac[offset + 2] & 0xff) << 8) |
        (mac[offset + 3] & 0xff);
      return String(binary % 10 ** digits).padStart(digits, "0");
    } catch (_) {
      return "";
    }
  }

  // Google prints the setup key as sixteen base32 characters in groups of four,
  // either as page text or inside a read-only field.
  function scrapeSecret() {
    const text = document.body ? document.body.innerText || "" : "";
    const grouped = /\b([A-Z2-7]{4}(?:[\s-]+[A-Z2-7]{4}){3,7})\b/.exec(text);
    if (grouped) return grouped[1].replace(/[\s-]/g, "");
    for (const input of [...document.querySelectorAll("input")].filter(visible)) {
      const value = String(input.value || "").replace(/[\s-]/g, "");
      if (/^[A-Z2-7]{16,32}$/.test(value)) return value;
    }
    // Google sometimes renders the key in its own node with no separators.
    for (const node of document.querySelectorAll("div,span,code,b,strong")) {
      const value = String(node.innerText || "").replace(/[\s-]/g, "");
      if (/^[A-Z2-7]{16,32}$/.test(value) && visible(node)) return value;
    }
    return "";
  }

  function keepSecret(secret) {
    if (!secret || state.kept === secret) return;
    state.kept = secret;
    const email = state.creds?.email || "";
    note(`authenticator secret captured for ${email || "this identity"} (${secret.length} chars)`);
    try {
      const sent = api.runtime.sendMessage({ type: "identity-totp", email, secret });
      if (sent && typeof sent.catch === "function") sent.catch(() => {});
    } catch (_) {
      // extension context gone
    }
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

  // A blind timer submits before the page has committed the value, and a
  // re-render between typing and clicking is the usual way a code is lost. Read
  // the field back first, refill once if it was cleared, then click.
  function submitAfter(field, value, delay = 900, tries = 0) {
    setTimeout(() => {
      const live = field && field.isConnected ? field : codeField();
      if (!live) return;
      if (String(live.value || "") !== value) {
        if (tries < 2) {
          fill(live, value);
          submitAfter(live, value, 600, tries + 1);
        }
        return;
      }
      pressNext();
    }, delay);
  }

  // Scored, not first-match: the challenge screen can carry more than one
  // visible input, and typing a backup code into the wrong one reads as "wrong
  // code" without Google ever having seen it.
  function codeField() {
    const inputs = [...document.querySelectorAll("input")].filter(visible);
    let best = null;
    let bestScore = 0;
    for (const input of inputs) {
      const type = (input.type || "text").toLowerCase();
      if (type === "password" || type === "email" || type === "checkbox") continue;
      // The setup key sits in a read-only field on the same screen as the code
      // input; never type the current code into it.
      if (input.readOnly || input.disabled) continue;
      if (/^[A-Z2-7]{16,32}$/.test(String(input.value || "").replace(/[\s-]/g, ""))) continue;
      const hints = `${input.getAttribute("aria-label") || ""} ${input.name || ""} ${
        input.placeholder || ""
      } ${input.id || ""}`.toLowerCase();
      let score = 0;
      if (input.autocomplete === "one-time-code") score += 6;
      if (/backup|verification code|one-time/.test(hints)) score += 5;
      if (/code|otp|verify/.test(hints)) score += 3;
      if (Number(input.maxLength) === 8) score += 4;
      if (Number(input.maxLength) > 0 && Number(input.maxLength) <= 12) score += 1;
      if (type === "tel") score += 2;
      if (type === "text" || type === "tel" || type === "number") score += 1;
      if (score > bestScore) {
        best = input;
        bestScore = score;
      }
    }
    return best || document.querySelector("#backupCodePin");
  }

  // One line in the log describing the field we typed into, so the next bad
  // code arrives with the shape of the screen that refused it.
  function fieldFingerprint(field) {
    if (!field) return "none";
    return [
      field.tagName.toLowerCase(),
      field.type || "text",
      `max=${field.maxLength}`,
      field.autocomplete || "-",
      (field.getAttribute("aria-label") || "-").slice(0, 40),
    ].join(" ");
  }

  // Debounce only: the same action may look needed for several ticks while the
  // SPA rerenders, and repeating a click is how a flow gets two submissions in.
  // Each run resets the clock. There is deliberately no "repeats" budget — the
  // old one ran out after three skips and turned this back into run-every-tick.
  function act(key, run) {
    const now = Date.now();
    if (state.lastAction === key && now - state.lastAt < DEBOUNCE_MS) return true;
    state.lastAction = key;
    state.lastAt = now;
    return Boolean(run());
  }

  // Google states the reason inline and moves on; the sentence is worth more in
  // the log than "refused", because it separates a spent code from a wrong one.
  function describeRefusal(body) {
    const hit = body.match(
      /[^.\n]{0,80}(didn.?t work|wrong|not valid|invalid|incorrect|already (been )?used|expired)[^.\n]{0,60}/i
    );
    return hit ? hit[0].replace(/\s+/g, " ").trim().slice(0, 120) : "no error text";
  }

  function tick() {
    if (!state.creds) return;
    if (!location.hostname.endsWith("google.com")) return;
    if (!document.body) return;

    const body = bodyText();
    const { email, password, code } = state.creds;

    // Google's browser-support check frame renders between steps and needs no
    // interaction. Without this it reads as an "unrecognised screen".
    if (location.pathname.includes("bscframe")) {
      once("bsc-frame", "browser-support check frame");
      return;
    }

    if (state.fillUrl !== location.href) {
      state.fillUrl = location.href;
      state.fills = { identifier: 0, password: 0 };
      // A TOTP is reusable and time-based: a new page that asks for it again
      // (enrolment -> challenge) must be allowed to answer.
      state.otpSubmitted = false;
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

    // The brand-new-account terms interstitial. It carries no field, so without
    // this click the whole flow sits on it until the lane times out.
    if (
      /welcome to your new account|before you continue to|your organization administrator manages|terms of service/i.test(
        body
      ) &&
      !document.querySelector('input[type="password"]')
    ) {
      act("interstitial", () => {
        const hit =
          clickExact(["i understand", "accept", "agree", "got it", "ok", "continue"]) ||
          clickByText(["i understand", "accept", "agree"]);
        if (hit) once("interstitial", `accepted: ${hit}`);
        return hit;
      });
      return;
    }

    // Enrollment finished: keep the secret and dismiss the confirmation.
    if (
      state.otpSecret &&
      /(2|two)-step verification is on|you.re all set|2-step verification is turned on/i.test(body)
    ) {
      keepSecret(state.otpSecret);
      if (!state.enrolled) {
        act("enrol-done", () => {
          state.enrolled = true;
          const hit = clickByText(["done", "turn on", "finish"], "button,[role=button],a");
          once("enrol-done", "2SV enrolled with the authenticator app");
          return hit;
        });
      }
      return;
    }

    // 2SV enrolment with the authenticator app. Google only offers the phone
    // when the tenant policy allows SMS, so with SMS disallowed this is the
    // screen that has to be finished before the challenge can be answered.
    const enrollPage = /scan the qr|can.t scan|setup key|enter a setup key|set up (2|two)-step|add a second step/i.test(
      body
    );

    // A signed-in challenge that offers the authenticator, answered with the
    // secret captured at enrolment. Checked before enrolment so a stored secret
    // is spent on the challenge rather than on a second set-up attempt.
    if (
      !state.otpSubmitted &&
      (state.creds.otp || state.otpSecret) &&
      body.includes("authenticator") &&
      !enrollPage
    ) {
      const field = codeField();
      if (field && visible(field)) {
        act("otp-challenge", () => {
          state.otpSubmitted = true;
          totpNow(state.creds.otp || state.otpSecret).then((value) => {
            if (!value) return;
            fill(field, value);
            note("authenticator challenge answered");
            submitAfter(field, value, 1200);
          });
          return true;
        });
        return;
      }
      if (
        act("pick-challenge-auth", () =>
          clickByText(["authenticator app", "use your authenticator"], "button,[role=button],li,div")
        )
      ) {
        return;
      }
      // nothing clickable on this screen: let the phone/method branches below run
    }

    const enrolling = body.includes("authenticator app") && enrollPage;

    if (enrolling && !state.enrolled) {
      if (!state.otpSecret) {
        const secret = scrapeSecret();
        if (secret) {
          state.otpSecret = secret;
          keepSecret(secret);
        }
      }

      if (!state.otpSecret && /can.t scan|setup key|enter a setup key/i.test(body)) {
        act("cant-scan", () => {
          const hit = clickByText([
            "can't scan it",
            "cant scan it",
            "can\u2019t scan it",
            "can't scan the qr code",
            "enter a setup key",
            "setup key",
          ]);
          if (hit) once("cant-scan", `setup key panel -> ${hit}`);
          return hit;
        });
        return;
      }

      const field = codeField();
      if (state.otpSecret && field && visible(field) && !state.otpSubmitted) {
        act("otp-fill", () => {
          state.otpSubmitted = true;
          totpNow(state.otpSecret).then((value) => {
            if (!value) return;
            fill(field, value);
            note("authenticator code entered");
            submitAfter(field, value, 1200);
          });
          return true;
        });
        return;
      }

      if (/scan the qr/i.test(body)) {
        act("qr-next", () => pressNext());
        return;
      }

      act("pick-auth", () => {
        const hit = clickByText(["authenticator app"], "button,[role=button],[role=radio],li,div");
        if (hit) once("pick-auth", `enrol method -> ${hit}`);
        return hit;
      });
      return;
    }

    // backup code entry. Every code is single-use, so a refusal advances to the
    // next one instead of latching the lane on a code that is already spent. A
    // success navigates away, so "the field is still here" is the signal that
    // this one did not take — but only after Google has had time to say why.
    const codes = state.codes.length ? state.codes : code ? [code] : [];
    const limit = Math.min(codes.length, MAX_CODES);
    const challengeOffersCode =
      body.includes("backup code") || body.includes("one of your backup codes");

    if (challengeOffersCode && limit) {
      const field = codeField();
      const open = Boolean(field && visible(field));
      // Only *new* text counts. The challenge screen carries help copy that can
      // contain "invalid" before anything has been typed, so the refusal has to
      // be a change from what the screen looked like at submit time.
      const refused = body !== state.codeBody && CODE_REFUSED.test(body);

      if (open && state.codeTried >= 0) {
        const held = Date.now() - state.codeAt;
        // An error is answered in place within a second or two; a slow success
        // is given a much wider berth before the code is written off.
        if ((refused && held > 3000) || held > 14_000) {
          const why = describeRefusal(body);
          state.codeTried = -1;
          state.codeAt = 0;
          state.codeBody = "";
          state.codeIndex += 1;
          state.lastAction = "";
          if (state.codeIndex >= limit) {
            once("codes-out", `no backup code accepted after ${limit} tries (${why})`);
          } else {
            note(`code ${state.codeIndex} refused (${why}) — trying ${state.codeIndex + 1}/${limit}`);
          }
        }
      }

      if (open && state.codeIndex < limit) {
        // One shot per index. The old code re-ran the same entry on every tick,
        // retyped the same rejected value and refreshed its own timeout, so the
        // list never advanced and Google saw a burst of bad submits.
        if (state.codeTried !== state.codeIndex) {
          const value = String(codes[state.codeIndex]).replace(/[\s-]/g, "");
          state.codeTried = state.codeIndex;
          state.codeAt = Date.now();
          state.codeSubmitted = true;
          state.codeBody = body;
          fill(field, value);
          once("code-field", `typing into ${fieldFingerprint(field)}`);
          note(
            `backup code ${state.codeIndex + 1}/${limit} entered ` +
              `(${value.length} chars, ${value.slice(0, 2)}…)`
          );
          submitAfter(field, value);
        }
        return;
      }

      // Never click a label while the entry field is open — the screen's own
      // heading matches the label text, and clicking it loops without typing.
      if (!open && (body.includes("enter one of your backup codes") || body.includes("use a backup code"))) {
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
      return;
    }

    // A backup-code challenge with nothing to type into it would loop on the
    // method list, so say so instead of clicking through to a dead end.
    if (challengeOffersCode && !limit) {
      once("no-codes", "backup-code challenge but this identity has no stored code");
      return;
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

    // Phone wall. Google falls back to SMS whenever the tenant policy allows it,
    // and an unenrolled account has nothing else to offer.
    if (
      /enter a phone number|get a verification code|text message with a verification code|standard message and data rates/i.test(
        body
      )
    ) {
      once(
        "phone-wall",
        "phone challenge — the tenant still allows SMS. Admin console → Security → Authentication → 2-Step Verification → Methods: Any except verification codes via text, phone call, Enforcement: On. Google then offers the authenticator app and the run enrols it automatically"
      );
      return;
    }

    // Enforcement on, but the policy allows no method this account can satisfy:
    // nothing can be enrolled, so no code can be entered. Tenant setting.
    if (/2-step verification policy|don.?t meet your organization/i.test(body)) {
      once(
        "policy",
        "the 2SV policy offers no usable method — set Admin console → Security → Authentication → 2-Step Verification → Methods: Any except verification codes via text, phone call (leave phone and security key in the list only if you can satisfy them)"
      );
      return;
    }

    // unknown screen: wait before calling it stuck, most screens are transient
    if (state.stuckUrl !== location.href) {
      state.stuckUrl = location.href;
      state.stuckSince = Date.now();
    }
    if (Date.now() - state.stuckSince > 8000) {
      once("stuck", `unrecognised screen at ${location.pathname} (${document.visibilityState})`);
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
        otp: message.otp || "",
      };
      // The runner sends every stored backup code; a refusal walks to the next
      // one without the lane dying on a spent code.
      state.codes = Array.isArray(message.codes) ? message.codes.filter(Boolean) : [];
      // A new identity means a new flow, even in the same document.
      if (state.lastEmail !== message.email) {
        state.lastEmail = message.email;
        state.triedAnotherWay = false;
        state.codeSubmitted = false;
        state.codeIndex = 0;
        state.codeAt = 0;
        state.codeTried = -1;
        state.codeBody = "";
        state.otpSecret = "";
        state.otpSubmitted = false;
        state.enrolled = false;
        state.kept = "";
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
