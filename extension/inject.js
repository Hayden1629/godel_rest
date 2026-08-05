// MAIN-world hook. Runs in the page's JS context so it can read the real
// Authorization header the app attaches to api.godelterminal.com requests.
// It does NOT modify any request — observe only — then hands what it sees to
// the ISOLATED-world content script via window.postMessage.
//
// Beyond the token, it reports, for each distinct API call:
//   - HTTP status (so a 404 like the retired /consolidated_financials route is
//     visibly different from a working 200),
//   - the terminal command in effect (correlated from /api/v1/command-action),
//   - a truncated response sample (so a new endpoint's shape is captured too).
// That's what lets you type a command in the terminal and see which endpoint
// (and payload) answers it.
(() => {
  const API_HOST = "api.godelterminal.com";
  let lastSeen = null;

  function relay(token) {
    if (!token || token === lastSeen) return;
    lastSeen = token;
    window.postMessage({ source: "godel-token-relay", token }, "*");
  }

  // ── command correlation ─────────────────────────────────────────────────────
  // The app POSTs /api/v1/command-action with {command:"..."} as commands run.
  // Remember the most recent one so calls that follow can be tagged with it.
  let currentCommand = null; // { name, ts }
  function noteCommandAction(bodyText) {
    try {
      const b = JSON.parse(bodyText);
      if (b && b.command) currentCommand = { name: b.command, ts: Date.now() };
    } catch (_) {}
  }
  function commandContext() {
    if (!currentCommand) return null;
    // only attribute calls within a few seconds of the command action
    if (Date.now() - currentCommand.ts > 8000) return null;
    return currentCommand.name;
  }

  // Endpoint discovery: report each distinct (command, method, path) once, with
  // status + a response sample. Keyed so the same path under two commands is
  // still captured separately.
  const seenEndpoints = new Set();
  function relayEndpoint(method, url, status, sample) {
    try {
      if (!url || !url.includes(API_HOST)) return;
      const u = new URL(url, location.href);
      const ctx = commandContext();
      const key = (ctx || "-") + " " + method + " " + u.pathname;
      if (seenEndpoints.has(key)) return;
      seenEndpoints.add(key);
      window.postMessage(
        {
          source: "godel-endpoint",
          command: ctx,
          method,
          path: u.pathname,
          query: u.search,
          status: status == null ? null : String(status),
          example: u.href,
          sample: sample ? String(sample).slice(0, 2000) : null,
        },
        "*"
      );
    } catch (_) {}
  }

  function authFromHeaders(headers) {
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

  function bodyToText(body) {
    if (body == null) return null;
    if (typeof body === "string") return body;
    try {
      return JSON.stringify(body);
    } catch (_) {
      return null;
    }
  }

  // ── Patch fetch ────────────────────────────────────────────────────────────
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    let url = "";
    let method = "GET";
    let isApi = false;
    try {
      url = typeof input === "string" ? input : (input && input.url) || "";
      isApi = url.includes(API_HOST);
      if (isApi) {
        method =
          (init && init.method) ||
          (input instanceof Request && input.method) ||
          "GET";
        method = String(method).toUpperCase();
        let auth = authFromHeaders(init && init.headers);
        if (!auth && input instanceof Request) auth = authFromHeaders(input.headers);
        const tok = tokenFromAuth(auth);
        if (tok) relay(tok);
        // capture command-action bodies to know which command is running
        if (/\/api\/v1\/command-action$/.test(url) && init && init.body) {
          noteCommandAction(bodyToText(init.body));
        }
      }
    } catch (_) {}

    const p = origFetch.apply(this, arguments);
    if (!isApi) return p;
    return p.then((res) => {
      try {
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("json")) {
          // clone so we don't consume the app's own reader
          res
            .clone()
            .text()
            .then((txt) => relayEndpoint(method, url, res.status, txt))
            .catch(() => relayEndpoint(method, url, res.status, null));
        } else {
          relayEndpoint(method, url, res.status, null);
        }
      } catch (_) {
        relayEndpoint(method, url, res.status, null);
      }
      return res;
    });
  };

  // ── Patch XMLHttpRequest ─────────────────────────────────────────────────────
  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__godelUrl = url;
    this.__godelMethod = String(method || "GET").toUpperCase();
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
  XMLHttpRequest.prototype.send = function (body) {
    try {
      const url = this.__godelUrl || "";
      if (String(url).includes(API_HOST)) {
        if (/\/api\/v1\/command-action$/.test(url) && body) {
          noteCommandAction(bodyToText(body));
        }
        this.addEventListener("loadend", () => {
          let sample = null;
          try {
            const ct = this.getResponseHeader("content-type") || "";
            if (ct.includes("json")) sample = this.responseText;
          } catch (_) {}
          relayEndpoint(this.__godelMethod, url, this.status, sample);
        });
      }
    } catch (_) {}
    return origSend.apply(this, arguments);
  };

  console.log("[GodelTokenRelay] inject active (status+command+sample capture)");
})();
