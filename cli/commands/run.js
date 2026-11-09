import { parseArgs } from "node:util";
import * as admin from "../../extension/lib/admin.js";
import { uid, withTimeout } from "../../extension/lib/api.js";
import { makeIdentityFields } from "../../extension/lib/names.js";
import { createLaneApi, installLaneApi } from "../lane-api.js";
import { resolveKeyArg } from "../key-flow.js";
import { sendGoogleFill } from "../content-bridge.js";
import { load, mutate, requireKey } from "../state.js";
import { exportAccounts } from "./export.js";
import { info, ok, warn } from "../util.js";

const HELP = `rac run [options]

Creates Google identities with the Admin SDK, then runs the Roblox Google-SSO
lane in Patchright. Each lane gets its own persistent Chrome profile and cookie
jar. The lane uses the extension's existing Google/Roblox automation code.

Options:
  --key <id>           key to use (default: first usable key)
  --count <n>          identities to make (default: 1, max: 50)
  --lanes <n>          lane profiles to reuse (default: 1, max: 4)
  --reuse              reuse an identity with status created/failed (default)
  --new                always create a new identity
  --first <name>       override the identity given name
  --last <name>        override the identity family name
  --email <address>    override the identity email (count must be 1)
  --password <value>   override the identity password
  --username <name>    requested Roblox username (default: generated)
  --birthday <date>    Roblox birthday YYYY-MM-DD (default: settings/1996-05-15)
  --gender <n>         Roblox gender 1/2/3 (default: settings/1)
  --timeout <seconds>  per-identity budget (default: settings/240)
  --no-prime           skip the direct Google sign-in / authenticator prime
  --headless           run lanes headless (headful is recommended)
  --channel <name>     Chrome channel (default: chrome)
  --proxy <url>        override the proxy for every lane
  --keep-google        keep the Google identity after a successful signup
  --keep-open          keep the lane browsers open after the run
  --verbose            print page console output
  --help               show this help
`;

export const summary = "create identities and run Roblox Google-SSO lanes";

async function saveIdentity(identity) {
  await mutate((state) => {
    const index = state.identities.findIndex((entry) => entry.id === identity.id);
    if (index >= 0) state.identities[index] = { ...state.identities[index], ...identity };
    else state.identities.push(identity);
  });
}

async function makeIdentity({ state, key, domain, values }) {
  if (!values.new) {
    const existing = state.identities.find(
      (entry) =>
        ["created", "failed"].includes(entry.status) &&
        (!values.email || entry.email.toLowerCase() === values.email.toLowerCase())
    );
    if (existing) return existing;
  }

  const generated = makeIdentityFields(domain, state.identities.length + 1);
  const first = values.first || generated.first;
  const last = values.last || generated.last;
  const email = String(
    values.email || `${first}.${last}${state.identities.length + 1}@${domain}`
  ).toLowerCase();
  const identity = {
    id: uid("id"),
    key_id: key.id,
    email,
    first,
    last,
    password: values.password || generated.password,
    domain,
    status: "created",
    created_at: new Date().toISOString(),
  };
  await admin.insertUser(key, identity);
  await saveIdentity(identity);
  ok(`${email} created`);
  return identity;
}

async function ensureSecondFactor(identity, key) {
  if (identity.otp_secret || (identity.codes && identity.codes.length)) return;
  try {
    const codes = await admin.mintCodes(key, identity.email);
    identity.codes = codes;
    await saveIdentity(identity);
    ok(`${identity.email}: ${codes.length} backup code(s) minted`);
  } catch (error) {
    warn(
      `${identity.email}: could not mint backup codes (${error.message || error}) — ` +
        "the lane will try to enrol the authenticator anyway"
    );
  }
}

