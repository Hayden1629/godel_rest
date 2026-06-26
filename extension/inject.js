// MAIN-world hook. Runs in the page's JS context so it can read the real
// Authorization header the app attaches to api.godelterminal.com requests.
// It does NOT modify any request — observe only — then hands the token to the
// ISOLATED-world content script via window.postMessage.
(() => {
  const API_HOST = "api.godelterminal.com";
  let lastSeen = null;

  function relay(token) {
    if (!token || token === lastSeen) return;
    lastSeen = token;
    window.postMessage({ source: "godel-token-relay", token }, "*");
  }

  // Endpoint discovery: report each distinct api path the app calls so we can
  // reverse-engineer new commands (e.g. the current breaking-news endpoint).
  const seenEndpoints = new Set();
  function relayEndpoint(method, url) {
    try {
      if (!url || !url.includes(API_HOST)) return;
      const u = new URL(url, location.href);
      const key = method + " " + u.pathname;
      if (seenEndpoints.has(key)) return;
      seenEndpoints.add(key);
      window.postMessage(
        {
          source: "godel-endpoint",
          method,
          path: u.pathname,
          query: u.search,
          example: u.href,
        },
        "*"
      );
    } catch (_) {}
  }

  function authFromHeaders(headers) {
    // headers may be a Headers instance, a plain object, or an array of pairs
    if (!headers) return null;
    try {
      if (headers instanceof Headers) {
        return headers.get("Authorization") || headers.get("authorization");
      }
      if (Array.isArray(headers)) {
        for (const [k, v] of headers) {
          if (String(k).toLowerCase() === "authorization") return v;
        }
        return null;
      }
      for (const k of Object.keys(headers)) {
        if (k.toLowerCase() === "authorization") return headers[k];
      }
    } catch (_) {}
    return null;
  }

  function tokenFromAuth(auth) {
    if (!auth) return null;
    const m = /^Bearer\s+(.+)$/i.exec(String(auth).trim());
    return m ? m[1] : null;
  }

  // ── Patch fetch ────────────────────────────────────────────────────────────
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      if (url.includes(API_HOST)) {
        const method =
          (init && init.method) ||
          (input instanceof Request && input.method) ||
          "GET";
        relayEndpoint(String(method).toUpperCase(), url);
        let auth = authFromHeaders(init && init.headers);
        if (!auth && input instanceof Request) auth = authFromHeaders(input.headers);
        const tok = tokenFromAuth(auth);
        if (tok) relay(tok);
      }
    } catch (_) {}
    return origFetch.apply(this, arguments);
  };

  // ── Patch XMLHttpRequest ─────────────────────────────────────────────────────
  const origOpen = XMLHttpRequest.prototype.open;
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__godelUrl = url;
    relayEndpoint(String(method || "GET").toUpperCase(), url);
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    try {
      if (
        String(name).toLowerCase() === "authorization" &&
        this.__godelUrl &&
        String(this.__godelUrl).includes(API_HOST)
      ) {
        const tok = tokenFromAuth(value);
        if (tok) relay(tok);
      }
    } catch (_) {}
    return origSetHeader.apply(this, arguments);
  };

  console.log("[GodelTokenRelay] inject active");
})();
