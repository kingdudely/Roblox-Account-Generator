import * as adminApi from "../extension/lib/admin.js";
import { load, mutate } from "./state.js";
import { sendGoogleFill } from "./content-bridge.js";
import { info, ok, warn } from "./util.js";

export async function storeTotp(email, secret, { enrolled = false, source = "captured" } = {}) {
  if (!secret) return;
  await mutate((state) => {
    const owner = String(email || state.totp.email || "").toLowerCase();
    state.totp = {
      ...(state.totp || {}),
      email: owner,
      secret,
      label: source,
      captured_at: new Date().toISOString(),
    };
    if (enrolled) state.setup.done["twostep.enroll"] = true;
  });
  ok(`${email || "admin"}: authenticator secret captured${enrolled ? " and accepted" : ""}`);
}

export async function mintCodesAndResend(page, email) {
  const state = await load();
  const key =
    state.keys.find((entry) => entry.admin_backup_codes?.length) ||
    state.keys.find((entry) => !entry.needs_consent && (entry.refresh_token || entry.private_key));
  if (!key) {
    warn("Google asked for backup codes, but no authorized key is available to mint them");
    return false;
  }
  try {
    const codes = await adminApi.mintCodes(key, email);
    await mutate((draft) => {
      const row = draft.keys.find((entry) => entry.id === key.id);
      if (row) row.admin_backup_codes = codes;
    });
    await sendGoogleFill(
      page,
      {
        email,
        password: state.setup?.auto?.adminPassword || "",
        otp: state.totp?.secret || "",
        codes,
      },
      { tries: 5, interval: 300 }
    );
    ok(`${email}: minted ${codes.length} backup code(s) for the challenge`);
    return true;
  } catch (error) {
    warn(`could not mint backup codes: ${error.message || error}`);
    return false;
  }
}

export async function handleAuthMessage(message, page, { onSetup } = {}) {
  if (!message || typeof message !== "object") return;
  const type = message.type;

  if (type === "log" || type === "setup-report") {
    const text = String(message.text || "");
    if (!text) return;
    if (message.level === "error") warn(text);
    else if (message.level === "warn") warn(text);
    else if (message.level === "ok") ok(text);
    else info(text);
    return;
  }

  if (type === "identity-totp") {
    await storeTotp(message.email || "", message.secret || "", {
      enrolled: Boolean(message.enrolled),
      source: message.enrolled ? "enrolled" : "sign-in",
    });
    return;
  }

  if (type === "google-2sv" && message.enrolled) {
    const email = String(message.email || "").toLowerCase();
    const state = await load();
    const saved = state.totp.secret && (!state.totp.email || state.totp.email === email);
    if (!saved) {
      warn(
        `${email || "admin"}: 2SV is already on without a saved authenticator secret — ` +
          "policy automation is unsafe for this admin"
      );
      await mutate((draft) => {
        draft.setup.auto.policySafe = false;
      });
    } else {
      await mutate((draft) => {
        draft.setup.auto.policySafe = true;
      });
    }
    return;
  }

  if (type === "google-need-codes") {
    // Do not hold the page's binding call open while minting and pushing a new
    // google-fill back into the same document.
    void mintCodesAndResend(page, message.email || "");
    return;
  }

  if (type === "setup-admin" && message.email) {
    const email = String(message.email).toLowerCase();
    const domain = String(message.domain || "").toLowerCase();
    await mutate((state) => {
      state.setup.auto.adminEmail = email;
      if (domain) state.setup.auto.domain = domain;
    });
    info(`admin account: ${email}`);
    return;
  }

  if (type === "setup-captcha") {
    warn("Google wants a captcha — solve it in the browser; the CLI resumes after that");
    return;
  }

  if (type === "setup-domain" || type === "setup-domain-status" || type === "setup-domain-verified") {
    // The setup command owns these; other commands only need the log line.
    return;
  }

  if (typeof onSetup === "function") {
    await onSetup(message, page);
  }
}