async function runLane(identity, laneIndex, storeId, laneModule, ctx) {
  const { key, values, settings, stop } = ctx;
  const otpFor = () => identity.otp_secret || "";
  const enrolledFor = () => Boolean(identity.totp_enrolled);

  let provisional = null;
  const result = await withTimeout(
    laneModule.runIdentity({
      identity,
      lane: laneIndex,
      storeId,
      timeout: Number(values.timeout) || Number(settings.timeout) || 240,
      shouldStop: () => stop.requested,
      log: (text) => info(text),
      otpFor,
      enrolledFor,
      username: values.username || "",
      birthday: values.birthday || settings.birthday || "1996-05-15",
      gender: Number(values.gender) || Number(settings.gender) || 1,
      acceptTerms: settings.acceptTerms !== false,
      laneSurface: "tab",
      primeGoogle: !values["no-prime"],
      probeIp: Boolean(settings.proxyUrl || settings.proxyList),
      onSession: async (cookie) => {
        provisional = {
          id: uid("acc"),
          identity_id: identity.id,
          key_id: key.id,
          username: null,
          password: identity.password,
          email: identity.email,
          user_id: null,
          display_name: null,
          cookie,
          status: "pending",
          birthday: values.birthday || settings.birthday || "1996-05-15",
          backup_codes: identity.codes || [],
          otp_secret: identity.otp_secret || null,
          store_id: storeId,
          created_at: new Date().toISOString(),
        };
        identity.cookie = cookie;
        identity.status = "signed-in";
        await saveIdentity(identity);
        await mutate((draft) => {
          const index = draft.accounts.findIndex((row) => row.identity_id === identity.id);
          if (index >= 0) draft.accounts[index] = provisional;
          else draft.accounts.push(provisional);
        });
        ok(`${identity.email}: Roblox cookie captured and saved`);
      },
    }),
    (Number(values.timeout) || Number(settings.timeout) || 240) * 1000 + 60_000,
    "identity"
  );

  const live = result.session?.status === 200;
  const account = {
    id: provisional?.id || uid("acc"),
    identity_id: identity.id,
    key_id: key.id,
    username: result.session?.name || result.username || null,
    password: identity.password,
    email: identity.email,
    user_id: result.session?.id || null,
    display_name: result.session?.name || null,
    cookie: result.cookie,
    status: live ? "live" : "pending",
    birthday: values.birthday || settings.birthday || "1996-05-15",
    backup_codes: identity.codes || [],
    otp_secret: identity.otp_secret || null,
    google_deleted: Boolean(identity.google_deleted),
    store_id: storeId,
    created_at: provisional?.created_at || new Date().toISOString(),
  };

  identity.status = live ? "account" : "failed";
  identity.error = live ? null : "pending — finish the Roblox finalise in the tab, then retry";
  identity.account_id = account.id;
  identity.roblox_username = live ? account.username : identity.roblox_username || null;
  await saveIdentity(identity);

  await mutate((draft) => {
    const index = draft.accounts.findIndex((row) => row.identity_id === identity.id);
    if (index >= 0) draft.accounts[index] = account;
    else draft.accounts.push(account);
  });

  if (live) {
    ok(`${account.username || identity.email} — Roblox account saved`);
    if (settings.deleteGoogleAfterSuccess !== false) {
      try {
        await admin.deleteUser(key, identity.email);
        identity.google_deleted = true;
        identity.google_deleted_at = new Date().toISOString();
        account.google_deleted = true;
        await saveIdentity(identity);
        await mutate((draft) => {
          const row = draft.accounts.find((entry) => entry.identity_id === identity.id);
          if (row) row.google_deleted = true;
        });
        ok(`${identity.email}: Google identity deleted; Cloud Identity seat freed`);
      } catch (error) {
        warn(`${identity.email}: could not delete the Google identity: ${error.message || error}`);
      }
    }
  } else {
    warn(`${identity.email}: cookie captured, but finalise is pending`);
  }
  return { ...result, account, live };
}

