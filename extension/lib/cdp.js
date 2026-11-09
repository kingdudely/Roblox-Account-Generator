// language: JavaScript, file: lib/cdp.js
// Small Chrome DevTools Protocol wrapper for the automatic setup. It only runs
// in Chrome: the extension's debugger permission lets us send Input.* commands,
// which produce trusted key/mouse events that a content script cannot.

import { api, sleep } from "./api.js";

export function cdpAvailable() {
  return Boolean(api.debugger && typeof api.debugger.attach === "function");
}

export function attachCdp(tabId) {
  return new Promise((resolve, reject) => {
    try {
      api.debugger.attach({ tabId }, "1.3", () => {
        const error = api.runtime.lastError;
        if (!error) {
          resolve();
          return;
        }
        const text = String(error.message || error);
        // The tab may already be attached by this extension after a retry.
        if (/already attached/i.test(text)) resolve();
        else reject(new Error(text));
      });
    } catch (error) {
      reject(error);
    }
  });
}

export function detachCdp(tabId) {
  return new Promise((resolve) => {
    if (!cdpAvailable() || !tabId) {
      resolve();
      return;
    }
    try {
      api.debugger.detach({ tabId }, () => resolve());
    } catch (_) {
      resolve();
    }
  });
}

export function sendCdp(tabId, method, params = {}) {
  return new Promise((resolve, reject) => {
    try {
      api.debugger.sendCommand({ tabId }, method, params, (result) => {
        const error = api.runtime.lastError;
        if (error) reject(new Error(String(error.message || error)));
        else resolve(result);
      });
    } catch (error) {
      reject(error);
    }
  });
}

export async function evaluateCdp(tabId, expression) {
  const result = await sendCdp(tabId, "Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  if (result?.exceptionDetails) {
    throw new Error(
      result.exceptionDetails.exception?.description ||
        result.exceptionDetails.text ||
        "page evaluation failed"
    );
  }
  return result?.result?.value;
}

const VISIBLE = `(el) => {
  const rect = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  return rect.width > 1 && rect.height > 1 && style.visibility !== "hidden" && style.display !== "none";
}`;

export async function pageInfo(tabId) {
  return evaluateCdp(
    tabId,
    `(() => ({
      url: location.href,
      title: document.title,
      text: (document.body ? document.body.innerText : "").slice(0, 6000)
    }))()`
  );
}

export async function listInputs(tabId) {
  return evaluateCdp(
    tabId,
    `(() => {
      const visible = ${VISIBLE};
      return [...document.querySelectorAll("input,textarea")]
        .filter(visible)
        .map((el, i) => {
          const rect = el.getBoundingClientRect();
          return {
            i,
            type: String(el.type || "text").toLowerCase(),
            name: el.name || "",
            id: el.id || "",
            aria: el.getAttribute("aria-label") || "",
            placeholder: el.placeholder || "",
            value: el.value || "",
            checked: Boolean(el.checked),
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
            w: rect.width,
            h: rect.height
          };
        });
    })()`
  );
}

export async function listButtons(tabId) {
  return evaluateCdp(
    tabId,
    `(() => {
      const visible = ${VISIBLE};
      return [...document.querySelectorAll('button,[role="button"],a,input[type="submit"]')]
        .filter(visible)
        .map((el, i) => {
          const rect = el.getBoundingClientRect();
          return {
            i,
            label: ((el.getAttribute("aria-label") || "") + " " + (el.innerText || el.value || ""))
              .replace(/\\s+/g, " ").trim(),
            disabled: Boolean(el.disabled),
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2
          };
        });
    })()`
  );
}

