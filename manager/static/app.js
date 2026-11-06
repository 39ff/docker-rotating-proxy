"use strict";
(function () {
  // ----------------------------------------------------------------------
  // Utilities
  // ----------------------------------------------------------------------
  const PROPS = new Set(["value", "checked", "disabled", "selected", "indeterminate"]);

  function storageGet(store, key) { try { return window[store].getItem(key); } catch (e) { return null; } }
  function storageSet(store, key, value) {
    try { if (value == null) window[store].removeItem(key); else window[store].setItem(key, value); } catch (e) { /* private mode */ }
  }

  let LANG = storageGet("localStorage", "rpx.lang") ||
    ((navigator.language || "en").toLowerCase().startsWith("ja") ? "ja" : "en");
  document.documentElement.lang = LANG;

  function t(key, vars) {
    const dict = window.I18N[LANG] || window.I18N.en;
    let s = dict[key];
    if (s == null) s = window.I18N.en[key];
    if (s == null) s = key;
    if (vars) for (const k of Object.keys(vars)) s = s.split("{" + k + "}").join(String(vars[k]));
    return s;
  }

  // Element builder. Text is always added as text nodes, never as HTML.
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    const props = {};
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v == null || v === false) continue;
        if (k === "class") el.className = v;
        else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
        else if (PROPS.has(k)) props[k] = v;
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    for (const [k, v] of Object.entries(props)) el[k] = v;
    return el;
  }

  async function api(method, path, body) {
    const opts = { method, headers: {}, credentials: "same-origin" };
    if (method !== "GET") {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["X-RPX-Request"] = "1";
      opts.body = JSON.stringify(body || {});
    }
    let res;
    try { res = await fetch(path, opts); } catch (e) { throw Object.assign(new Error(t("error.network")), { code: "network" }); }
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) {
      if (res.status === 401 && data.error === "login_required") { renderLogin(); }
      throw Object.assign(new Error(data.message || res.statusText), { code: data.error, status: res.status, data });
    }
    return data;
  }

  function toast(message, kind) {
    const el = h("div", { class: "toast" + (kind ? " " + kind : "") }, message);
    document.getElementById("toasts").append(el);
    setTimeout(() => el.remove(), kind === "bad" ? 7000 : 3500);
  }

  async function copyText(text, btn) {
    let ok = false;
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); ok = true; } catch (e) { ok = false; }
    }
    if (!ok) { // plain-http pages have no clipboard API
      const ta = h("textarea", { class: "hidden-copy" }, text);
      ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.append(ta); ta.select();
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      ta.remove();
    }
    if (btn) { const old = btn.textContent; btn.textContent = ok ? t("common.copied") : "✗"; setTimeout(() => { btn.textContent = old; }, 1500); }
  }

  function debounce(fn, ms) { let id; return (...a) => { clearTimeout(id); id = setTimeout(() => fn(...a), ms); }; }
  function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString(LANG === "ja" ? "ja-JP" : "en-US") : t("overview.never"); }
  function hostForUrl(host) { return host.includes(":") && !host.startsWith("[") ? "[" + host + "]" : host; }
  function isLoopbackOrGateway(ip) { return /^127\./.test(ip) || ip === "::1" || /^172\.16\.231\.1$/.test(ip); }
  const root = () => document.getElementById("app");

  function brandMark(cls) { return h("img", { class: "brand-mark " + (cls || ""), src: "/static/favicon.svg", alt: "" }); }

  function langButton() {
    return h("button", { class: "ghost lang", type: "button", onclick: () => {
      LANG = LANG === "ja" ? "en" : "ja";
      storageSet("localStorage", "rpx.lang", LANG);
      document.documentElement.lang = LANG;
      rerender();
    } }, LANG === "ja" ? "English" : "日本語");
  }

  let rerender = () => {};

  function kindLabel(kind) {
    return { public: "public", httpsquid: "httpsquid", http: "http", socks5: "socks5", https: "https" }[kind] || kind;
  }
  function kindBadge(kind) {
    const cls = { socks5: "info", http: "info", https: "info", httpsquid: "", public: "warn" }[kind] || "";
    return h("span", { class: "badge plain " + cls }, kindLabel(kind));
  }
  function healthBadge(s) {
    if (s.state !== "running") return h("span", { class: "badge bad" }, s.state || "?");
    if (s.health === "healthy" || s.health === "") return h("span", { class: "badge ok" }, s.health || "running");
    if (s.health === "starting") return h("span", { class: "badge warn" }, "starting");
    return h("span", { class: "badge bad" }, s.health);
  }

  // ----------------------------------------------------------------------
  // Shared pieces: upstream preview, snippets, job modal
  // ----------------------------------------------------------------------
  const SCHEME_OPTIONS = [["http", "up.type.http"], ["socks5", "up.type.socks5"], ["https", "up.type.https"],
    ["httpsquid", "up.type.httpsquid"], ["", "up.type.public"]];

  function schemeSelect(value, onchange) {
    return h("select", { onchange: (e) => onchange(e.target.value), value },
      SCHEME_OPTIONS.map(([v, key]) => h("option", { value: v, selected: v === value }, t(key))));
  }

  function previewTable(rows) {
    if (!rows || !rows.length) return null;
    const ok = rows.filter((r) => r.ok).length;
    return h("div", { class: "stack" },
      h("div", { class: "row between" }, h("h3", null, t("up.preview")),
        h("span", { class: "small muted" }, t("up.previewCount", { ok, bad: rows.length - ok }))),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, h("th", null, t("up.type")), h("th", null, t("up.host")),
          h("th", null, t("up.user")), h("th", null, t("up.static")), h("th", null, t("up.format")))),
        h("tbody", null, rows.slice(0, 200).map((r) => r.ok
          ? h("tr", null, h("td", null, kindBadge(r.kind)), h("td", null, h("span", { class: "mono" }, r.hostport)),
            h("td", null, r.user ? h("span", { class: "mono" }, r.user + (r.has_password ? ":•••" : "")) : h("span", { class: "muted" }, "—")),
            h("td", null, r.static ? "✓" : h("span", { class: "muted small" }, t("up.rotationOnly"))),
            h("td", { class: "small muted" }, r.format))
          : h("tr", null, h("td", null, h("span", { class: "badge bad" }, "✗")),
            h("td", { colspan: "3" }, h("span", { class: "mono" }, r.input)),
            h("td", { class: "small" }, h("span", { class: "muted" }, r.error))))))));
  }

  function upstreamAdder(opts) {
    // opts: {text, scheme, onChange(text, scheme, rows)}
    const st = { text: opts.text || "", scheme: opts.scheme == null ? "http" : opts.scheme, rows: opts.rows || null };
    const previewBox = h("div", null, previewTable(st.rows));
    const refresh = debounce(async () => {
      if (!st.text.trim()) { st.rows = null; previewBox.replaceChildren(); opts.onChange && opts.onChange(st); return; }
      try {
        const res = await api("POST", "/api/upstreams/preview", { text: st.text, default_scheme: st.scheme });
        st.rows = res.rows;
        previewBox.replaceChildren(previewTable(st.rows) || "");
      } catch (e) { previewBox.replaceChildren(h("div", { class: "callout bad" }, e.message)); }
      opts.onChange && opts.onChange(st);
    }, 300);
    const ta = h("textarea", { placeholder: t("up.placeholder"), spellcheck: "false", value: st.text,
      oninput: (e) => { st.text = e.target.value; refresh(); } });
    const el = h("div", null,
      h("div", { class: "field" }, h("label", null, t("up.paste")), ta),
      h("div", { class: "field" }, h("label", null, t("up.defaultType")),
        schemeSelect(st.scheme, (v) => { st.scheme = v; refresh(); })),
      previewBox);
    if (st.text && !st.rows) refresh();
    return { el, st };
  }

  function snippets(host, staticPort) {
    const hp = hostForUrl(host);
    const rot = "http://" + hp + ":3128";
    const sticky = staticPort ? "http://" + hp + ":" + staticPort : rot;
    const items = {
      curl: [t("snip.rotating"), "curl -x " + rot + " https://api.ipify.org"]
        .concat(staticPort ? [t("snip.sticky", { port: staticPort }), "curl -x " + sticky + " https://api.ipify.org"] : []).join("\n"),
      python: ["import requests", "", t("snip.rotating"), 'proxy = "' + rot + '"',
        'r = requests.get("https://api.ipify.org", proxies={"http": proxy, "https": proxy})', "print(r.text)"].join("\n"),
      playwright: ['import { chromium } from "playwright";', "",
        (staticPort ? t("snip.sticky", { port: staticPort }) : t("snip.rotating")).replace(/^#/, "//"),
        'const browser = await chromium.launch({ proxy: { server: "' + sticky + '" } });'].join("\n"),
      mcp: ["claude mcp add browser-sticky -- npx @playwright/mcp@latest --proxy-server " + sticky,
        "claude mcp add fetch-rotating -- uvx mcp-server-fetch --proxy-url " + rot].join("\n"),
    };
    const labels = { curl: "snip.curl", python: "snip.python", playwright: "snip.playwright", mcp: "snip.mcp" };
    let current = storageGet("localStorage", "rpx.snippet") || "curl";
    if (!items[current]) current = "curl";
    const pre = h("pre", { class: "code" });
    const copyBtn = h("button", { class: "small copy", type: "button", onclick: () => copyText(items[current], copyBtn) }, t("common.copy"));
    const tabs = h("div", { class: "tabs" });
    function draw() {
      pre.textContent = items[current];
      tabs.replaceChildren(...Object.keys(items).map((k) => h("button", { type: "button", class: k === current ? "active" : "",
        onclick: () => { current = k; storageSet("localStorage", "rpx.snippet", k); draw(); } }, t(labels[k]))));
    }
    draw();
    return h("div", null, tabs, h("div", { class: "code-box" }, pre, copyBtn));
  }

  function proxyHost(ov) { return (ov && ov.public_host) || location.hostname; }

  // Poll a job until it finishes. onUpdate(job) is called on every poll.
  async function followJob(job, onUpdate) {
    let current = job;
    onUpdate(current);
    while (current.status === "running") {
      await new Promise((r) => setTimeout(r, 1000));
      try { current = await api("GET", "/api/jobs/" + current.id); } catch (e) { if (e.code === "login_required") return current; continue; }
      onUpdate(current);
    }
    return current;
  }

  function jobStatusBadge(status) {
    const cls = { running: "warn", succeeded: "ok", failed: "bad" }[status] || "";
    return h("span", { class: "badge " + cls }, status === "running" ? h("span", { class: "spinner" }) : null, t("job." + status));
  }

  function openJobModal(job, titleKey, onDone) {
    const status = h("span");
    const pre = h("pre", { class: "code log" });
    const close = h("button", { type: "button", onclick: () => backdrop.remove() }, t("common.close"));
    const backdrop = h("div", { class: "modal-backdrop" }, h("div", { class: "card modal" },
      h("div", { class: "card-head" }, h("h2", null, t(titleKey)), status), pre, h("div", { class: "row end" }, close)));
    document.body.append(backdrop);
    followJob(job, (j) => {
      status.replaceChildren(jobStatusBadge(j.status));
      const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
      pre.textContent = j.log;
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    }).then((j) => { if (onDone) onDone(j); });
  }

  async function startApply(onDone) {
    let job;
    try { job = await api("POST", "/api/apply", {}); } catch (e) {
      if (e.code === "busy" && e.data && e.data.job) job = await api("GET", "/api/jobs/" + e.data.job);
      else { toast(e.message, "bad"); return; }
    }
    openJobModal(job, "job.apply", onDone);
  }

  // ----------------------------------------------------------------------
  // Boot
  // ----------------------------------------------------------------------
  let SESSION = null;

  async function boot() {
    try { SESSION = await api("GET", "/api/session"); } catch (e) {
      root().replaceChildren(h("div", { class: "auth-box" }, h("div", { class: "callout bad" }, e.message)));
      return;
    }
    if (SESSION.setup_required) return wizardStart();
    if (!SESSION.authenticated) return renderLogin();
    const step = parseInt(storageGet("sessionStorage", "rpx.wizard") || "0", 10);
    if (step >= 2 && step <= 4) { W.step = step; return renderWizard(); }
    appStart();
  }

  function renderLogin() {
    rerender = renderLogin;
    stopTimers();
    const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
    const err = h("div");
    const submit = h("button", { class: "primary", type: "submit" }, t("login.submit"));
    const form = h("form", { onsubmit: async (e) => {
      e.preventDefault(); submit.disabled = true; err.replaceChildren();
      try { await api("POST", "/api/login", { password: pw.value }); appStart(); }
      catch (ex) { err.replaceChildren(h("div", { class: "callout bad" }, ex.message)); submit.disabled = false; }
    } }, h("div", { class: "field" }, h("label", null, t("setup.password")), pw), err, h("div", { class: "row end" }, submit));
    root().replaceChildren(h("div", { class: "auth-box" },
      h("div", { class: "wizard-brand" }, brandMark(), h("h1", null, t("brand")), h("span", { class: "spacer" }), langButton()),
      h("div", { class: "card" }, h("h2", null, t("login.title")), h("p"), form)));
    pw.focus();
  }

  // ----------------------------------------------------------------------
  // First-run wizard
  // ----------------------------------------------------------------------
  const W = { step: 1, token: "", allowMine: null, allowMore: "", adder: { text: "", scheme: "http", rows: null }, vpnOpen: false };

  function wizardStart() {
    const params = new URLSearchParams(location.search);
    let token = params.get("token") || "";
    if (token) storageSet("sessionStorage", "rpx.token", token); else token = storageGet("sessionStorage", "rpx.token") || "";
    if (params.has("token")) history.replaceState(null, "", "/setup");
    W.token = token;
    W.step = 1;
    renderWizard();
  }

  function setStep(n) {
    W.step = n;
    storageSet("sessionStorage", "rpx.wizard", n >= 2 && n <= 4 ? String(n) : null);
    renderWizard();
    window.scrollTo(0, 0);
  }

  function renderWizard() {
    rerender = renderWizard;
    stopTimers();
    const steps = ["setup.step1", "setup.step2", "setup.step3", "setup.step4"];
    const body = { 1: wizPassword, 2: wizAllow, 3: wizUpstreams, 4: wizStart }[W.step]();
    root().replaceChildren(h("div", { class: "wizard" },
      h("div", { class: "wizard-brand" }, brandMark(), h("h1", null, t("setup.title")), h("span", { class: "spacer" }), langButton()),
      h("ol", { class: "steps" }, steps.map((k, i) => h("li", { class: i + 1 < W.step ? "done" : i + 1 === W.step ? "current" : "" }, t(k)))),
      h("div", { class: "card" }, body)));
  }

  function wizPassword() {
    if (!W.token) return h("div", { class: "callout bad" }, t("setup.noToken"));
    const pw = h("input", { type: "password", autocomplete: "new-password", minlength: "10", required: true });
    const pw2 = h("input", { type: "password", autocomplete: "new-password", required: true });
    const err = h("div");
    const next = h("button", { class: "primary", type: "submit" }, t("common.next"));
    setTimeout(() => pw.focus(), 0);
    return h("form", { onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      if (pw.value !== pw2.value) { err.replaceChildren(h("div", { class: "callout bad" }, t("setup.passwordMismatch"))); return; }
      next.disabled = true;
      try {
        await api("POST", "/api/setup", { token: W.token, password: pw.value });
        storageSet("sessionStorage", "rpx.token", null);
        SESSION = await api("GET", "/api/session");
        setStep(2);
      } catch (ex) { err.replaceChildren(h("div", { class: "callout bad" }, ex.message)); next.disabled = false; }
    } },
    h("h2", null, t("setup.step1")), h("p", { class: "lead" }, t("setup.passwordIntro")),
    h("div", { class: "field" }, h("label", null, t("setup.password")), pw, h("div", { class: "hint" }, t("setup.passwordHint"))),
    h("div", { class: "field" }, h("label", null, t("setup.passwordConfirm")), pw2),
    err, h("div", { class: "actions" }, next));
  }

  function wizAllow() {
    const ip = (SESSION && SESSION.client_ip) || "";
    const local = isLoopbackOrGateway(ip);
    if (W.allowMine == null) W.allowMine = !local;
    const err = h("div");
    const mine = h("input", { type: "checkbox", checked: W.allowMine, onchange: (e) => { W.allowMine = e.target.checked; } });
    const more = h("textarea", { placeholder: "198.51.100.0/24", spellcheck: "false", value: W.allowMore,
      oninput: (e) => { W.allowMore = e.target.value; } });
    more.style.minHeight = "80px";
    const next = h("button", { class: "primary", type: "button", onclick: async () => {
      next.disabled = true; err.replaceChildren();
      const values = (W.allowMine && ip ? [[ip, "web UI setup"]] : [])
        .concat(W.allowMore.split(/\r?\n/).map((s) => s.split("#")[0].trim()).filter(Boolean).map((v) => [v, ""]));
      try {
        for (const [value, comment] of values) await api("POST", "/api/allowlist/add", { value, comment });
        setStep(3);
      } catch (ex) { err.replaceChildren(h("div", { class: "callout bad" }, ex.message)); next.disabled = false; }
    } }, t("common.next"));
    return h("div", null,
      h("h2", null, t("setup.step2")), h("p", { class: "lead" }, t("setup.allowIntro")),
      local ? h("div", { class: "callout warn field" }, t("setup.localWarning")) : null,
      h("div", { class: "field" }, h("label", { class: "check" }, mine,
        h("span", null, t("setup.allowMine"), h("br"), h("span", { class: "small muted" }, t("setup.yourIp", { ip }))))),
      h("div", { class: "field" }, h("label", null, t("setup.allowMore")), more,
        h("div", { class: "hint" }, t("setup.allowSystem", { ips: "127.0.0.1, 172.16.231.1" }))),
      err, h("div", { class: "actions" }, next));
  }

  function vpnForm(onAdded) {
    const name = h("input", { type: "text", placeholder: "my-vpn", spellcheck: "false" });
    const file = h("input", { type: "file", accept: ".ovpn,.conf" });
    const user = h("input", { type: "text", autocomplete: "off", spellcheck: "false" });
    const pass = h("input", { type: "password", autocomplete: "new-password" });
    const err = h("div");
    const add = h("button", { type: "button", onclick: async () => {
      err.replaceChildren();
      const f = file.files && file.files[0];
      if (!f) { err.replaceChildren(h("div", { class: "callout bad" }, t("vpn.file"))); return; }
      add.disabled = true;
      try {
        const content = await f.text();
        const vname = name.value.trim() || f.name.replace(/\.(ovpn|conf)$/i, "").replace(/[^A-Za-z0-9._-]/g, "-").replace(/^[^A-Za-z0-9]+/, "") || "vpn";
        const res = await api("POST", "/api/vpns/add", { name: vname, filename: f.name, content, username: user.value, password: pass.value });
        toast(t("vpn.added", { name: res.name }));
        name.value = ""; file.value = ""; user.value = ""; pass.value = "";
        if (onAdded) onAdded(res);
      } catch (ex) { err.replaceChildren(h("div", { class: "callout bad" }, ex.message)); }
      add.disabled = false;
    } }, t("common.add"));
    return h("div", null,
      h("div", { class: "grid grid-2" },
        h("div", { class: "field" }, h("label", null, t("vpn.name")), name),
        h("div", { class: "field" }, h("label", null, t("vpn.file")), file),
        h("div", { class: "field" }, h("label", null, t("vpn.username")), user),
        h("div", { class: "field" }, h("label", null, t("vpn.password")), pass)),
      err, h("div", { class: "row end" }, add));
  }

  function wizUpstreams() {
    const err = h("div");
    const next = h("button", { class: "primary", type: "button" }, t("common.next"));
    const adder = upstreamAdder({ text: W.adder.text, scheme: W.adder.scheme, rows: W.adder.rows, onChange: (st) => {
      W.adder = { text: st.text, scheme: st.scheme, rows: st.rows };
      const ok = (st.rows || []).filter((r) => r.ok).length;
      next.textContent = ok ? t("up.addCount", { n: ok }) + " → " : t("common.next");
    } });
    next.onclick = async () => {
      next.disabled = true; err.replaceChildren();
      try {
        if (W.adder.text.trim()) await api("POST", "/api/upstreams/add", { text: W.adder.text, default_scheme: W.adder.scheme });
        setStep(4);
      } catch (ex) { err.replaceChildren(h("div", { class: "callout bad" }, ex.message)); next.disabled = false; }
    };
    const details = h("details", { open: W.vpnOpen, ontoggle: (e) => { W.vpnOpen = e.target.open; } },
      h("summary", null, t("setup.vpnOptional")), vpnForm());
    return h("div", null,
      h("h2", null, t("setup.step3")), h("p", { class: "lead" }, t("setup.upstreamIntro")),
      adder.el, h("div", { class: "field" }), details, err,
      h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: () => setStep(2) }, t("common.back")), next));
  }

  function wizStart() {
    const summary = h("p", { class: "muted" }, t("common.loading"));
    const area = h("div");
    const start = h("button", { class: "primary big", type: "button" }, t("setup.start"));
    Promise.all([api("GET", "/api/overview"), api("GET", "/api/allowlist")]).then(([ov, al]) => {
      summary.textContent = t("setup.summary", { upstreams: ov.peers - ov.counts.vpn, static: ov.static_ports.length,
        vpn: ov.counts.vpn, allow: al.entries.filter((e) => !e.invalid).length });
      W.ov = ov;
    }).catch((e) => { summary.textContent = e.message; });

    start.onclick = async () => {
      start.disabled = true;
      let job;
      try { job = await api("POST", "/api/apply", {}); } catch (e) {
        if (e.code === "busy" && e.data && e.data.job) job = await api("GET", "/api/jobs/" + e.data.job);
        else { area.replaceChildren(h("div", { class: "callout bad" }, e.message)); start.disabled = false; return; }
      }
      const status = h("div", { class: "callout info" }, h("span", { class: "spinner" }), h("span", { class: "grow" }, t("setup.starting")));
      const pre = h("pre", { class: "code log" });
      const logBox = h("details", null, h("summary", null, t("job.showLog")), pre);
      area.replaceChildren(status, h("div", { class: "field" }), logBox);
      const done = await followJob(job, (j) => { pre.textContent = j.log; pre.scrollTop = pre.scrollHeight; });
      if (done.status !== "succeeded") {
        status.className = "callout bad"; status.replaceChildren(h("span", { class: "grow" }, t("setup.failed")));
        logBox.open = true; start.disabled = false;
        return;
      }
      status.className = "callout ok";
      status.replaceChildren(h("strong", { class: "grow" }, "✓ " + t("setup.done")));
      const results = h("div", null, h("p", { class: "muted" }, h("span", { class: "spinner" }), " ", t("overview.testing")));
      area.append(h("div", { class: "field" }), results);
      start.classList.add("hidden");
      let ov = W.ov;
      try {
        const probe = await api("POST", "/api/probe", {});
        ov = await api("GET", "/api/overview");
        ov.last_probe = probe;
      } catch (e) { /* shown below */ }
      results.replaceChildren(endpointsCard(ov, { compact: true }), h("div", { class: "field" }),
        h("h3", null, t("overview.useIt")), h("div", { class: "field" }),
        snippets(proxyHost(ov), ov && ov.static_ports.length ? ov.static_ports[0].port : null),
        h("div", { class: "actions" }, h("button", { class: "primary", type: "button", onclick: () => {
          storageSet("sessionStorage", "rpx.wizard", null); location.hash = "#/overview"; appStart();
        } }, t("setup.openDashboard"))));
    };
    return h("div", null, h("h2", null, t("setup.step4")), h("p", { class: "lead" }, t("setup.startIntro")), summary,
      area, h("div", { class: "actions" }, h("button", { type: "button", class: "ghost", onclick: () => setStep(3) }, t("common.back")), start));
  }

  // ----------------------------------------------------------------------
  // Dashboard
  // ----------------------------------------------------------------------
  const VIEWS = ["overview", "upstreams", "allowlist", "vpn"];
  let VIEW = "overview";
  let OV = null;
  let timer = null;
  let routeBound = false;

  function stopTimers() { if (timer) { clearInterval(timer); timer = null; } }

  function appStart() {
    if (!routeBound) { window.addEventListener("hashchange", () => { if (rerender === renderApp) renderApp(); }); routeBound = true; }
    if (location.pathname !== "/") history.replaceState(null, "", "/" + location.hash);
    renderApp();
  }

  function renderApp() {
    rerender = renderApp;
    stopTimers();
    const v = location.hash.replace(/^#\/?/, "");
    VIEW = VIEWS.includes(v) ? v : "overview";
    const main = h("main");
    const banners = h("div", { class: "banners" });
    root().replaceChildren(
      h("header", { class: "topbar" },
        h("div", { class: "brand" }, brandMark(), t("brand")),
        h("nav", { class: "nav" }, VIEWS.map((k) => h("a", { href: "#/" + k, class: k === VIEW ? "active" : "" }, t("nav." + k)))),
        h("div", { class: "topbar-right" }, langButton(),
          h("button", { class: "ghost", type: "button", onclick: async () => { try { await api("POST", "/api/logout", {}); } catch (e) { /* ignore */ } renderLogin(); } }, t("nav.logout")))),
      banners, main);
    const load = { overview: viewOverview, upstreams: viewUpstreams, allowlist: viewAllowlist, vpn: viewVpn }[VIEW];
    main.append(h("p", { class: "muted" }, t("common.loading")));
    refreshBanners(banners);
    load(main);
    if (VIEW === "overview") timer = setInterval(() => { if (!document.querySelector(".modal-backdrop")) viewOverview(main, true); }, 15000);
  }

  async function refreshBanners(box) {
    box = box || document.querySelector(".banners");
    if (!box) return;
    try { OV = await api("GET", "/api/overview"); } catch (e) { return; }
    const items = [];
    if (OV.pending) {
      items.push(h("div", { class: "callout info" }, h("span", { class: "grow" }, t("banner.pending")),
        h("button", { class: "primary", type: "button", onclick: () => startApply(() => renderApp()) }, t("banner.apply"))));
    }
    if (!OV.client_allowed && OV.client_ip && !isLoopbackOrGateway(OV.client_ip)) {
      items.push(h("div", { class: "callout warn" }, h("span", { class: "grow" }, t("banner.notAllowed", { ip: OV.client_ip })),
        h("button", { type: "button", onclick: async () => {
          try { await api("POST", "/api/allowlist/add", { value: OV.client_ip, comment: "web UI" }); renderApp(); } catch (e) { toast(e.message, "bad"); }
        } }, t("banner.addIt"))));
    }
    box.replaceChildren(...items);
  }

  // ---- overview --------------------------------------------------------

  function endpointsCard(ov, opts) {
    opts = opts || {};
    const host = proxyHost(ov);
    const probe = ov && ov.last_probe;
    const byPort = {};
    if (probe) for (const s of probe.static) byPort[s.port] = s;
    const rotUrl = "http://" + hostForUrl(host) + ":" + ((ov && ov.rotating_port) || 3128);
    const copyBtn = h("button", { class: "small", type: "button", onclick: () => copyText(rotUrl, copyBtn) }, t("common.copy"));
    const rot = probe ? probe.rotating : null;
    const rotLine = rot
      ? h("span", { class: "badge " + (rot.ok === rot.total ? "ok" : rot.ok ? "warn" : "bad") },
        t("overview.rotationResult", { ok: rot.ok, total: rot.total, ips: rot.ips.length }))
      : h("span", { class: "badge plain" }, t("overview.notTested"));
    const testBtn = h("button", { type: "button", onclick: async () => {
      testBtn.disabled = true; testBtn.replaceChildren(h("span", { class: "spinner" }), t("overview.testing"));
      try { const p = await api("POST", "/api/probe", {}); ov.last_probe = p; card.replaceWith(endpointsCard(ov, opts)); }
      catch (e) { toast(e.message, "bad"); testBtn.disabled = false; testBtn.textContent = t("overview.test"); }
    } }, t("overview.test"));
    const rows = (ov ? ov.static_ports : []).map((sp) => {
      const r = byPort[sp.port];
      return h("tr", null, h("td", { class: "num" }, h("strong", { class: "mono" }, sp.port)),
        h("td", null, h("span", { class: "small" }, sp.description)),
        h("td", null, r ? (r.ok ? h("span", { class: "mono" }, r.ip) : h("span", { class: "small muted" }, r.error)) : h("span", { class: "muted" }, "—")),
        h("td", { class: "num" }, r && r.ok ? r.ms + " ms" : ""),
        h("td", null, r ? h("span", { class: "badge " + (r.ok ? "ok" : "bad") }, r.ok ? "OK" : "NG") : ""));
    });
    const card = h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, t("overview.endpoints")),
        probe ? h("span", { class: "sub" }, fmtTime(probe.at)) : null, testBtn),
      h("div", { class: "stack" },
        h("div", null, h("h3", null, t("overview.rotation")), h("p", { class: "small muted" }, t("overview.rotationHint")),
          h("div", { class: "endpoint" }, h("code", null, rotUrl), rotLine, copyBtn),
          rot && rot.ips.length ? h("p", { class: "small muted" }, rot.ips.join("  ·  ")) : null,
          rot && rot.errors.length ? h("p", { class: "small muted" }, rot.errors.join(", ")) : null),
        h("div", null, h("h3", null, t("overview.static")), h("p", { class: "small muted" }, t("overview.staticHint")),
          rows.length ? h("div", { class: "table-wrap" }, h("table", null,
            h("thead", null, h("tr", null, h("th", { class: "num" }, t("overview.port")), h("th", null, t("overview.upstream")),
              h("th", null, t("overview.exitIp")), h("th", { class: "num" }, t("overview.latency")), h("th", null, ""))),
            h("tbody", null, rows))) : h("div", { class: "empty table-wrap" }, t("common.none")))));
    return card;
  }

  async function viewOverview(main, quiet) {
    let ov;
    try { ov = await api("GET", "/api/overview"); } catch (e) { if (!quiet) main.replaceChildren(h("div", { class: "callout bad" }, e.message)); return; }
    if (OV && OV.last_probe && !ov.last_probe) ov.last_probe = OV.last_probe;
    OV = ov;
    const services = ov.compose.services;
    const healthy = services.filter((s) => s.state === "running" && (s.health === "healthy" || s.health === "")).length;
    const stat = (label, value, hint) => h("div", { class: "card stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), hint ? h("div", { class: "hint" }, hint) : null);
    const c = ov.counts;
    main.replaceChildren(h("div", { class: "stack" },
      !ov.compose.generated ? h("div", { class: "callout info" }, t("overview.notGenerated")) : null,
      ov.compose.error ? h("div", { class: "callout bad" }, t("overview.dockerError", { error: ov.compose.error })) : null,
      h("div", { class: "grid grid-4" },
        stat(t("overview.upstreams"), ov.peers, ["socks5/http " + c.gost, "https " + c.https, "httpsquid " + c.private, "public " + c.public]
          .filter((s) => !/ 0$/.test(s)).join(" · ") || "—"),
        stat(t("overview.static"), ov.static_ports.length, ov.static_ports.length ? ov.static_ports[0].port + "–" + ov.static_ports[ov.static_ports.length - 1].port : "—"),
        stat(t("nav.vpn"), c.vpn, null),
        stat(t("overview.services"), services.length ? healthy + " / " + services.length : "—", t("overview.appliedAt", { when: fmtTime(ov.applied_at) }))),
      endpointsCard(ov),
      h("div", { class: "grid grid-2" },
        h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("overview.services"))),
          services.length ? h("div", { class: "table-wrap" }, h("table", null, h("tbody", null, services.map((s) =>
            h("tr", null, h("td", null, h("strong", null, s.service)), h("td", { class: "small muted" }, s.name),
              h("td", null, healthBadge(s))))))) : h("div", { class: "empty" }, "—")),
        h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("overview.useIt"))),
          snippets(proxyHost(ov), ov.static_ports.length ? ov.static_ports[0].port : null))),
      ov.warnings.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("overview.warnings")),
        h("span", { class: "sub" }, String(ov.warnings_total))),
        h("pre", { class: "code log" }, ov.warnings.join("\n"))) : null));
  }

  // ---- upstreams -------------------------------------------------------

  async function viewUpstreams(main) {
    let data;
    try { data = await api("GET", "/api/upstreams"); } catch (e) { main.replaceChildren(h("div", { class: "callout bad" }, e.message)); return; }
    const selected = new Set();
    const addBtn = h("button", { class: "primary", type: "button", disabled: true }, t("common.add"));
    const adder = upstreamAdder({ onChange: (st) => {
      const ok = (st.rows || []).filter((r) => r.ok).length;
      addBtn.disabled = !ok; addBtn.textContent = ok ? t("up.addCount", { n: ok }) : t("common.add");
    } });
    addBtn.onclick = async () => {
      addBtn.disabled = true;
      try {
        const res = await api("POST", "/api/upstreams/add", { text: adder.st.text, default_scheme: adder.st.scheme });
        toast(t("up.added", { n: res.added, dup: res.duplicates ? t("up.dup", { n: res.duplicates }) : "" }));
        renderApp();
      } catch (e) { toast(e.message, "bad"); addBtn.disabled = false; }
    };
    const delBtn = h("button", { class: "danger", type: "button", disabled: true, onclick: async () => {
      try { const r = await api("POST", "/api/upstreams/delete", { ids: Array.from(selected) }); toast(t("up.deleted", { n: r.removed })); renderApp(); }
      catch (e) { toast(e.message, "bad"); }
    } }, t("common.deleteSelected"));
    const toggle = (id, on) => { if (on) selected.add(id); else selected.delete(id); delBtn.disabled = !selected.size; };
    const rows = data.entries.map((e) => h("tr", null,
      h("td", null, h("input", { type: "checkbox", onchange: (ev) => toggle(e.id, ev.target.checked) })),
      h("td", { class: "num muted small" }, e.line),
      h("td", null, kindBadge(e.kind)),
      h("td", null, h("span", { class: "mono" }, e.hostport)),
      h("td", null, e.user ? h("span", { class: "mono" }, e.user + (e.has_password ? ":•••" : "")) : h("span", { class: "muted" }, "—")),
      h("td", { class: "num" }, e.static_port ? h("strong", { class: "mono" }, e.static_port) : h("span", { class: "muted small" }, t("up.rotationOnly")))));

    const publicUrl = h("input", { type: "url", placeholder: "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt" });
    const pubBtn = h("button", { type: "button", onclick: async () => {
      try {
        const body = publicUrl.value.trim() ? { url: publicUrl.value.trim() } : {};
        const job = await api("POST", "/api/public/refresh", body);
        openJobModal(job, "job.public", () => renderApp());
      } catch (e) { toast(e.message, "bad"); }
    } }, t("up.publicRefresh"));

    main.replaceChildren(h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("up.add"))), adder.el, h("div", { class: "row end field" }), h("div", { class: "row end" }, addBtn)),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", null, t("up.list")), h("span", { class: "sub" }, "proxyList.txt · " + data.entries.length), delBtn),
        data.entries.length ? h("div", { class: "table-wrap" }, h("table", null,
          h("thead", null, h("tr", null, h("th", null, ""), h("th", { class: "num" }, t("common.line")), h("th", null, t("up.type")),
            h("th", null, t("up.host")), h("th", null, t("up.user")), h("th", { class: "num" }, t("up.static")))),
          h("tbody", null, rows))) : h("div", { class: "empty table-wrap" }, t("common.none"))),
      data.invalid.length ? h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("up.invalid"))),
        h("div", { class: "table-wrap" }, h("table", null, h("tbody", null, data.invalid.map((e) => h("tr", null,
          h("td", { class: "num muted small" }, e.line), h("td", null, h("span", { class: "mono" }, e.text)),
          h("td", { class: "small muted" }, e.error),
          h("td", null, h("button", { class: "small danger", type: "button", onclick: async () => {
            try { await api("POST", "/api/upstreams/delete", { ids: [e.id] }); renderApp(); } catch (ex) { toast(ex.message, "bad"); }
          } }, t("common.delete"))))))))) : null,
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("up.public")),
        h("span", { class: "sub" }, t("up.publicCount", { n: data.public_count }))),
        h("p", { class: "small muted" }, t("up.publicIntro")),
        h("div", { class: "inline-fields" }, h("div", { class: "field" }, h("label", null, t("up.publicUrl")), publicUrl), h("div"), h("div", { class: "field" }, pubBtn)))));
  }

  // ---- allowlist -------------------------------------------------------

  async function viewAllowlist(main) {
    let data;
    try { data = await api("GET", "/api/allowlist"); } catch (e) { main.replaceChildren(h("div", { class: "callout bad" }, e.message)); return; }
    const selected = new Set();
    const ip = data.client_ip;
    const present = data.entries.some((e) => e.value === ip);
    const value = h("input", { type: "text", placeholder: "198.51.100.7 / 198.51.100.0/24", spellcheck: "false" });
    const note = h("input", { type: "text", placeholder: "office" });
    const add = async (v, c) => {
      try { const r = await api("POST", "/api/allowlist/add", { value: v, comment: c }); if (r.added) toast(r.value + " ✓"); renderApp(); }
      catch (e) { toast(e.message, "bad"); }
    };
    const delBtn = h("button", { class: "danger", type: "button", disabled: true, onclick: async () => {
      try { await api("POST", "/api/allowlist/delete", { ids: Array.from(selected) }); renderApp(); } catch (e) { toast(e.message, "bad"); }
    } }, t("common.deleteSelected"));
    main.replaceChildren(h("div", { class: "stack" }, h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", null, t("allow.title")), h("span", { class: "sub" }, "template/allowed_ip.txt"), delBtn),
      h("p", { class: "muted" }, t("allow.intro")),
      h("div", { class: "callout " + (present ? "ok" : "info") + " field" },
        h("span", { class: "grow" }, t("allow.yourIp", { ip }), " ", present ? "✓" : ""),
        present ? null : h("button", { class: "primary", type: "button", onclick: () => add(ip, "web UI") }, t("allow.addMine"))),
      h("form", { class: "inline-fields field", onsubmit: (e) => { e.preventDefault(); if (value.value.trim()) add(value.value.trim(), note.value); } },
        h("div", null, h("label", null, t("allow.value")), value), h("div", null, h("label", null, t("allow.comment")), note),
        h("button", { type: "submit" }, t("common.add"))),
      h("div", { class: "table-wrap" }, h("table", null,
        h("thead", null, h("tr", null, h("th", null, ""), h("th", null, t("allow.address")), h("th", null, t("allow.note")), h("th", null, ""))),
        h("tbody", null, data.entries.map((e) => h("tr", null,
          h("td", null, e.system ? "🔒" : h("input", { type: "checkbox", onchange: (ev) => {
            if (ev.target.checked) selected.add(e.id); else selected.delete(e.id); delBtn.disabled = !selected.size;
          } })),
          h("td", null, h("span", { class: "mono" }, e.value), e.invalid ? h("span", { class: "badge bad" }, "invalid") : null),
          h("td", { class: "small muted" }, e.comment),
          h("td", null, e.system ? h("span", { class: "badge plain" }, t("allow.system")) : e.value === ip ? h("span", { class: "badge info plain" }, "you") : null)))))))));
  }

  // ---- vpn -------------------------------------------------------------

  async function viewVpn(main) {
    let data, ov;
    try { [data, ov] = await Promise.all([api("GET", "/api/vpns"), api("GET", "/api/overview")]); }
    catch (e) { main.replaceChildren(h("div", { class: "callout bad" }, e.message)); return; }
    const stateOf = {};
    for (const s of ov.compose.services) stateOf[s.service] = s;
    main.replaceChildren(h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("vpn.title")), h("span", { class: "sub" }, "openvpn/")),
        h("p", { class: "muted" }, t("vpn.intro")),
        data.vpns.length ? h("div", { class: "table-wrap" }, h("table", null,
          h("thead", null, h("tr", null, h("th", null, t("vpn.name")), h("th", null, t("vpn.file")), h("th", null, t("vpn.credentials")),
            h("th", { class: "num" }, t("up.static")), h("th", null, t("vpn.container")), h("th", null, ""))),
          h("tbody", null, data.vpns.map((v) => h("tr", null,
            h("td", null, h("strong", null, v.name)), h("td", null, h("span", { class: "mono small" }, v.file)),
            h("td", null, v.has_credentials ? t("common.yes") : h("span", { class: "muted" }, t("common.no"))),
            h("td", { class: "num" }, h("strong", { class: "mono" }, v.static_port)),
            h("td", null, stateOf[v.service] ? healthBadge(stateOf[v.service]) : h("span", { class: "muted small" }, v.container)),
            h("td", null, h("button", { class: "small danger", type: "button", onclick: async () => {
              if (!window.confirm(t("vpn.deleteConfirm", { name: v.name }))) return;
              try { await api("POST", "/api/vpns/delete", { name: v.name }); renderApp(); } catch (e) { toast(e.message, "bad"); }
            } }, t("common.delete")))))))) : h("div", { class: "empty table-wrap" }, t("common.none"))),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", null, t("vpn.add"))), vpnForm(() => renderApp()))));
  }

  boot();
})();
