// language: JavaScript, file: content/awake.js
// Runs in the page's own world, at document_start, before Google's bundles.
//
// A background tab never gets animation frames, and Google's sign-in UI is an
// rAF-driven SPA — so the field fills, the click dispatches, and the transition
// that should follow it never runs. Rather than keep a window on screen for
// every lane, this makes the document believe it is the selected tab and gives
// the page a frame clock that does not depend on being painted.
//
// Two pieces matter:
//   1. requestAnimationFrame, rescheduled off a MessageChannel. Timers in a
//      background tab are clamped to one second; message ports are not.
//   2. visibilityState / hidden / hasFocus, which is what Google's own code
//      reads before it starts a transition.
//
// The shim reports itself on window.__racAwake so the runner can tell "hidden
// but advancing" from "hidden and stalled". Nothing here touches the document.

(() => {
  if (window.__racAwake) return;

  const FRAME_MS = 16; // ~60 fps, same as a painted tab
  const callbacks = new Map();
  let nextId = 1;
  let lastFrame = 0;
  let running = false;

  const record = { patched: false, frames: 0, startedAt: Date.now() };
  window.__racAwake = record;

  // Capture the real getters first: the runner logs what Firefox actually
  // thinks, next to what the shim is claiming.
  const realVisibility = Object.getOwnPropertyDescriptor(Document.prototype, "visibilityState");
  record.nativeVisibility = () => {
    try {
      return realVisibility && realVisibility.get ? realVisibility.get.call(document) : "unknown";
    } catch (_) {
      return "unknown";
    }
  };

  try {
    Object.defineProperty(Document.prototype, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    Object.defineProperty(Document.prototype, "hidden", {
      configurable: true,
      get: () => false,
    });
  } catch (_) {
    // A frozen property means the throttle still applies; the runner sees
    // patched:false and can fall back to a visible lane.
    return;
  }
  record.patched = true;

  try {
    Object.defineProperty(Document.prototype, "hasFocus", {
      configurable: true,
      writable: true,
      value: () => true,
    });
  } catch (_) {
    // cosmetic only
  }

  const channel = new MessageChannel();

  // Frames are only produced while something is waiting for one, so an idle
  // sign-in screen costs nothing. While callbacks are queued the port spins at
  // FRAME_MS, which is what the compositor would have done anyway.
  channel.port1.onmessage = () => {
    if (!callbacks.size) {
      running = false;
      return;
    }
    const now = performance.now();
    if (now - lastFrame >= FRAME_MS) {
      lastFrame = now;
      record.frames += 1;
      const batch = [...callbacks.values()];
      callbacks.clear();
      for (const callback of batch) {
        try {
          callback(now);
        } catch (error) {
          // Surface it the way an uncaught frame error would, without killing
          // the clock for every other callback.
          setTimeout(() => {
            throw error;
          });
        }
      }
    }
    if (callbacks.size) channel.port2.postMessage(0);
    else running = false;
  };

  function requestFrame(callback) {
    if (typeof callback !== "function") return 0;
    const id = nextId++;
    callbacks.set(id, callback);
    if (!running) {
      running = true;
      channel.port2.postMessage(0);
    }
    return id;
  }

  function cancelFrame(id) {
    callbacks.delete(id);
  }

  try {
    Object.defineProperty(window, "requestAnimationFrame", {
      configurable: true,
      writable: true,
      value: requestFrame,
    });
    Object.defineProperty(window, "cancelAnimationFrame", {
      configurable: true,
      writable: true,
      value: cancelFrame,
    });
  } catch (_) {
    record.patched = false;
    return;
  }

  // Anything already parked on the event re-reads the state and moves.
  queueMicrotask(() => {
    try {
      document.dispatchEvent(new Event("visibilitychange"));
    } catch (_) {
      // nothing waiting
    }
  });
})();
