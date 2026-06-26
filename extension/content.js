// ISOLATED-world bridge. Receives tokens from the MAIN-world inject.js via
// window.postMessage and forwards them to the background service worker, which
// owns the network permission to reach the local poller.
window.addEventListener("message", (event) => {
  if (event.source !== window) return;
  const data = event.data;
  if (!data) return;
  if (data.source === "godel-token-relay" && data.token) {
    chrome.runtime.sendMessage({ type: "TOKEN", token: data.token });
  } else if (data.source === "godel-endpoint" && data.path) {
    chrome.runtime.sendMessage({
      type: "ENDPOINT",
      method: data.method,
      path: data.path,
      query: data.query,
      example: data.example,
    });
  }
});
