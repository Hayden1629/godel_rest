function fmt(ts) {
  return new Date(ts).toLocaleString();
}

chrome.storage.local.get("status").then(({ status }) => {
  const el = document.getElementById("status");
  if (!status) return;
  const expTxt = status.exp
    ? `expires ${fmt(status.exp * 1000)}`
    : "exp unknown";
  const lines = [
    `<div class="row">Captured: ${fmt(status.capturedAt)}</div>`,
    `<div class="row muted">${expTxt}</div>`,
  ];
  if (status.delivered) {
    lines.push(`<div class="row ok">Delivered to local poller ✓</div>`);
  } else {
    lines.push(
      `<div class="row bad">Not delivered: ${status.error || "unknown"}</div>`,
      `<div class="row muted">Start the receiver: <code>python -m server.token_server</code></div>`
    );
  }
  el.innerHTML = lines.join("");
});
