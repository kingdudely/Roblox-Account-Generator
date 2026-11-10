import { keyUsable } from "../state.js";

export const MAIN_STEP_IDS = ["domain", "signup", "duckdns", "setup", "accounts"];

export const MAIN_STEP_LABELS = {
  domain: "pick a DuckDNS subdomain",
  signup: "create the Cloud Identity tenant",
  duckdns: "create the DuckDNS record with the tenant admin",
  setup: "finish the tenant and build the OAuth key",
  accounts: "create Roblox account(s)",
};

export const SETUP_LABELS = [
  ["tenant.signup", "Tenant created"],
  ["tenant.verify", "Domain verified"],
  ["tenant.cloud-identity", "Cloud Identity Free"],
  ["twostep.enroll", "Admin 2SV enrolled"],
  ["twostep.policy", "2SV policy set"],
  ["key.project", "Cloud project"],
  ["key.admin-sdk", "Admin SDK enabled"],
  ["key.consent", "Consent screen"],
  ["key.scopes", "OAuth scopes"],
  ["key.client", "OAuth client"],
  ["key.authorize", "Key authorized"],
];

const FROM_ALIASES = {
  domain: "domain",
  start: "domain",
  signup: "signup",
  tenant: "signup",
  duckdns: "duckdns",
  dns: "duckdns",
  setup: "setup",
  key: "setup",
  accounts: "accounts",
  run: "accounts",
};

export function normaliseFrom(value) {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return "";
  if (FROM_ALIASES[text]) return FROM_ALIASES[text];
  if (SETUP_LABELS.some(([id]) => id === text)) return "setup";
  throw new Error(`unknown start step: ${value}`);
}

function firstSubdomain(state) {
  return String(
    state.duckdns?.subdomain || state.setup?.auto?.subdomain || ""
  )
    .trim()
    .toLowerCase();
}

function tokenOf(state) {
  return String(state.duckdns?.token || state.setup?.auto?.token || "").trim();
}

function tenantExists(state) {
  return Boolean(
    state.setup?.done?.["tenant.signup"] ||
      state.setup?.done?.["tenant.verify"] ||
      (state.keys || []).length
  );
}

export function usableKey(state) {
  return (state.keys || []).find((key) => keyUsable(key)) || null;
}

export function buildPlan(
  state,
  { accounts = 1, tenantOnly = false, from = "", fromProvided = false } = {}
) {
  const subdomain = firstSubdomain(state);
  const token = tokenOf(state);
  const key = usableKey(state);
  const needs = {
    domain: !subdomain,
    signup: !tenantExists(state),
    duckdns: !token || !subdomain,
    setup: !key,
    accounts: Number(accounts) > 0 && !tenantOnly,
  };

  const plan = [];
  for (const id of MAIN_STEP_IDS) {
    if (!needs[id]) continue;
    plan.push({ id, label: MAIN_STEP_LABELS[id] });
  }

  const startAt = normaliseFrom(from);
  if (fromProvided && startAt) {
    const index = MAIN_STEP_IDS.indexOf(startAt);
    const forced = [];
    for (const id of MAIN_STEP_IDS.slice(index)) {
      if (id === "accounts" && (tenantOnly || Number(accounts) <= 0)) continue;
      const existing = plan.find((step) => step.id === id);
      forced.push(existing || { id, label: MAIN_STEP_LABELS[id], forced: true });
    }
    return forced;
  }

  return plan;
}

export function nextCommand(state) {
  const key = usableKey(state);
  const live = (state.accounts || []).filter((account) => account.status === "live").length;
  if (key) {
    return live ? `rac run --count ${Math.min(50, live + 1)}` : "rac run --count 1";
  }
  return "npm run create-tenant";
}

export function statusModel(state) {
  const subdomain = firstSubdomain(state);
  const token = tokenOf(state);
  const key = usableKey(state);
  const tenant = tenantExists(state);
  const live = (state.accounts || []).filter((account) => account.status === "live").length;
  const pending = (state.accounts || []).filter((account) => account.status === "pending").length;
  const done = state.setup?.done || {};

  const rows = [
    {
      id: "domain",
      label: "DuckDNS domain",
      done: Boolean(subdomain),
      detail: subdomain ? `${subdomain}.duckdns.org` : "not chosen",
    },
    {
      id: "signup",
      label: "Cloud Identity tenant",
      done: tenant,
      detail: tenant
        ? state.setup?.auto?.adminEmail || (subdomain ? `admin@${subdomain}.duckdns.org` : "created")
        : "not created",
    },
    {
      id: "duckdns",
      label: "DuckDNS record + token",
      done: Boolean(subdomain && token),
      detail: token ? `token ${token.slice(0, 8)}…` : "missing",
    },
    {
      id: "setup",
      label: "OAuth key",
      done: Boolean(key),
      detail: key
        ? key.label || key.id
        : (state.keys || []).length
          ? "needs authorize"
          : "not ready",
    },
    {
      id: "accounts",
      label: "Roblox accounts",
      done: live > 0,
      detail: `${live} live${pending ? `, ${pending} pending` : ""}`,
    },
  ];

  const phases = SETUP_LABELS.map(([id, label]) => ({
    id,
    label,
    done: Boolean(done[id] || (id === "tenant.signup" && tenant) || (id === "key.authorize" && key)),
  }));

  return {
    subdomain,
    domain: subdomain ? `${subdomain}.duckdns.org` : "",
    admin: state.setup?.auto?.adminEmail || (subdomain ? `admin@${subdomain}.duckdns.org` : ""),
    rows,
    phases,
    accounts: { live, pending, total: (state.accounts || []).length },
    key,
    next: nextCommand(state),
  };
}

export function setupProgress(state) {
  const done = state.setup?.done || {};
  return {
    done: SETUP_LABELS.filter(([id]) => done[id]).length,
    total: SETUP_LABELS.length,
  };
}
