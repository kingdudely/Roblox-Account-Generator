// language: JavaScript, file: lib/cdp-setup.js
// Chrome-only signup driver. It reads the page with Runtime.evaluate and then
// types/clicks through Input.* so every event is trusted by the browser.

import { sleep } from "./api.js";
import {
  clickButton,
  clickInput,
  clickLabelText,
  listInputs,
  pageInfo,
  typeInput,
} from "./cdp.js";

let currentUrl = "";
let lastClickKey = "";
let lastClickAt = 0;

export function resetSignupDriver() {
  currentUrl = "";
  lastClickKey = "";
  lastClickAt = 0;
}

function canClick(key, wait = 5500) {
  const now = Date.now();
  if (lastClickKey === key && now - lastClickAt < wait) return false;
  lastClickKey = key;
  lastClickAt = now;
  return true;
}

async function resetForUrl(url) {
  if (url !== currentUrl) {
    currentUrl = url;
    lastClickKey = "";
    lastClickAt = 0;
  }
}

function textMatch(input, pattern) {
  return pattern.test(
    `${input.aria} ${input.name} ${input.id} ${input.placeholder}`.toLowerCase()
  );
}

async function next(tabId, labels = ["next"], key = "next") {
  if (!canClick(key)) return { wait: true };
  await sleep(700 + Math.random() * 600);
  const clicked = await clickButton(tabId, labels);
  if (!clicked) return { wait: true };
  await sleep(1800 + Math.random() * 1800);
  return { clicked: key };
}

async function businessPage(tabId, profile, log) {
  const inputs = await listInputs(tabId);
  const business =
    inputs.find((input) => textMatch(input, /business/)) ||
    inputs.find((input) => input.type === "text");
  if (business && business.value !== profile.businessName) {
    log("cdp: typing Business name");
    await typeInput(tabId, business.i, profile.businessName);
  }
  const radios = await listInputs(tabId);
  const checked = radios.some((input) => input.type === "radio" && input.checked);
  if (!checked) {
    const radio = radios.find((input) => input.type === "radio");
    if (radio) await clickInput(tabId, radio.i);
    else await clickLabelText(tabId, ["just you", "one", "2 - 9"], "label,[role=radio],li");
    await sleep(250 + Math.random() * 250);
  }
  return next(tabId, ["next"], "business-next");
}

async function contactPage(tabId, profile, log) {
  let inputs = (await listInputs(tabId)).filter((input) => input.type === "text");
  if (inputs[0] && inputs[0].value !== profile.firstName) {
    await typeInput(tabId, inputs[0].i, profile.firstName);
  }
  inputs = (await listInputs(tabId)).filter((input) => input.type === "text");
  if (inputs[1] && inputs[1].value !== profile.lastName) {
    await typeInput(tabId, inputs[1].i, profile.lastName);
  }
  const email = (await listInputs(tabId)).find((input) => input.type === "email");
  if (email && email.value !== profile.recoveryEmail) {
    await typeInput(tabId, email.i, profile.recoveryEmail);
  }
  return next(tabId, ["next"], "contact-next");
}

async function domainPage(tabId, profile, log) {
  const input = (await listInputs(tabId)).find((item) => item.type === "text");
  if (input && input.value !== profile.domain) {
    log("cdp: typing domain");
    await typeInput(tabId, input.i, profile.domain);
  }
  return next(tabId, ["next"], "domain-next");
}

async function confirmPage(tabId) {
  return next(tabId, ["next"], "bringconfirm-next");
}

async function accountPage(tabId, profile, log) {
  const text = (await listInputs(tabId)).filter((input) => input.type === "text");
  if (text[0] && text[0].value !== profile.adminUser) {
    await typeInput(tabId, text[0].i, profile.adminUser);
  }

  let passwords = (await listInputs(tabId)).filter((input) => input.type === "password");
  if (passwords[0] && passwords[0].value !== profile.adminPassword) {
    await typeInput(tabId, passwords[0].i, profile.adminPassword);
  }
  passwords = (await listInputs(tabId)).filter((input) => input.type === "password");
  if (passwords[1] && passwords[1].value !== profile.adminPassword) {
    await typeInput(tabId, passwords[1].i, profile.adminPassword);
  }

  if (!canClick("account-create", 9000)) return { wait: true };
  // The username check runs while the button is disabled. Give it time before
  // pressing Agree so a rejected username can settle.
  await sleep(2500 + Math.random() * 2200);
  const clicked = await clickButton(tabId, ["agree and continue", "agree & continue"]);
  if (!clicked) return { wait: true };
  await sleep(2400 + Math.random() * 2200);
  return { clicked: "account-create" };
}

async function welcomePage(tabId) {
  return next(tabId, ["get started", "next", "continue"], "welcome-next");
}

export async function driveSignupTick(tabId, profile, log) {
  try {
    const info = await pageInfo(tabId);
    if (!info) return { wait: true };
    await resetForUrl(info.url);

    if (!/^https:\/\/workspace\.google\.com\/gcpidentity\/signup/i.test(info.url)) {
      return { done: true, url: info.url };
    }

    const body = String(info.text || "").toLowerCase();
    if (/can.t complete your signup|keep spammers|refuse to create domains/i.test(body)) {
      return { error: "Google refused the signup: can't complete your signup", url: info.url };
    }
    if (/creating your account|please wait/i.test(body)) {
      return { wait: true, url: info.url };
    }

    const path = new URL(info.url).pathname.replace(/\/+$/, "");
    if (/\/gcpidentity\/signup\/contact/i.test(path)) {
      return contactPage(tabId, profile, log);
    }
    if (/\/gcpidentity\/signup\/bringconfirm/i.test(path)) {
      return confirmPage(tabId);
    }
    if (/\/gcpidentity\/signup\/bring/i.test(path)) {
      return domainPage(tabId, profile, log);
    }
    if (/\/gcpidentity\/signup\/account/i.test(path)) {
      return accountPage(tabId, profile, log);
    }
    if (/\/gcpidentity\/signup\/welcome/i.test(path)) {
      return welcomePage(tabId);
    }
    if (/\/gcpidentity\/signup$/i.test(path)) {
      return businessPage(tabId, profile, log);
    }

    return welcomePage(tabId);
  } catch (error) {
    // A navigation between pages destroys the execution context. That is a
    // normal part of the flow, not a reason to abandon trusted input.
    return { wait: true, transient: String(error.message || error) };
  }
}