export async function clickRect(tabId, rect) {
  if (!rect) return false;
  const x = Math.round(rect.x);
  const y = Math.round(rect.y);
  await sendCdp(tabId, "Input.dispatchMouseEvent", {
    type: "mouseMoved",
    x,
    y,
  });
  await sleep(90 + Math.random() * 140);
  await sendCdp(tabId, "Input.dispatchMouseEvent", {
    type: "mousePressed",
    x,
    y,
    button: "left",
    clickCount: 1,
  });
  await sleep(60 + Math.random() * 90);
  await sendCdp(tabId, "Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x,
    y,
    button: "left",
    clickCount: 1,
  });
  await sleep(120 + Math.random() * 160);
  return true;
}

export async function clickInput(tabId, index) {
  const rect = await evaluateCdp(
    tabId,
    `(() => {
      const visible = ${VISIBLE};
      const el = [...document.querySelectorAll("input,textarea")].filter(visible)[${Number(
        index
      )}];
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const rect = el.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    })()`
  );
  return clickRect(tabId, rect);
}

async function clearFocused(tabId) {
  await sendCdp(tabId, "Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "a",
    code: "KeyA",
    windowsVirtualKeyCode: 65,
    modifiers: 2,
  });
  await sendCdp(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "a",
    code: "KeyA",
    windowsVirtualKeyCode: 65,
    modifiers: 2,
  });
  await sendCdp(tabId, "Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "Backspace",
    code: "Backspace",
    windowsVirtualKeyCode: 8,
  });
  await sendCdp(tabId, "Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "Backspace",
    code: "Backspace",
    windowsVirtualKeyCode: 8,
  });
  await sleep(120);
}

export async function typeText(tabId, text, delay = 95) {
  const value = String(text ?? "");
  for (const char of value) {
    // input.insertText is handled by the browser input pipeline and fires a
    // trusted beforeinput/input pair. The extra key events keep the page's
    // keyboard listeners happy without inserting the character twice.
    await sendCdp(tabId, "Input.insertText", { text: char });
    await sendCdp(tabId, "Input.dispatchKeyEvent", {
      type: "keyDown",
      key: char,
      text: "",
    });
    await sendCdp(tabId, "Input.dispatchKeyEvent", {
      type: "keyUp",
      key: char,
      text: "",
    });
    await sleep(delay + Math.random() * 55);
  }
}

export async function typeInput(tabId, index, text) {
  await clickInput(tabId, index);
  await sleep(180 + Math.random() * 180);
  await clearFocused(tabId);
  await typeText(tabId, text);
  await sleep(250 + Math.random() * 250);
}

export async function clickButton(tabId, labels) {
  const wanted = labels.map((label) => label.toLowerCase());
  const rect = await evaluateCdp(
    tabId,
    `(() => {
      const visible = ${VISIBLE};
      const wanted = ${JSON.stringify(wanted)};
      const nodes = [...document.querySelectorAll('button,[role="button"],a,input[type="submit"]')]
        .filter(visible);
      const el = nodes.find((node) => {
        const label = ((node.getAttribute("aria-label") || "") + " " + (node.innerText || node.value || ""))
          .replace(/\\s+/g, " ").trim().toLowerCase();
        return wanted.some((want) => label === want || label.includes(want));
      });
      if (!el) return null;
      if (el.disabled) return { disabled: true };
      el.scrollIntoView({ block: "center" });
      const rect = el.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    })()`
  );
  if (!rect || rect.disabled) return null;
  await clickRect(tabId, rect);
  return true;
}

export async function clickLabelText(tabId, labels, scope = "label,[role=radio],button,li,a") {
  const wanted = labels.map((label) => label.toLowerCase());
  const rect = await evaluateCdp(
    tabId,
    `(() => {
      const visible = ${VISIBLE};
      const wanted = ${JSON.stringify(wanted)};
      const nodes = [...document.querySelectorAll(${JSON.stringify(scope)})].filter(visible);
      const el = nodes.find((node) => {
        const label = ((node.getAttribute("aria-label") || "") + " " + (node.innerText || ""))
          .replace(/\\s+/g, " ").trim().toLowerCase();
        return wanted.some((want) => label === want || label.includes(want));
      });
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const rect = el.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    })()`
  );
  return clickRect(tabId, rect);
}