export async function run(argv) {
  if (argv.includes("--help") || argv.includes("-h")) {
    console.log(HELP);
    return 0;
  }
  const { values } = parseArgs({
    args: argv,
    options: {
      key: { type: "string" },
      count: { type: "string", default: "1" },
      lanes: { type: "string", default: "1" },
      reuse: { type: "boolean", default: true },
      new: { type: "boolean", default: false },
      first: { type: "string" },
      last: { type: "string" },
      email: { type: "string" },
      password: { type: "string" },
      username: { type: "string" },
      birthday: { type: "string" },
      gender: { type: "string" },
      timeout: { type: "string" },
      "no-prime": { type: "boolean", default: false },
      headless: { type: "boolean", default: false },
      channel: { type: "string", default: "chrome" },
      proxy: { type: "string" },
      "keep-google": { type: "boolean", default: false },
      "keep-open": { type: "boolean", default: false },
      verbose: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  let state = await load();
  const key = values.key ? await requireKey(values.key) : await resolveKeyArg([], { usable: true });
  if (key.needs_consent) {
    throw new Error(`${key.label || key.id} needs authorization first: rac key authorize ${key.id}`);
  }
  const domain = String(key.domain || "").trim().toLowerCase() || (await admin.detectDomain(key));
  if (!domain) throw new Error("could not detect the tenant domain — run `rac key check` first");

  const count = Math.max(1, Math.min(50, Number(values.count) || 1));
  const lanes = Math.max(1, Math.min(4, Number(values.lanes) || 1));
  if (values.email && count !== 1) throw new Error("--email only works with --count 1");
  const settings = {
    ...state.settings,
    ...(values.proxy ? { proxyUrl: values.proxy, proxyList: "" } : {}),
    ...(values["keep-google"] ? { deleteGoogleAfterSuccess: false } : {}),
  };

  info(`key:    ${key.label || key.id}`);
  info(`domain: ${domain}`);
  info(`count:  ${count}, lanes: ${lanes}`);
  info(`mode:   ${values["no-prime"] ? "SSO only" : "Google prime + SSO"}${values.headless ? " (headless)" : ""}`);

  const identities = [];
  for (let index = 0; index < count; index += 1) {
    state = await load();
    const identity = await makeIdentity({ state, key, domain, values });
    await ensureSecondFactor(identity, key);
    identities.push(identity);
  }

  const laneIdentities = new Map();
  const laneApi = await createLaneApi({
    headless: values.headless,
    channel: values.channel,
    verbose: values.verbose,
    settings,
    onLog: (text) => info(text),
    onMessage: async (message, page, lane) => {
      const identity = laneIdentities.get(lane.storeId);
      if (!identity || !message || typeof message !== "object") return;
      if (message.type === "log" || message.type === "setup-report") {
        const text = String(message.text || "");
        if (!text) return;
        if (message.level === "error" || message.level === "warn") warn(text);
        else if (message.level === "ok") ok(text);
        else info(text);
        return;
      }
      if (message.type === "identity-totp" && message.secret) {
        identity.otp_secret = message.secret;
        identity.totp_enrolled = Boolean(message.enrolled) || Boolean(identity.totp_enrolled);
        await saveIdentity(identity);
        ok(`${identity.email}: authenticator secret captured${message.enrolled ? " and accepted" : ""}`);
        return;
      }
      if (message.type === "google-2sv" && message.enrolled) {
        if (message.secret && state.totp?.secret) {
          identity.otp_secret = state.totp.secret;
          await saveIdentity(identity);
        } else if (!message.secret) {
          identity.noSecondFactor =
            "Google already has 2SV on this account and no authenticator secret is saved";
          warn(`${identity.email}: ${identity.noSecondFactor}`);
        }
        return;
      }
      if (message.type === "google-need-codes") {
        void (async () => {
          try {
            const codes = await admin.mintCodes(key, identity.email);
            identity.codes = codes;
            await saveIdentity(identity);
            await sendGoogleFill(
              page,
              {
                email: identity.email,
                password: identity.password,
                otp: identity.otp_secret || "",
                codes,
              },
              { tries: 4, interval: 250 }
            );
            ok(`${identity.email}: minted ${codes.length} backup code(s) for the challenge`);
          } catch (error) {
            identity.noSecondFactor = `could not mint backup codes: ${error.message || error}`;
            warn(`${identity.email}: ${identity.noSecondFactor}`);
          }
        })();
        return;
      }
    },
  });

  installLaneApi(laneApi.api);
  const laneModule = await import("../../extension/lib/lane.js");

  const stop = { requested: false };
  const onSigint = () => {
    stop.requested = true;
    warn("stopping after the current identity (press Ctrl+C again to force quit)");
    process.once("SIGINT", () => process.exit(130));
  };
  process.on("SIGINT", onSigint);

  const ctx = { key, state, values, settings, stop };
  let made = 0;
  let pending = 0;

  try {
    for (let index = 0; index < identities.length; index += 1) {
      if (stop.requested) break;
      const identity = identities[index];
      const laneIndex = index % lanes;
      const container = await laneModule.ensureContainer(laneIndex);
      const storeId = container.storeId;
      laneIdentities.set(storeId, identity);
      info(`identity ${index + 1}/${identities.length}: ${identity.email} on lane ${laneIndex + 1}`);

      try {
        const result = await runLane(identity, laneIndex, storeId, laneModule, {
          ...ctx,
          state,
        });
        if (result.live) made += 1;
        else pending += 1;
      } catch (error) {
        identity.status = "failed";
        identity.error = String(error.message || error).slice(0, 240);
        await saveIdentity(identity);
        warn(`${identity.email}: ${identity.error}`);
      }
      state = await load();
    }

    ok(`finished: ${made} live, ${pending} pending, ${Math.max(0, identities.length - made - pending)} failed`);
    if (pending > 0 && !values["keep-open"]) {
      warn("pending lanes were closed — rerun with --keep-open to finish them in the browser");
    }
    if (settings.autoExport !== false) {
      await exportAccounts().catch(() => {});
    }

    if (values["keep-open"]) {
      console.log("lane browsers are open — press Ctrl+C to close");
      await new Promise((resolve) => {
        process.once("SIGINT", resolve);
        process.once("SIGTERM", resolve);
      });
    }
    return made > 0 ? 0 : pending > 0 ? 2 : 1;
  } finally {
    process.removeListener("SIGINT", onSigint);
    await laneApi.close().catch(() => {});
  }
}
