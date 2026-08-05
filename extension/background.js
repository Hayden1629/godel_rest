// Service worker: dedupes tokens and POSTs them to the local Python receiver.
const RECEIVER = "http://127.0.0.1:8765/token";
const OBSERVE = "http://127.0.0.1:8765/observe";

let lastToken = null;

function decodeExp(jwt) {
  try {
    const payload = JSON.parse(
      atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))
    );
    return payload.exp || null;
  } catch (_) {
    return null;
  }
}

async function relay(token) {
  if (!token || token === lastToken) return;
  lastToken = token;
  const exp = decodeExp(token);
  const status = {
    capturedAt: Date.now(),
    exp,
    delivered: false,
    error: null,
  };
  try {
    const res = await fetch(RECEIVER, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token, exp }),
    });
    status.delivered = res.ok;
    if (!res.ok) status.error = "receiver HTTP " + res.status;
  } catch (e) {
    status.error = "receiver unreachable (" + e.message + ")";
  }
  await chrome.storage.local.set({ status });
  console.log("[GodelTokenRelay] token relayed", status);
}

async function observe(msg) {
  try {
    await fetch(OBSERVE, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: msg.command,
        method: msg.method,
        path: msg.path,
        query: msg.query,
        status: msg.status,
        example: msg.example,
        sample: msg.sample,
      }),
    });
  } catch (_) {
    // receiver not running; ignore
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg) return;
  if (msg.type === "TOKEN") relay(msg.token);
  else if (msg.type === "ENDPOINT") observe(msg);
});
