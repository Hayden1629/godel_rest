// Shared by Chrome and Firefox (Firefox exposes the promise-based chrome.* API).
// Built with DOM nodes, not innerHTML: status.error carries network error text.
const ORIGINS = ["https://app.godelterminal.com/*", "http://127.0.0.1:8765/*"];

function fmt(ts) {
  return new Date(ts).toLocaleString();
}

function row(text, cls = "") {
  const div = document.createElement("div");
  div.className = ("row " + cls).trim();
  div.textContent = text;
  return div;
}

// Firefox lets the user withhold MV3 host permissions; without them the hook
// never injects into the app and nothing relays, so surface it here.
async function checkAccess() {
  const el = document.getElementById("access");
  const granted = await chrome.permissions.contains({ origins: ORIGINS });
  if (granted) return;
  const button = document.createElement("button");
  button.textContent = "Grant access";
  button.addEventListener("click", async () => {
    const ok = await chrome.permissions.request({ origins: ORIGINS });
    el.replaceChildren(
      ok
        ? row("Access granted. Reload the Gödel tab.", "ok")
        : row("Still not granted.", "bad")
    );
  });
  el.replaceChildren(row("Site access not granted — the hook can't run.", "bad"), button);
}

chrome.storage.local.get("status").then(({ status }) => {
  if (!status) return;
  const expTxt = status.exp ? `expires ${fmt(status.exp * 1000)}` : "exp unknown";
  const rows = [row(`Captured: ${fmt(status.capturedAt)}`), row(expTxt, "muted")];
  if (status.delivered) {
    rows.push(row("Delivered to local poller ✓", "ok"));
  } else {
    rows.push(
      row(`Not delivered: ${status.error || "unknown"}`, "bad"),
      row("Start the receiver: ./services.sh start", "muted")
    );
  }
  const el = document.getElementById("status");
  el.className = "";
  el.replaceChildren(...rows);
});

checkAccess();
