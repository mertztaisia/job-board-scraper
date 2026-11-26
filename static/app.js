/* Job Tracker UI */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const store = {
    get(k, d) { try { const v = localStorage.getItem("jt." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("jt." + k, JSON.stringify(v)); } catch { /* ignore */ } },
  };

  let META = { statuses: {}, interests: [], ai: {} };
  let STATUSES = {};
  let RESUMES = [];
  const FILTER_DEFAULTS = {
    statuses: [], tags: [], remote: false, paid: false, added: "",
    fits: [], minPay: "", maxYears: "", posted: "", sites: [], described: false, todo: false, locs: [], companies: [],
  };
  const state = Object.assign({ q: "", ...FILTER_DEFAULTS, sort: "status", order: "asc" },
    store.get("filters.v2", {}));
  state.q = "";
  state.archived = false;   // never persisted: archived roles stay hidden on every fresh load
  state.sort = "status";    // always open in the default order: Not applied first ... Not a fit last
  state.order = "asc";
  state.page = 1;
  state.pageSize = [25, 50, 100].includes(store.get("pageSize", 50)) ? store.get("pageSize", 50) : 50;
  let roles = [];

  // ------------------------------------------------------------------ Search and filters <-> URL
  // The roles view's search and filters live in the address bar (#/?q=ml&fit=strong,good),
  // so a view can be bookmarked, shared or reloaded. Only values that differ from the
  // defaults are written. A URL with filters in it wins over the last filters remembered.
  const URL_KEYS = [   // [state key, URL parameter, kind]
    ["q", "q", "text"], ["statuses", "status", "list"], ["fits", "fit", "list"], ["tags", "tags", "pipe"],
    ["remote", "remote", "flag"], ["paid", "paid", "flag"], ["described", "described", "flag"], ["todo", "todo", "flag"],
    ["minPay", "min_pay", "text"], ["maxYears", "max_years", "text"], ["posted", "posted", "text"], ["added", "added", "text"],
    ["sites", "sites", "list"], ["locs", "loc", "pipe"], ["companies", "co", "pipe"], ["archived", "archived", "flag"],
    ["sort", "sort", "text"], ["order", "order", "text"], ["page", "page", "int"],
  ];
  const URL_DEFAULTS = { q: "", ...FILTER_DEFAULTS, archived: false, sort: "status", order: "asc", page: 1 };
  function hashParams() {
    const i = location.hash.indexOf("?");
    return new URLSearchParams(i >= 0 ? location.hash.slice(i + 1) : "");
  }
  function stateFromUrl() {
    const p = hashParams();
    if (!URL_KEYS.some(([, name]) => p.has(name))) return false;
    Object.assign(state, structuredClone(URL_DEFAULTS));
    for (const [key, name, kind] of URL_KEYS) {
      if (!p.has(name)) continue;
      const v = p.get(name);
      state[key] = kind === "list" ? v.split(",").filter(Boolean)
        : kind === "pipe" ? v.split("|").filter(Boolean)
        : kind === "flag" ? v === "1"
        : kind === "int" ? Math.max(1, parseInt(v, 10) || 1) : v;
    }
    return true;
  }
  function urlFromState() {
    const parts = [];
    for (const [key, name, kind] of URL_KEYS) {
      const v = state[key];
      let out = null;
      if (kind === "list" || kind === "pipe") out = v.length ? v.map(encodeURIComponent).join(kind === "pipe" ? "|" : ",") : null;
      else if (kind === "flag") out = v ? "1" : null;
      else if (v !== URL_DEFAULTS[key] && v !== "" && v != null) out = encodeURIComponent(String(v));
      if (out != null) parts.push(`${name}=${out}`);
    }
    if (selectedId) parts.push(`role=${selectedId}`);   // the open role, so a refresh reopens it
    return "#/" + (parts.length ? "?" + parts.join("&") : "");
  }
  // Keep the address bar in step with the roles view, without adding a history entry per click.
  function syncUrl() {
    if (route() !== "roles") return;
    const want = urlFromState();
    if (location.hash !== want) history.replaceState(null, "", want);
  }
  let selectedId = null;
  let current = null;

  // ------------------------------------------------------------------ API
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" }, ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch { /* empty */ }
    if (!res.ok) { const e = new Error((data && data.error) || res.statusText); e.status = res.status; e.data = data; throw e; }
    return data;
  }

  // ------------------------------------------------------------------ Formatting
  const money = (n) => n >= 1000 ? `$${Math.round(n / 1000)}k` : `$${Math.round(n)}`;
  const dollars = (n, d = 2) => `$${(n || 0).toFixed(d)}`;
  function fmtPay(r) {
    if (r.pay_min == null && r.pay_max == null) return '<span class="cell-dim">—</span>';
    if (r.pay_min == null || r.pay_min === r.pay_max) return money(r.pay_max ?? r.pay_min);
    return `${money(r.pay_min)}–${money(r.pay_max)}`;
  }
  function fmtYears(r) {
    if (r.years_min == null) return '<span class="cell-dim">—</span>';
    return r.years_max ? `${r.years_min}–${r.years_max} yrs` : `${r.years_min}+ yrs`;
  }
  function fmtDate(s, withTime = false) {
    if (!s) return "";
    const d = new Date(s.length <= 10 ? s + "T12:00:00" : s);
    if (isNaN(d)) return esc(s);
    const opts = { month: "short", day: "numeric" };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
    let out = d.toLocaleDateString(undefined, opts);
    if (withTime && s.length > 10) out += " " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    return out;
  }
  function ago(s) {
    if (!s) return "";
    const mins = (Date.now() - new Date(s)) / 60000;
    if (mins < 1) return "just now";
    if (mins < 60) return `${Math.round(mins)}m ago`;
    if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
    return `${Math.round(mins / 1440)}d ago`;
  }
  const localISO = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  function isFresh(r) {
    if (r.status !== "not_applied" || !r.date_posted) return false;
    const y = new Date(); y.setDate(y.getDate() - 1);
    return r.date_posted >= localISO(y);
  }
  // Short labels for the narrow Status column; the full label shows on hover.
  const STATUS_SHORT = { closed: "Closed" };
  // Sidebar filter list: one line per status (full label on hover).
  const SIDEBAR_SHORT = { closed: "No longer accepting" };
  const statusTag = (s, short = false) => {
    const full = STATUSES[s] || s;
    const label = short && STATUS_SHORT[s] ? STATUS_SHORT[s] : full;
    return `<span class="tag s-${esc(s)}"${label !== full ? ` title="${esc(full)}"` : ""}><span class="dot"></span>${esc(label)}</span>`;
  };
  // AI match level (strong / good / partial / weak / not_a_fit / unknown)
  const MATCH_CLS = { strong: "strong", good: "good", partial: "partial", weak: "weak", not_a_fit: "no", unknown: "unsure" };
  const matchOf = (r) => r.ai_match || null;             // null = not screened yet
  const matchInfo = (m) => (META.match_levels || {})[m] || { label: m, desc: "" };
  function fitPill(r) {
    const m = matchOf(r);
    if (!m) return '<span class="cell-dim" title="Not screened by AI yet">—</span>';
    const lv = matchInfo(m);
    const tip = `${lv.label}: ${lv.desc}${r.ai_reason ? "\n\nWhy: " + r.ai_reason : ""}`;
    return `<span class="fit ${MATCH_CLS[m] || ""}" title="${esc(tip)}"><span class="dot"></span>${esc(lv.label)}</span>`;
  }

  // Short names for the roles list: "Tech screen · to do · Nov 25".
  const STEP_STATE_SHORT = { scheduled: "to do", waiting: "waiting", passed: "passed", rejected: "rejected", cancelled: "cancelled" };
  const STEP_KIND_SHORT = { recruiter: "Recruiter", technical_screen: "Tech screen", hiring_manager: "Hiring mgr",
    technical: "Technical", coding: "Coding", system_design: "Sys design", behavioral: "Behavioral",
    take_home: "Take-home", onsite: "Onsite", other: "Other" };
  function statusExtra(r) {
    // The current step: the next one still to do (soonest first), else the latest one.
    if (["interviewing", "applied"].includes(r.status) && r.current_step) {
      const [n, st, kind, date] = r.current_step.split("|");
      const todo = st === "scheduled";
      const overdue = todo && date && daysUntil(date) < 0;
      const parts = [STEP_KIND_SHORT[kind] || `Step ${n}`, STEP_STATE_SHORT[st] || st];
      if (todo && date) parts.push(fmtDate(date));
      const full = `${roundKind(kind)}: ${roundStatus(st)}${date ? ` (${fmtDate(date)})` : ""}`;
      return `<span class="status-extra ${overdue ? "overdue" : todo ? "todo" : ""}" title="${esc(full)}">${esc(parts.join(" · "))}</span>`;
    }
    if (r.status === "offer" && r.offer_deadline) return `<span class="status-extra">due ${fmtDate(r.offer_deadline)}</span>`;
    return "";
  }
  function daysUntil(d) {
    const t = new Date(d + "T00:00:00"), today = new Date(); today.setHours(0, 0, 0, 0);
    return Math.round((t - today) / 86400000);
  }

  function renderMarkdown(src, highlight) {
    const inline = (s) => s
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])\*(?!\s)([^*]+?)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    const lines = esc(src || "").replace(/\\([*_#\-[\]().!+>])/g, "$1").split(/\r?\n/);
    let html = "", inList = false;
    for (const raw of lines) {
      const t = raw.trim();
      const li = t.match(/^(?:[*\-•]|\d+\.)\s+(.*)/);
      if (li) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
      if (inList) { html += "</ul>"; inList = false; }
      if (!t) continue;
      const h = t.match(/^#{1,6}\s+(.*)/);
      html += h ? `<h4>${inline(h[1])}</h4>` : `<p>${inline(t)}</p>`;
    }
    if (inList) html += "</ul>";
    html = html.replace(/(\d{1,2}\s*(?:\+|–|-|to)?\s*\d{0,2}\+?\s*years?)/gi, "<mark>$1</mark>");
    for (const tok of (highlight || "").split(/\s+/).filter((t) => t.length > 2)) {
      const re = new RegExp(`(?![^<]*>)(${tok.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
      html = html.replace(re, "<mark>$1</mark>");
    }
    return html;
  }

  // ------------------------------------------------------------------ Toasts
  function toast(msg, kind = "") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = esc(msg);
    $("#toasts").appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 4200);
    setTimeout(() => el.remove(), 4600);
  }

  // ------------------------------------------------------------------ Confirm dialog
  function confirmDialog({ title, body, ok = "Continue", cancel = "Cancel" }) {
    return new Promise((resolve) => {
      $("#confirmTitle").textContent = title;
      $("#confirmBody").innerHTML = body;
      $("#confirmOk").textContent = ok;
      // cancel: null hides the second button (a dialog with a single "OK")
      const cancelBtn = $("#confirmDialog .dialog-footer [data-cclose]");
      cancelBtn.textContent = cancel || "";
      cancelBtn.classList.toggle("hidden", !cancel);
      $("#confirmDialog").classList.remove("hidden");
      const done = (v) => { $("#confirmDialog").classList.add("hidden"); resolve(v); };
      $("#confirmOk").onclick = () => done(true);
      $$("#confirmDialog [data-cclose]").forEach((b) => b.onclick = () => done(false));
      $("#confirmDialog").onmousedown = (e) => { if (e.target.id === "confirmDialog") done(false); };
      setTimeout(() => $("#confirmOk").focus(), 30);
    });
  }

  // ------------------------------------------------------------------ Sidebar
  function renderStatusList(counts = {}) {
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    const allOn = state.statuses.length === 0;
    $("#statusList").innerHTML =
      `<li class="status-item all ${allOn ? "on" : ""}" data-s=""><span class="check"></span>All statuses<span class="count">${total}</span></li>` +
      Object.entries(STATUSES).map(([k, label]) => `
        <li class="status-item ${state.statuses.includes(k) ? "on" : ""}" data-s="${k}"${SIDEBAR_SHORT[k] ? ` title="${esc(label)}"` : ""}>
          <span class="check"></span><span class="tag s-${k}" style="background:none;padding:0"><span class="dot"></span></span>${esc(SIDEBAR_SHORT[k] || label)}
          <span class="count">${counts[k] || 0}</span>
        </li>`).join("");
  }

  function renderTagFilter() {
    const tags = META.interests || [];
    state.tags = state.tags.filter((t) => tags.includes(t));
    $("#tagFilter").innerHTML = tags.length
      ? tags.map((t) => `<button class="chip ${state.tags.includes(t) ? "on" : ""}" data-tag="${esc(t)}">${esc(t)}</button>`).join("")
      : `<span class="empty-note">Add interests on the <a href="#/preferences">Preferences</a> page to filter by them.</span>`;
  }

  // AI fit and source chips (multi-select; none selected = all).
  const SITE_LABELS = { linkedin: "LinkedIn", indeed: "Indeed", zip_recruiter: "ZipRecruiter", glassdoor: "Glassdoor",
    manual: "Added by you" };
  function renderChipFilters() {
    const fits = [...Object.entries(META.match_levels || {}).map(([k, v]) => [k, v.label]), ["unscreened", "Not screened"]];
    $("#fitFilter").innerHTML = fits.map(([k, l]) =>
      `<button class="chip ${state.fits.includes(k) ? "on" : ""}" data-fit="${k}">${esc(l)}</button>`).join("");
    const sites = META.sites || [];
    state.sites = state.sites.filter((s) => sites.includes(s));
    $("#siteFilter").innerHTML = sites.length
      ? sites.map((s) => `<button class="chip ${state.sites.includes(s) ? "on" : ""}" data-site="${esc(s)}">${esc(SITE_LABELS[s] || s)}</button>`).join("")
      : '<span class="empty-note">No roles yet.</span>';
    // Location: the Remote chip, places you've picked (click to remove), then every city in
    // your roles, most common first (the first few, then "+N more").
    const picked = new Set(state.locs.map((l) => l.toLowerCase()));
    const suggestions = (META.places || []).filter((c) => !picked.has(c.name.toLowerCase()));
    const shown = showAllPlaces ? suggestions : suggestions.slice(0, PLACES_SHOWN);
    const more = suggestions.length - shown.length;
    $("#locFilter").innerHTML =
      `<button class="chip ${state.remote ? "on" : ""}" data-remote title="Remote roles only">Remote</button>`
      + state.locs.map((l) => `<button class="chip on" data-loc-rm="${esc(l)}" title="Remove">${esc(l)}<span class="chip-x">×</span></button>`).join("")
      + shown.map((c) => `<button class="chip add" data-loc-add="${esc(c.name)}" title="${c.count} role${c.count === 1 ? "" : "s"}">${esc(c.name)}</button>`).join("")
      + (more > 0 ? `<button class="chip" data-places-more>+${more} more</button>`
        : showAllPlaces && suggestions.length > PLACES_SHOWN ? `<button class="chip" data-places-more>Show fewer</button>` : "");
  }
  const PLACES_SHOWN = 8;
  let showAllPlaces = false;

  // Company: employers you've picked (click to remove), then the employers with the most
  // roles (the first few, then "+N more"); the box suggests every employer as you type.
  function renderCompanyFilter() {
    const picked = new Set(state.companies.map((c) => c.toLowerCase()));
    const all = META.companies || [];
    const suggestions = all.filter((c) => !picked.has(c.name.toLowerCase()));
    const shown = showAllCompanies ? suggestions : suggestions.slice(0, COMPANIES_SHOWN);
    const more = suggestions.length - shown.length;
    $("#coFilter").innerHTML =
      state.companies.map((c) => `<button class="chip on" data-co-rm="${esc(c)}" title="Remove">${esc(c)}<span class="chip-x">×</span></button>`).join("")
      + shown.map((c) => `<button class="chip add" data-co-add="${esc(c.name)}" title="${c.count} role${c.count === 1 ? "" : "s"}">${esc(c.name)}</button>`).join("")
      + (more > 0 ? `<button class="chip" data-companies-more>+${more} more</button>`
        : showAllCompanies && suggestions.length > COMPANIES_SHOWN ? `<button class="chip" data-companies-more>Show fewer</button>` : "")
      + (!all.length && !state.companies.length ? '<span class="empty-note">No roles yet.</span>' : "");
    const opts = all.map((c) => `<option value="${esc(c.name)}">${c.count} role${c.count === 1 ? "" : "s"}</option>`).join("");
    if ($("#companyOptions").innerHTML !== opts) $("#companyOptions").innerHTML = opts;
  }
  const COMPANIES_SHOWN = 8;
  let showAllCompanies = false;

  // Badge on each collapsible group: how many of its filters are on.
  function renderFilterBadges() {
    const counts = {
      status: state.statuses.length,
      fit: state.fits.length,
      interests: state.tags.length,
      location: (state.remote ? 1 : 0) + state.locs.length,
      company: state.companies.length,
      role: [state.paid, state.described, state.minPay, state.maxYears].filter(Boolean).length,
      dates: [state.posted, state.added].filter(Boolean).length,
      source: state.sites.length,
      more: (state.archived ? 1 : 0) + (state.todo ? 1 : 0),
    };
    for (const [g, n] of Object.entries(counts)) {
      const b = $(`#badge-${g}`);
      b.textContent = n || "";
      b.classList.toggle("on", n > 0);
    }
  }

  function renderNavStats(counts) {
    const applied = ["applied", "interviewing", "offer", "rejected"].reduce((a, k) => a + (counts[k] || 0), 0);
    $("#navStats").innerHTML = [
      ["To review", counts.not_applied || 0],
      ["Applied", applied],
      ["Interviewing", counts.interviewing || 0],
      ["Offers", counts.offer || 0],
    ].map(([l, n]) => `<span class="nav-stat"><b>${n}</b>${l}</span>`).join("");
  }

  function syncFilterControls() {
    $("#fPaid").checked = state.paid;
    $("#fArchived").checked = state.archived;
    $$("#fAdded button").forEach((b) => b.classList.toggle("on", b.dataset.v === state.added));
    $$("#fPosted button").forEach((b) => b.classList.toggle("on", b.dataset.v === state.posted));
    $("#fDescribed").checked = state.described;
    $("#fTodo").checked = state.todo;
    $("#fMinPay").value = state.minPay;
    $("#fMaxYears").value = state.maxYears;
    renderFilterBadges();
    $$(".grid th[data-sort]").forEach((th) => {
      th.classList.toggle("sorted", th.dataset.sort === state.sort);
      th.classList.toggle("asc", th.dataset.sort === state.sort && state.order === "asc");
    });
  }

  // ------------------------------------------------------------------ Table
  let loadSeq = 0;
  async function load() {
    const seq = ++loadSeq;
    const p = new URLSearchParams({ q: state.q, sort: state.sort, order: state.order });
    if (state.statuses.length) p.set("status", state.statuses.join(","));
    if (state.tags.length) p.set("tags", state.tags.join("|"));
    if (state.remote) p.set("remote", "1");
    if (state.paid) p.set("paid", "1");
    if (state.added) p.set("added", state.added);
    if (state.archived) p.set("archived", "1");
    if (state.fits.length) p.set("fit", state.fits.join(","));
    if (state.minPay) p.set("min_pay", state.minPay);
    if (state.maxYears) p.set("max_years", state.maxYears);
    if (state.posted) p.set("posted", state.posted);
    if (state.sites.length) p.set("sites", state.sites.join(","));
    if (state.described) p.set("described", "1");
    if (state.todo) p.set("todo", "1");
    if (state.locs.length) p.set("loc", state.locs.join("|"));
    if (state.companies.length) p.set("co", state.companies.join("|"));
    p.set("limit", state.pageSize);
    p.set("offset", (state.page - 1) * state.pageSize);
    const data = await api("/api/roles?" + p);
    if (seq !== loadSeq) return;
    const pages = Math.max(1, Math.ceil(data.matching / state.pageSize));
    if (state.page > pages) { state.page = pages; return load(); }   // e.g. last item on a page was archived
    roles = data.roles;
    renderStatusList(data.counts);
    renderTagFilter();
    renderChipFilters();
    renderCompanyFilter();
    renderNavStats(data.all_counts);
    renderRows();
    renderPager(data.matching);
    $("#resultCount").innerHTML = `<b>${data.matching}</b> of <b>${data.total}</b> roles` +
      (state.archived ? " (incl. archived)" : "");
    $("#archivedCount").textContent = data.archived_count ? `(${data.archived_count})` : "";
    store.set("filters.v2", { ...state, q: "", archived: false, sort: "status", order: "asc", page: 1 });
    syncFilterControls();
    syncUrl();
  }

  // Filters, search and sort changes go back to page 1.
  function refilter() { state.page = 1; load(); }

  function pageList(cur, last) {
    const out = new Set([1, last, cur - 1, cur, cur + 1]);
    return [...out].filter((n) => n >= 1 && n <= last).sort((a, b) => a - b)
      .flatMap((n, i, arr) => (i && n - arr[i - 1] > 1 ? ["…", n] : [n]));
  }

  function renderPager(matching) {
    const el = $("#pager");
    const last = Math.max(1, Math.ceil(matching / state.pageSize));
    const from = matching ? (state.page - 1) * state.pageSize + 1 : 0;
    const to = Math.min(matching, state.page * state.pageSize);
    el.classList.toggle("hidden", matching === 0);
    el.innerHTML = `
      <span class="pager-info"><b>${from}–${to}</b> of <b>${matching}</b></span>
      <label class="pager-size">Rows per page
        <select id="pageSize">${[25, 50, 100].map((n) => `<option ${n === state.pageSize ? "selected" : ""}>${n}</option>`).join("")}</select></label>
      <span style="flex:1"></span>
      <div class="pager-nav">
        <button class="btn minimal small" data-page="${state.page - 1}" ${state.page <= 1 ? "disabled" : ""} title="Previous page">‹ Prev</button>
        ${pageList(state.page, last).map((n) => n === "…" ? '<span class="pager-gap">…</span>'
          : `<button class="btn minimal small ${n === state.page ? "on" : ""}" data-page="${n}">${n}</button>`).join("")}
        <button class="btn minimal small" data-page="${state.page + 1}" ${state.page >= last ? "disabled" : ""} title="Next page">Next ›</button>
      </div>`;
    $$("[data-page]", el).forEach((b) => b.onclick = () => {
      state.page = +b.dataset.page;
      load().then(() => { $(".table-wrap").scrollTop = 0; });
    });
    $("#pageSize").onchange = (e) => { state.pageSize = +e.target.value; store.set("pageSize", state.pageSize); refilter(); };
  }

  function renderRows() {
    $("#empty").classList.toggle("hidden", roles.length > 0);
    $("#rows").innerHTML = roles.map((r) => {
      const fresh = isFresh(r);
      const cls = [r.id === selectedId ? "selected" : "", fresh && !r.archived ? "fresh" : "",
        ["not_a_fit", "withdrawn", "closed"].includes(r.status) ? "dim" : "", r.archived ? "archived" : ""].join(" ");
      return `
      <tr data-id="${r.id}" class="${cls}">
        <td>${statusTag(r.status, true)}${statusExtra(r)}</td>
        <td>${fitPill(r)}</td>
        <td><div class="title-wrap"><span class="cell-title" title="${esc(r.title)}">${esc(r.title)}</span>
          ${r.archived ? '<span class="tag mini archived">ARCHIVED</span>' : ""}
          ${fresh && !r.archived ? '<span class="tag mini new" title="Posted today or yesterday (job boards only give the date)">NEW</span>' : ""}
          ${(r.ai_tags || []).slice(0, 1).map((t) => `<span class="tag mini ai" title="${esc((r.ai_tags || []).join(", "))}">${esc(t.toUpperCase())}${r.ai_tags.length > 1 ? " +" + (r.ai_tags.length - 1) : ""}</span>`).join("")}</div></td>
        <td class="cell-muted" title="${esc(r.company)}">${esc(r.company)}</td>
        <td class="cell-muted" title="${esc(r.location)}">${r.is_remote ? '<span class="tag mini remote">REMOTE</span> ' : ""}${esc((r.location || "").replace(/^Remote\s*\(?|\)$/g, ""))}</td>
        <td class="num mono">${fmtPay(r)}</td>
        <td class="num mono">${fmtYears(r)}</td>
        <td class="mono cell-muted">${fmtDate(r.date_posted)}</td>
        <td class="mono">${r.applied_at ? fmtDate(r.applied_at) : '<span class="cell-dim">—</span>'}</td>
        <td>${r.resume_path ? `<span title="${esc(r.resume_path)}" class="cell-muted">📄</span>` : ""}</td>
      </tr>`;
    }).join("");
  }

  // ------------------------------------------------------------------ Drawer
  async function openRole(id) {
    saveNotesNow();
    selectedId = id;
    $$("#rows tr").forEach((tr) => tr.classList.toggle("selected", +tr.dataset.id === id));
    syncUrl();
    try {
      current = await api(`/api/roles/${id}`);
    } catch (e) {   // e.g. a ?role= link to a role that was merged or no longer exists
      if (selectedId === id) { selectedId = null; syncUrl(); }
      $$("#rows tr.selected").forEach((tr) => tr.classList.remove("selected"));
      toast(e.status === 404 ? "That role no longer exists." : e.message, "error");
      return;
    }
    if (selectedId !== id) return;   // another role was opened meanwhile
    renderDrawer();
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
  }

  function closeDrawer() {
    saveNotesNow();
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    selectedId = null; current = null;
    $$("#rows tr.selected").forEach((tr) => tr.classList.remove("selected"));
    syncUrl();
  }

  // The role named in the URL (?role=ID), e.g. after a refresh: open it.
  function openRoleFromUrl() {
    const id = parseInt(hashParams().get("role"), 10);
    if (id && id !== selectedId) openRole(id);
  }

  function resumeOptions(selected) {
    const known = RESUMES.some((f) => f.path === selected);
    return `<option value="">— none —</option>` +
      RESUMES.map((f) => `<option value="${esc(f.path)}" ${f.path === selected ? "selected" : ""}>${esc(f.name)}</option>`).join("") +
      (selected && !known ? `<option value="${esc(selected)}" selected>${esc(selected)}</option>` : "") +
      `<option value="__other__">Other path…</option>`;
  }

  function aiCard(r) {
    if (!r.ai_match) {
      return `<div class="card"><div class="card-head">AI screen</div><div class="card-body cell-dim" style="font-size:13px">
        Not screened yet. ${META.ai.configured ? "New roles are screened automatically after each pull; use Re-screen on the Preferences page for older ones."
        : 'Add an Anthropic API key to turn on AI screening (see <a href="#/preferences">Preferences → AI screening</a>).'}</div></div>`;
    }
    const lv = matchInfo(matchOf(r));
    return `<div class="card"><div class="card-head">AI screen</div><div class="card-body">
      <div class="ai-head">${fitPill(r)}<span class="cell-dim" style="font-size:12px">${esc(lv.desc)}</span></div>
      <div class="ai-reason">${esc(r.ai_reason || "")}</div>
      ${(r.ai_flags || []).length ? `<ul class="ai-flags">${r.ai_flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}
      ${(r.ai_tags || []).length ? `<div class="tagrow">${r.ai_tags.map((t) => `<span class="tag ai">${esc(t)}</span>`).join("")}</div>` : ""}
      <div class="ai-meta">${esc(r.ai_model || "")} · ${fmtDate(r.ai_screened_at, true)}</div>
    </div></div>`;
  }

  function renderDrawer() {
    const r = current;
    if (!r) return;
    const meta = r.metadata || {};
    const kv = [
      ["Location", (r.is_remote ? "Remote" : "") + (r.location && r.location !== "Remote" ? (r.is_remote ? " · " : "") + r.location : "")],
      ["Pay", fmtPay(r).replace(/<[^>]+>/g, "") || "—"],
      ["Experience", fmtYears(r).replace(/<[^>]+>/g, "")],
      ["Posted", r.date_posted ? fmtDate(r.date_posted) : "—"],
      ["Source", r.source || r.site],
      ["Level", r.job_level || meta.job_level || "—"],
      ["Job type", r.job_type || meta.job_type || "—"],
      ["Industry", meta.company_industry || "—"],
      ["Company size", meta.company_num_employees || "—"],
      ["Added to DB", fmtDate(r.first_seen, true)],
      ["Last updated", fmtDate(r.updated_at, true)],
      ["Role ID", `#${r.id}`],
    ];
    $("#drawer").innerHTML = `
      <div class="drawer-head">
        <div class="drawer-top">
          <div style="min-width:0">
            <div class="drawer-company">${esc(r.company)}</div>
            <div class="drawer-title">${esc(r.title)}</div>
          </div>
          <button class="btn minimal icon drawer-close" id="closeDrawer" title="Close (Esc)">✕</button>
        </div>
        <div class="drawer-links">
          ${statusTag(r.status)}
          ${isFresh(r) ? '<span class="tag new">Posted today or yesterday</span>' : ""}
          ${r.is_remote ? '<span class="tag remote">Remote</span>' : ""}
          <span style="flex:1"></span>
          <button class="btn small" id="archiveBtn" title="${r.archived ? "Show this role in default views again" : "Hide from default views and search"}">${r.archived ? "Unarchive" : "Archive"}</button>
          ${r.job_url ? `<a class="btn small" href="${esc(r.job_url)}" target="_blank" rel="noopener">Posting ↗</a>` : ""}
          ${r.job_url_direct && r.job_url_direct !== r.job_url ? `<a class="btn small" href="${esc(r.job_url_direct)}" target="_blank" rel="noopener">Company page ↗</a>` : ""}
        </div>
        ${r.archived ? `<div class="archived-banner">Archived ${fmtDate(r.archived_at)}. Hidden from the default view and search until you turn on “Include archived”.</div>` : ""}
      </div>
      <div class="drawer-body">
        <div class="card">
          <div class="card-head">Status <span class="save-state" id="saveState"></span></div>
          <div class="card-body">
            <div class="status-picker">
              ${Object.entries(STATUSES).map(([k, l]) => `<button class="s-${k} ${k === r.status ? "on" : ""} ${l.length > 16 ? "wide" : ""}" data-status="${k}"><span class="dot"></span>${esc(l)}</button>`).join("")}
            </div>
          </div>
        </div>

        ${offerCard(r)}
        ${roundsCard(r)}
        ${qaCard(r)}
        ${aiCard(r)}

        <div class="card">
          <div class="card-head">Application</div>
          <div class="card-body form-grid">
            <label class="field"><span>Applied on</span><input type="date" id="dApplied" value="${esc(r.applied_at || "")}"></label>
            <div class="field"><span>Resume used</span>
              <div class="resume-row">
                <select id="dResume" style="flex:1">${resumeOptions(r.resume_path)}</select>
                ${r.resume_path ? `<a class="btn small" style="height:30px" href="/api/file?path=${encodeURIComponent(r.resume_path)}" target="_blank" title="${esc(r.resume_path)}">Open</a>` : ""}
              </div>
            </div>
            <label class="field span2"><span>Notes</span><textarea id="dNotes" rows="3" placeholder="Recruiter name, referral, interview dates…">${esc(r.notes || "")}</textarea></label>
          </div>
        </div>

        <div class="card">
          <div class="card-head">Details <button class="btn minimal small" id="editBtn">Edit</button></div>
          <div class="card-body" id="detailsBody">
            <dl class="kv">${kv.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v || "—")}</dd>`).join("")}</dl>
          </div>
        </div>

        ${r.description ? `
        <div class="card" id="descCard">
          <div class="card-head">Description <span>
            <button class="btn minimal small" id="editDesc">Edit</button>
            <button class="btn minimal small" id="expandDesc">Expand</button></span></div>
          <div class="card-body" id="descBody"><div class="desc" id="desc">${renderMarkdown(r.description, state.q)}</div></div>
        </div>` : `
        <div class="card" id="descCard"><div class="card-head">Description <button class="btn minimal small" id="editDesc">+ Add description</button></div>
          <div class="card-body cell-dim" id="descBody" style="font-size:13px">No description was scraped for this posting.
            Open the posting, copy the job description and use <b>Add description</b> to paste it here.</div></div>`}

        <div class="card">
          <div class="card-head">History</div>
          <div class="card-body">
            <ul class="timeline">
              ${(r.history || []).map((h) => `
                <li class="s-${esc(h.status)}"><span class="tl-dot"></span>
                  <div><div>${esc(STATUSES[h.status] || h.status)}</div>${h.note ? `<div class="tl-note">${esc(h.note)}</div>` : ""}</div>
                  <span class="tl-time">${fmtDate(h.at, true)}</span></li>`).join("")}
            </ul>
          </div>
        </div>

        <div class="card"><details class="raw"><summary>All scraped metadata</summary><pre>${esc(JSON.stringify(meta, null, 2))}</pre></details></div>

        ${sameCompanyCard(r)}
      </div>`;
    wireDrawer();
  }

  // Your other roles at this employer (under any of its names), at the bottom of the panel.
  function sameCompanyCard(r) {
    const others = r.same_company || [];
    const place = (o) => {
      const loc = o.location && !/^remote$/i.test(o.location) ? o.location : "";
      return (o.is_remote ? "Remote" : "") + (o.is_remote && loc && !/^remote/i.test(loc) ? " · " : "") + (/^remote/i.test(loc) && o.is_remote ? "" : loc);
    };
    const rows = others.map((o) => `
      <li data-open-role="${o.id}" tabindex="0" title="Open this role">
        <div class="sc-main"><div class="sc-title">${esc(o.title)}${o.archived ? ' <span class="tag mini archived">ARCHIVED</span>' : ""}</div>
          <div class="sc-sub">${esc(place(o))}${o.company !== r.company ? `${place(o) ? " · " : ""}listed as ${esc(o.company)}` : ""}</div></div>
        <div class="sc-side">${statusTag(o.status, true)}${fitPill(o)}</div>
      </li>`).join("");
    return `<div class="card" id="sameCompanyCard">
      <div class="card-head"><span>Other roles at ${esc(r.company)}${others.length ? ` (${others.length})` : ""}</span>
        ${others.length ? `<button class="btn minimal small" id="filterCompany" title="Show only this employer's roles in the list">Show in list</button>` : ""}</div>
      <div class="card-body">${others.length ? `<ul class="same-co">${rows}</ul>`
        : '<div class="cell-dim" style="font-size:13px">No other roles at this company in your tracker.</div>'}</div>
    </div>`;
  }

  function setSaveState(text, ok) {
    const el = $("#saveState");
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("ok", !!ok);
  }

  // Notes autosave. The pending save remembers which role the text belongs to, and
  // is sent immediately when the panel closes or switches role, so text can't be
  // lost (Esc) or saved onto another role (clicking a different row).
  let notesPending = null;   // { id, value, timer }
  function saveNotesNow(opts = {}) {
    if (!notesPending) return Promise.resolve();
    const { id, value, timer } = notesPending;
    clearTimeout(timer);
    notesPending = null;
    if (current && current.id === id) setSaveState("Saving…");
    return api(`/api/roles/${id}`, { method: "PATCH", body: { notes: value }, ...opts }).then((r) => {
      if (current && current.id === id) { current = r; setSaveState("Saved", true); }
      const i = roles.findIndex((x) => x.id === id);
      if (i >= 0) { Object.assign(roles[i], r); renderRows(); }
    }).catch((e) => toast(`Couldn't save notes: ${e.message}`, "error"));
  }
  window.addEventListener("pagehide", () => saveNotesNow({ keepalive: true }));

  async function patch(changes, msg) {
    setSaveState("Saving…");
    try {
      current = await api(`/api/roles/${current.id}`, { method: "PATCH", body: changes });
      const i = roles.findIndex((x) => x.id === current.id);
      if (i >= 0) Object.assign(roles[i], current);
      renderRows();
      setSaveState("Saved", true);
      if (msg) toast(msg, "success");
      return true;
    } catch (e) {
      setSaveState("");
      toast(e.message, "error");
      return false;
    }
  }

  // Run an async UI action; on failure show the error and redraw from the last saved state.
  const guard = (fn) => async (...args) => {
    try { await fn(...args); }
    catch (e) { toast(e.message, "error"); if (current) renderDrawer(); }
  };

  function wireDrawer() {
    $("#closeDrawer").onclick = closeDrawer;
    $$(".status-picker button").forEach((b) => b.onclick = () => {
      if (b.dataset.status === current.status && b.dataset.status !== "interviewing") return;
      openStage(b.dataset.status);
    });
    $("#archiveBtn").onclick = guard(async () => {
      const archived = !current.archived;
      current = await api(`/api/roles/${current.id}/archive`, { method: "POST", body: { archived } });
      toast(archived ? `Archived “${current.title}”. It's hidden until you turn on “Include archived”.` : `Unarchived “${current.title}”`, "success");
      if (archived && !state.archived) closeDrawer(); else renderDrawer();
      load();
    });
    wireCards();
    $("#dApplied").onchange = (e) => patch({ applied_at: e.target.value || null });
    $("#dResume").onchange = async (e) => {
      let v = e.target.value;
      if (v === "__other__") {
        v = prompt("Full path to the resume file you used:", current.resume_path || "");
        if (v == null) { e.target.innerHTML = resumeOptions(current.resume_path); return; }
      }
      if (await patch({ resume_path: v || null })) renderDrawer();
    };
    $("#dNotes").oninput = (e) => {
      setSaveState("Editing…");
      const id = current.id;
      if (notesPending && notesPending.id !== id) saveNotesNow();
      if (notesPending) clearTimeout(notesPending.timer);
      notesPending = { id, value: e.target.value, timer: setTimeout(() => saveNotesNow(), 600) };
    };
    const ex = $("#expandDesc");
    if (ex) ex.onclick = () => { const d = $("#desc"); d.classList.toggle("expanded"); ex.textContent = d.classList.contains("expanded") ? "Collapse" : "Expand"; };
    $("#editDesc").onclick = renderDescEditor;
    $("#editBtn").onclick = renderEditDetails;
    $$("#sameCompanyCard [data-open-role]").forEach((li) => {
      const go = () => openRole(+li.dataset.openRole);
      li.onclick = go;
      li.onkeydown = (e) => { if (e.key === "Enter") go(); };
    });
    const fc = $("#filterCompany");
    if (fc) fc.onclick = () => {
      // The panel lists archived matches too, so include archived roles if any are among them.
      const anyArchived = (current.same_company || []).some((o) => o.archived);
      // The shortest of the employer's names ("Globex", not "Globex.com Services LLC") still
      // matches every one of them, and reads better on the chip.
      const name = [current.company, ...(current.same_company || []).map((o) => o.company)]
        .reduce((a, b) => (b.length < a.length ? b : a));
      Object.assign(state, { companies: [name], page: 1 }, anyArchived ? { archived: true } : {});
      refilter();
      toast(`Showing roles at ${name}. Remove it under Company in the filters.`, "success");
    };
  }

  // ------------------------------------------------------------------ Offer / interviews / Q&A cards
  const roundKind = (k) => (META.round_kinds || {})[k] || k || "Interview";
  const roundStatus = (s) => (META.round_statuses || {})[s] || s;

  function offerCard(r) {
    if (r.status !== "offer" && !r.offer_deadline && !r.offer_salary && !r.offer_details) return "";
    let due = "—", dueCls = "";
    if (r.offer_deadline) {
      const d = daysUntil(r.offer_deadline);
      due = `${fmtDate(r.offer_deadline)}`;
      dueCls = d < 0 ? "due-past" : d <= 3 ? "due-soon" : "";
      due += ` <span class="cell-dim" style="font-size:12px">(${d < 0 ? `${-d}d ago` : d === 0 ? "today" : `in ${d}d`})</span>`;
    }
    return `<div class="card"><div class="card-head">Offer <button class="btn minimal small" data-stage="offer">Edit</button></div>
      <div class="card-body"><div class="offer-grid">
        <div class="offer-stat"><div class="v ${dueCls}">${due}</div><div class="l">Respond by</div></div>
        <div class="offer-stat"><div class="v">${r.offer_salary ? money(r.offer_salary) : "—"}</div><div class="l">Final salary</div></div>
      </div>${r.offer_details ? `<div class="qa-a" style="margin-top:10px">${esc(r.offer_details)}</div>` : ""}</div></div>`;
  }

  function roundsCard(r) {
    const rounds = r.rounds || [];
    // Shown once you've applied, so the first step (e.g. a technical screen) can be added from there.
    if (!rounds.length && !["applied", "interviewing", "offer"].includes(r.status)) return "";
    const opts = (cur) => Object.entries(META.round_statuses || {}).map(([k, l]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(l)}</option>`).join("");
    const when = (x) => {
      if (!x.scheduled_on) return "";
      const d = daysUntil(x.scheduled_on);
      const rel = x.status !== "scheduled" ? "" : d < 0 ? ` (${-d}d overdue)` : d === 0 ? " (today)" : ` (in ${d}d)`;
      return `${fmtDate(x.scheduled_on)}${rel} · `;
    };
    return `<div class="card"><div class="card-head">Steps <button class="btn minimal small" data-stage="interviewing">+ Add step</button></div>
      <div class="card-body">${rounds.length ? `<ul class="rounds">${rounds.map((x) => `
        <li class="r-${esc(x.status)}"><span class="round-num">${x.round_number}</span>
          <div class="round-main"><div class="round-title">${esc(roundKind(x.kind))}</div>
            <div class="round-sub">${when(x)}${esc(roundStatus(x.status))}${x.notes ? " · " + esc(x.notes) : ""}</div></div>
          <div class="round-actions">${x.status === "scheduled"
            ? `<button class="btn small" data-round-done="${x.id}" title="Done, waiting to hear back">Mark done</button>` : ""}
            <select data-round-status="${x.id}" title="Change where this step stands">${opts(x.status)}</select>
            <button class="btn minimal small" data-round-del="${x.id}" title="Delete step">✕</button></div></li>`).join("")}</ul>`
        : `<div class="cell-dim" style="font-size:13px">No steps yet. Use <b>+ Add step</b> when you get a recruiter call, technical screen, take-home or interview.</div>`}</div></div>`;
  }

  function qaCard(r) {
    const qa = r.qa || [];
    const rounds = r.rounds || [];
    const groups = [["Application", qa.filter((q) => !q.round_id)]].concat(
      rounds.map((x) => [`${x.round_number}. ${roundKind(x.kind)}`, qa.filter((q) => q.round_id === x.id)]));
    const body = groups.filter(([, items]) => items.length).map(([label, items]) => `
      <div class="qa-group-label">${esc(label)}</div>
      ${items.map((q) => `<div class="qa-item" data-qa="${q.id}">
        <div class="qa-tools"><button class="btn minimal small" data-qa-edit="${q.id}">Edit</button><button class="btn minimal small" data-qa-del="${q.id}">✕</button></div>
        <div class="qa-q">${esc(q.question)}</div><div class="qa-a ${q.answer ? "" : "empty"}">${esc(q.answer || "No answer saved")}</div></div>`).join("")}`).join("");
    return `<div class="card"><div class="card-head">Questions & answers <span class="cell-dim" style="text-transform:none;letter-spacing:0;font-weight:500">${qa.length || ""}</span></div>
      <div class="card-body"><div class="qa-list">${body || '<div class="cell-dim" style="font-size:13px">Save application and interview questions here so you can prep for the next round.</div>'}</div>
      <form id="qaAdd" class="qa-row" style="margin-top:12px">
        <div style="display:flex;flex-direction:column;gap:6px">
          <label class="field"><input name="question" placeholder="+ Add a question you were asked"></label>
          <label class="field hidden" data-qa-more><textarea name="answer" rows="2" placeholder="Your answer (or what you'd say next time)"></textarea></label>
          <div class="inline hidden" data-qa-more>${rounds.length ? `<label class="field" style="max-width:240px"><select name="round_id"><option value="">Application</option>${rounds.map((x) => `<option value="${x.id}">${x.round_number}. ${esc(roundKind(x.kind))}</option>`).join("")}</select></label>` : ""}
            <button class="btn primary small" style="height:30px">Add</button></div>
        </div></form></div></div>`;
  }

  function wireCards() {
    $$("[data-stage]", $("#drawer")).forEach((b) => b.onclick = () => openStage(b.dataset.stage));
    $$("[data-round-status]").forEach((s) => s.onchange = guard(async () => {
      const status = s.value;
      let body = { status };
      if (status === "passed") {
        const r = current.rounds.find((x) => x.id === +s.dataset.roundStatus);
        const later = current.rounds.some((x) => x.round_number > r.round_number);
        if (!later && confirm(`Passed the ${roundKind(r.kind).toLowerCase()}. Add the next step now?`)) {
          return openStage("interviewing", { roundId: r.id, presetStatus: "passed", createNext: true });
        }
      }
      current = await api(`/api/rounds/${s.dataset.roundStatus}`, { method: "PATCH", body });
      renderDrawer(); load();
    }));
    // One click from "to do" to "done, waiting to hear back".
    $$("[data-round-done]").forEach((b) => b.onclick = guard(async () => {
      current = await api(`/api/rounds/${b.dataset.roundDone}`, { method: "PATCH", body: { status: "waiting" } });
      renderDrawer(); load();
    }));
    $$("[data-round-del]").forEach((b) => b.onclick = guard(async () => {
      if (!confirm("Delete this step? Questions saved under it are kept under Application.")) return;
      current = await api(`/api/rounds/${b.dataset.roundDel}`, { method: "DELETE" });
      renderDrawer(); load();
    }));
    $$("[data-qa-del]").forEach((b) => b.onclick = guard(async () => {
      if (!confirm("Delete this question?")) return;
      current = await api(`/api/qa/${b.dataset.qaDel}`, { method: "DELETE" }); renderDrawer();
    }));
    $$("[data-qa-edit]").forEach((b) => b.onclick = () => {
      const q = current.qa.find((x) => x.id === +b.dataset.qaEdit);
      const item = $(`.qa-item[data-qa="${q.id}"]`);
      item.innerHTML = `<form class="qa-edit" style="display:flex;flex-direction:column;gap:6px">
        <label class="field"><input name="question" value="${esc(q.question)}"></label>
        <label class="field"><textarea name="answer" rows="3">${esc(q.answer || "")}</textarea></label>
        <div style="display:flex;gap:6px;justify-content:flex-end"><button type="button" class="btn small" data-cancel>Cancel</button><button class="btn primary small">Save</button></div></form>`;
      $("[data-cancel]", item).onclick = renderDrawer;
      $("form", item).onsubmit = (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        // On failure the edit form stays as typed, so nothing is lost.
        api(`/api/qa/${q.id}`, { method: "PATCH", body: { question: fd.get("question"), answer: fd.get("answer") } })
          .then((r) => { current = r; renderDrawer(); })
          .catch((err) => toast(err.message, "error"));
      };
    });
    const add = $("#qaAdd");
    if (add) {
      add.question.addEventListener("focus", () => $$("[data-qa-more]", add).forEach((el) => el.classList.remove("hidden")));
      add.onsubmit = (e) => {
        e.preventDefault();
        const fd = new FormData(add);
        if (!fd.get("question").trim()) return;
        // On failure the form stays as typed, so nothing is lost.
        api(`/api/roles/${current.id}/qa`, { method: "POST", body: {
          question: fd.get("question"), answer: fd.get("answer"), round_id: fd.get("round_id") ? +fd.get("round_id") : null } })
          .then((r) => {
            current = r;
            renderDrawer();
            const inp = $("#qaAdd input[name=question]"); if (inp) inp.focus();
          }).catch((err) => toast(err.message, "error"));
      };
    }
  }

  // ------------------------------------------------------------------ Status-change popup
  // One dialog; the fields change with the status being moved to.
  const STAGE_COPY = {
    not_applied: "Moving back to Not applied.",
    interested: "Saving this one for later.",
    applied: "Nice! Log what you submitted and any questions you were asked.",
    interviewing: "Add or update a step: what kind it is (recruiter call, technical screen, take-home, onsite…), when, and where it stands.",
    offer: "Congrats! Capture the offer details so you don't miss the deadline.",
    rejected: "Sorry. Note where it ended so you can learn from it.",
    withdrawn: "Why are you withdrawing?",
    closed: "The posting is closed or no longer accepting applications.",
    not_a_fit: "Why isn't this a fit? (helps tune your preferences)",
  };
  let stage = null;

  // Same rule as the server (db.company_blocked): same name, or the blocked name plus more words.
  const companyKey = (s) => String(s || "").toLowerCase().replace(/[^\w\s&]/g, " ").replace(/\s+/g, " ").trim();
  function isCompanyBlocked(company) {
    const c = companyKey(company);
    return !!c && (META.blocked_companies || []).some((b) => { b = companyKey(b); return b && (c === b || c.startsWith(b + " ")); });
  }

  function qaRowsHtml(rows) {
    return rows.map((q, i) => `<div class="qa-row" data-i="${i}">
      <div style="display:flex;flex-direction:column;gap:6px">
        <label class="field"><input data-q="${i}" value="${esc(q.question)}" placeholder="Question"></label>
        <label class="field"><textarea data-a="${i}" rows="2" placeholder="Your answer">${esc(q.answer)}</textarea></label>
      </div><button type="button" class="btn minimal small rm" data-qrm="${i}" title="Remove">✕</button></div>`).join("");
  }

  function openStage(target, opts = {}) {
    const r = current;
    const rounds = r.rounds || [];
    const last = rounds[rounds.length - 1];
    stage = {
      target, qa: [],
      roundMode: opts.roundId ? String(opts.roundId) : (last && last.status === "scheduled" && target === "interviewing" ? String(last.id) : "new"),
      round: { kind: "", scheduled_on: localISO(new Date()), status: opts.presetStatus || "scheduled", create_next: !!opts.createNext,
        next: { kind: "", scheduled_on: "" } },
      rejectedRound: "",
      blockCompany: false,
    };
    if (stage.roundMode !== "new") {
      const x = rounds.find((y) => String(y.id) === stage.roundMode);
      if (x) Object.assign(stage.round, { kind: x.kind || "", scheduled_on: x.scheduled_on || "", status: opts.presetStatus || (x.status === "scheduled" ? "waiting" : x.status) });
    }
    if (["applied", "interviewing"].includes(target)) stage.qa = [{ question: "", answer: "" }];
    renderStage();
    $("#stageDialog").classList.remove("hidden");
  }

  function renderStage() {
    const r = current, t = stage.target;
    const rounds = r.rounds || [];
    const nextNum = (rounds.reduce((m, x) => Math.max(m, x.round_number), 0) || 0) + 1;
    $("#stageTitle").innerHTML = t === r.status
      ? `<span class="stage-flow">Update · ${statusTag(t)}</span>`
      : `<span class="stage-flow">${statusTag(r.status)}<span class="arrow">→</span>${statusTag(t)}</span>`;
    const kinds = Object.entries(META.round_kinds || {});
    const pills = (name, items, cur, cls = "") => `<div class="seg-pills" data-pills="${name}">${items.map(([v, l]) =>
      `<button type="button" class="${cls ? cls + v : ""} ${v === cur ? "on" : ""}" data-v="${v}">${cls ? '<span class="dot"></span>' : ""}${esc(l)}</button>`).join("")}</div>`;
    let html = `<div class="stage-help" style="margin-bottom:14px">${esc(STAGE_COPY[t] || "")}</div>`;

    if (t === "applied") {
      html += `<div class="stage-section form-grid">
        <label class="field"><span>Applied on</span><input type="date" name="applied_at" value="${esc(r.applied_at || localISO(new Date()))}"></label>
        <label class="field"><span>Resume used</span><select name="resume_path">${RESUMES.map((f, i) => `<option value="${esc(f.path)}" ${(r.resume_path ? f.path === r.resume_path : i === 0) ? "selected" : ""}>${esc(f.name)}</option>`).join("")}<option value="">— none —</option></select></label></div>`;
    }
    if (t === "interviewing") {
      const sel = stage.roundMode;
      html += `<div class="stage-section"><div class="stage-label">Step</div>
        ${rounds.length ? `<div class="seg-pills" data-pills="roundMode" style="margin-bottom:10px">
          ${rounds.map((x) => `<button type="button" class="${String(x.id) === sel ? "on" : ""}" data-v="${x.id}">Update: ${esc(roundKind(x.kind))}</button>`).join("")}
          <button type="button" class="${sel === "new" ? "on" : ""}" data-v="new">+ New step</button></div>` : ""}
        <div class="form-grid">
          <label class="field"><span>Type</span><select name="round_kind"><option value="">Select…</option>${kinds.map(([v, l]) => `<option value="${v}" ${v === stage.round.kind ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
          <label class="field"><span>Date or deadline</span><input type="date" name="round_date" value="${esc(stage.round.scheduled_on || "")}"></label>
        </div>
        <div class="stage-label" style="margin-top:12px">Where it stands</div>
        ${pills("roundStatus", Object.entries(META.round_statuses || {}), stage.round.status, "r-")}
        ${stage.round.status === "passed" ? `<div class="next-round">
          <label class="switch" style="padding:0 0 6px"><input type="checkbox" name="create_next" ${stage.round.create_next ? "checked" : ""}><span class="track"></span><span>Got the next step: add it now</span></label>
          ${stage.round.create_next ? `<div class="form-grid"><label class="field"><span>Next step type</span><select name="next_kind"><option value="">Select…</option>${kinds.map(([v, l]) => `<option value="${v}" ${v === stage.round.next.kind ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>
            <label class="field"><span>Next step date</span><input type="date" name="next_date" value="${esc(stage.round.next.scheduled_on || "")}"></label></div>` : ""}
        </div>` : ""}</div>`;
    }
    if (t === "offer") {
      html += `<div class="stage-section form-grid">
        <label class="field"><span>Deadline to respond</span><input type="date" name="offer_deadline" value="${esc(r.offer_deadline || "")}"></label>
        <label class="field"><span>Final salary ($/yr)</span><input type="number" step="1000" name="offer_salary" value="${r.offer_salary ?? ""}" placeholder="e.g. 185000"></label>
        <label class="field span2"><span>Other details</span><textarea name="offer_details" rows="2" placeholder="Bonus, equity, sign-on, start date…">${esc(r.offer_details || "")}</textarea></label></div>`;
    }
    if (t === "not_a_fit" && r.company) {
      const blocked = isCompanyBlocked(r.company);
      html += `<div class="stage-section">${blocked
        ? `<div class="cell-dim" style="font-size:13px"><b>${esc(r.company)}</b> is already on your blocked companies list.</div>`
        : `<label class="switch"><input type="checkbox" name="block_company" ${stage.blockCompany ? "checked" : ""}><span class="track"></span>
            <span>Also block <b>${esc(r.company)}</b>: skip its roles in future pulls (you can undo this in Preferences → Blocked companies)</span></label>`}</div>`;
    }
    if (t === "rejected" && rounds.length) {
      html += `<div class="stage-section"><div class="stage-label">Rejected after</div>
        ${pills("rejectedRound", [["", "Application"], ...rounds.map((x) => [String(x.id), `${x.round_number}. ${roundKind(x.kind)}`])], stage.rejectedRound)}</div>`;
    }
    if (["applied", "interviewing"].includes(t)) {
      html += `<div class="stage-section"><div class="stage-label">${t === "applied" ? "Application / screening questions" : "Questions you were asked"}</div>
        <div class="qa-editor">${qaRowsHtml(stage.qa)}<button type="button" class="btn small add-row" id="qaPlus">+ Add question</button></div></div>`;
    }
    html += `<div class="stage-section"><label class="field"><span>Notes (saved to the role's history)</span>
      <textarea name="note" rows="3" placeholder="Anything to remember: who you talked to, next steps, impressions…">${esc(stage.note || "")}</textarea></label></div>`;
    $("#stageBody").innerHTML = html;
    $("#stageSkip").classList.toggle("hidden", t === r.status);
    wireStage();
  }

  function captureStage() {
    const f = $("#stageForm");
    const val = (n) => (f.elements[n] ? f.elements[n].value : undefined);
    stage.note = val("note") ?? stage.note;
    if (f.elements.block_company) stage.blockCompany = f.elements.block_company.checked;
    if (f.elements.round_kind) {
      stage.round.kind = val("round_kind"); stage.round.scheduled_on = val("round_date");
      if (f.elements.create_next) stage.round.create_next = f.elements.create_next.checked;
      if (f.elements.next_kind) { stage.round.next.kind = val("next_kind"); stage.round.next.scheduled_on = val("next_date"); }
    }
    $$("[data-q]", f).forEach((el) => { stage.qa[+el.dataset.q].question = el.value; });
    $$("[data-a]", f).forEach((el) => { stage.qa[+el.dataset.a].answer = el.value; });
  }

  function wireStage() {
    const f = $("#stageForm");
    $$("[data-pills]", f).forEach((g) => g.onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      captureStage();
      const name = g.dataset.pills, v = b.dataset.v;
      if (name === "roundStatus") stage.round.status = v;
      if (name === "rejectedRound") stage.rejectedRound = v;
      if (name === "roundMode") {
        stage.roundMode = v;
        const x = (current.rounds || []).find((y) => String(y.id) === v);
        Object.assign(stage.round, x ? { kind: x.kind || "", scheduled_on: x.scheduled_on || "", status: x.status === "scheduled" ? "waiting" : x.status }
          : { kind: "", scheduled_on: localISO(new Date()), status: "scheduled" });
      }
      renderStage();
    });
    if (f.elements.create_next) f.elements.create_next.onchange = () => { captureStage(); renderStage(); };
    const plus = $("#qaPlus");
    if (plus) plus.onclick = () => { captureStage(); stage.qa.push({ question: "", answer: "" }); renderStage(); $$("[data-q]").pop()?.focus(); };
    $$("[data-qrm]", f).forEach((b) => b.onclick = () => { captureStage(); stage.qa.splice(+b.dataset.qrm, 1); renderStage(); });
  }

  async function submitStage(skip) {
    captureStage();
    const f = $("#stageForm"), t = stage.target, r = current;
    const body = { status: t };
    if (!skip) {
      body.note = stage.note;
      if (t === "applied") { body.applied_at = f.elements.applied_at.value; body.resume_path = f.elements.resume_path.value; }
      if (t === "offer") {
        body.offer_deadline = f.elements.offer_deadline.value;
        body.offer_salary = f.elements.offer_salary.value ? Number(f.elements.offer_salary.value) : null;
        body.offer_details = f.elements.offer_details.value;
      }
      if (t === "interviewing") {
        body.round = { kind: stage.round.kind, scheduled_on: stage.round.scheduled_on, status: stage.round.status,
          create_next: stage.round.status === "passed" && stage.round.create_next, next: stage.round.next };
        if (stage.roundMode !== "new") body.round.id = +stage.roundMode;
      }
      if (t === "rejected" && stage.rejectedRound) body.round = { id: +stage.rejectedRound, status: "rejected" };
      body.qa = stage.qa.filter((q) => q.question.trim());
    }
    try {
      current = await api(`/api/roles/${r.id}/transition`, { method: "POST", body });
      $("#stageDialog").classList.add("hidden");
      toast(t === r.status ? "Updated" : `Marked “${current.title}” as ${STATUSES[t]}`, "success");
      renderDrawer(); load();
    } catch (e) { toast(e.message, "error"); return; }
    // Applies with "Skip details" too: it's a separate choice, not a detail of the status.
    if (t === "not_a_fit" && stage.blockCompany && r.company) {
      try {
        const res = await api("/api/blocked-companies", { method: "POST", body: { company: r.company } });
        META.blocked_companies = res.blocked;
        toast(`Blocked ${r.company}. Future pulls will skip its roles.`, "success");
      } catch (e) { toast(`Couldn't block ${r.company}: ${e.message}`, "error"); }
    }
  }

  // Add or edit the description yourself (e.g. when the scrape didn't get it),
  // then optionally have the AI screen this one role against the new text.
  function renderDescEditor() {
    const r = current;
    const aiOn = META.ai.configured && META.ai_enabled;
    $("#editDesc").classList.add("hidden");
    const ex = $("#expandDesc"); if (ex) ex.classList.add("hidden");
    const body = $("#descBody");
    body.className = "card-body";
    body.removeAttribute("style");
    body.innerHTML = `<form id="descForm" style="display:flex;flex-direction:column;gap:8px">
        <label class="field"><textarea name="description" rows="12" placeholder="Paste the job description from the posting">${esc(r.description || "")}</textarea></label>
        ${aiOn ? `<label class="switch"><input type="checkbox" name="rescreen" checked><span class="track"></span>
          <span>Screen this role with AI using this description (about $0.0005)</span></label>`
        : `<div class="cell-dim" style="font-size:12px">AI screening is off, so the fit won't be updated.</div>`}
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button type="button" class="btn" id="cancelDesc">Cancel</button>
          <button class="btn primary">Save</button></div></form>`;
    const form = $("#descForm");
    form.description.focus();
    $("#cancelDesc").onclick = renderDrawer;
    form.onsubmit = async (e) => {
      e.preventDefault();
      const text = form.description.value;
      const rescreen = aiOn && form.rescreen.checked && text.trim();
      const id = r.id, had = !!r.description;
      const btn = $("button.primary", form); btn.disabled = true;
      if (!(await patch({ description: text, note: had ? "Description edited" : "Description added by you" }))) {
        btn.disabled = false;   // error already shown; keep the text so nothing is lost
        return;
      }
      if (!rescreen) { renderDrawer(); toast("Description saved", "success"); return; }
      renderDrawer();
      setSaveState("AI screening…");
      try {
        const res = await api(`/api/roles/${id}/screen`, { method: "POST" });
        if (current && current.id === id) { current = res.role; renderDrawer(); }
        const lv = matchInfo(res.role.ai_match);
        toast(`Description saved. Assessed fit: ${lv.label}${res.role.ai_reason ? ` (${res.role.ai_reason})` : ""}`, "success");
        load();
      } catch (err) {
        if (current && current.id === id) setSaveState("");
        toast(`Description saved, but the AI screen didn't run: ${err.message}`, "error");
      }
    };
  }

  function renderEditDetails() {
    const r = current;
    const f = (name, label, type = "text", span = "") =>
      `<label class="field ${span}"><span>${label}</span><input name="${name}" type="${type}" value="${esc(r[name] ?? "")}"></label>`;
    $("#detailsBody").innerHTML = `
      <form class="form-grid" id="editForm">
        ${f("title", "Title", "text", "span2")}${f("company", "Company")}${f("location", "Location")}
        ${f("job_url", "Posting URL", "url", "span2")}
        ${f("pay_min", "Pay min", "number")}${f("pay_max", "Pay max", "number")}
        ${f("years_min", "Years min", "number")}${f("years_max", "Years max", "number")}
        <label class="switch span2"><input type="checkbox" name="is_remote" ${r.is_remote ? "checked" : ""}><span class="track"></span><span>Remote</span></label>
        <div class="span2" style="display:flex;gap:8px;justify-content:flex-end">
          <button type="button" class="btn" id="cancelEdit">Cancel</button>
          <button class="btn primary">Save</button>
        </div>
      </form>`;
    $("#cancelEdit").onclick = renderDrawer;
    $("#editForm").onsubmit = async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const body = {};
      for (const k of ["title", "company", "location", "job_url"]) body[k] = fd.get(k);
      for (const k of ["pay_min", "pay_max", "years_min", "years_max"]) body[k] = fd.get(k) === "" ? null : Number(fd.get(k));
      body.is_remote = fd.get("is_remote") ? 1 : 0;
      if (await patch(body, "Details updated")) renderDrawer();
    };
  }

  // ------------------------------------------------------------------ Add dialog
  // Two ways to add a role: "From a link" (read the posting: job boards, any public careers
  // page, optionally with AI; you check the filled-in form, then add) or "Enter manually".
  const aiAvailable = () => META.ai.configured && META.ai_enabled;
  let importSeq = 0;
  let ruleNote = "";   // why the read link was preset to Not a fit (saved in the role's history)
  function openAdd() {
    const form = $("#addForm");
    form.reset();
    importSeq++;   // forget an import still running from last time
    $("#addStatus").innerHTML = Object.entries(STATUSES).map(([k, l]) => `<option value="${k}" ${k === "not_applied" ? "selected" : ""}>${esc(l)}</option>`).join("");
    ruleNote = "";
    $("#addResume").innerHTML = `<option value="">— none —</option>` + RESUMES.map((f, i) => `<option value="${esc(f.path)}" ${i === 0 ? "selected" : ""}>${esc(f.name)}</option>`).join("");
    $("#dupeBox").classList.add("hidden");
    $$("#addFields .autofilled").forEach((el) => el.classList.remove("autofilled"));
    $("#importUrl").value = "";
    setImportStatus("");
    const ai = aiAvailable();
    $("#importAi").checked = ai && store.get("importAi", true);
    $("#importAi").disabled = !ai;
    $("#importAiNote").textContent = ai ? "Fills in fields through job board APIs and AI if needed (about $0.001)"
      : (META.ai.configured ? "AI is turned off in Preferences" : "Needs an Anthropic API key (Preferences → AI screening)");
    form.screen_after.checked = ai;
    form.screen_after.disabled = !ai;
    $("#addScreenWrap").classList.toggle("hidden", !ai);
    setAddMode(store.get("addMode", "link"), true);
    $("#addDialog").classList.remove("hidden");
  }
  const closeAdd = () => { importSeq++; $("#addDialog").classList.add("hidden"); };

  // In "From a link" mode the form stays hidden until the link has been read (or you choose
  // to fill it in yourself), so an empty form can't be submitted by accident.
  let addFilled = false;
  function setAddMode(mode, fresh) {
    if (fresh) addFilled = false;
    store.set("addMode", mode);
    $$("#addMode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
    const link = mode === "link";
    $("#importPanel").classList.toggle("hidden", !link);
    const showForm = !link || addFilled;
    $("#addFields").classList.toggle("hidden", !showForm);
    $("#addSubmit").classList.toggle("hidden", !showForm);
    setTimeout(() => (link && !addFilled ? $("#importUrl") : $("#addForm").title).focus(), 30);
  }

  function setImportStatus(html, kind = "") {
    const el = $("#importStatus");
    el.innerHTML = html;
    el.className = `import-status ${kind} ${html ? "" : "hidden"}`;
  }

  const IMPORT_SOURCES = { board: "the job board", structured: "the page's job data", page: "the page text", ai: "AI" };
  async function importFromLink() {
    const url = $("#importUrl").value.trim();
    if (!url) { setImportStatus("Paste the link to a job posting first.", "error"); $("#importUrl").focus(); return; }
    const useAi = $("#importAi").checked && !$("#importAi").disabled;
    store.set("importAi", $("#importAi").checked);
    const seq = ++importSeq, btn = $("#importBtn");
    btn.disabled = true; btn.classList.add("working"); btn.textContent = "Reading the page…";
    setImportStatus(useAi ? "Reading the page, then asking the AI. This should take about 30 seconds."
      : "Reading the page. Pages built with JavaScript can take a few seconds more.");
    try {
      const r = await api("/api/import", { method: "POST", body: { url, use_ai: useAi } });
      if (seq !== importSeq) return;   // the dialog was closed or reopened meanwhile
      fillAddForm(r.fields);
      // New roles start as Not applied, unless the posting breaks one of your must-have
      // preferences (the same rules a pull uses): then Not a fit, which you can change.
      const status = $("#addStatus");
      status.value = r.not_a_fit ? "not_a_fit" : "not_applied";
      status.closest("label").classList.toggle("autofilled", !!r.not_a_fit);
      ruleNote = r.not_a_fit ? `Not a fit by your preferences: ${r.not_a_fit}` : "";
      const used = [...new Set(Object.values(r.sources || {}))].map((x) => IMPORT_SOURCES[x] || x);
      const n = Object.keys(r.fields).filter((k) => !["job_url", "site"].includes(k)).length;
      const cost = r.ai && r.ai.used ? ` AI cost ${dollars(r.ai.cost, 4)}.` : "";
      const verdict = r.not_a_fit ? `<div class="import-verdict"><b>Set to Not a fit:</b> ${esc(r.not_a_fit)} (your preferences). Change the status if you still want it.</div>` : "";
      setImportStatus(verdict + `<b>Filled in ${n} field${n === 1 ? "" : "s"}</b> from ${used.length > 1 ? used.slice(0, -1).join(", ") + " and " + used.at(-1) : used[0] || "the page"}.${cost} Check them below, then add the role.`
        + (r.messages && r.messages.length ? `<ul>${r.messages.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""));
      addFilled = true;
      setAddMode("link");
      checkDupes();
    } catch (e) {
      if (seq !== importSeq) return;
      setImportStatus(`${esc(e.message)} <a href="#" id="importManual">Enter it manually instead</a>`, "error");
      const m = $("#importManual");
      if (m) m.onclick = (ev) => { ev.preventDefault(); $("#addForm").job_url.value = url; setAddMode("manual"); };
    } finally {
      if (seq === importSeq) { btn.disabled = false; btn.classList.toggle("working", false); btn.textContent = "Fill in from link"; }
    }
  }

  function fillAddForm(fields) {
    const form = $("#addForm");
    $$("#addFields .autofilled").forEach((el) => el.classList.toggle("autofilled", false));
    for (const [k, v] of Object.entries(fields)) {
      const el = form.elements[k];
      if (!el || v == null || v === "") continue;
      if (el.type === "checkbox") el.checked = !!v;
      else el.value = v;
      const label = el.closest("label.field, label.switch");
      if (label && el.type !== "hidden") label.classList.add("autofilled");
    }
  }

  let dupeT;
  async function checkDupes() {
    const form = $("#addForm");
    const company = form.company.value.trim(), url = form.job_url.value.trim();
    if (!company && !url) { $("#dupeBox").classList.add("hidden"); return; }
    const { matches } = await api(`/api/check?company=${encodeURIComponent(company)}&url=${encodeURIComponent(url)}`);
    const box = $("#dupeBox");
    if (!matches.length) { box.classList.add("hidden"); return; }
    box.innerHTML = `<b>Already in your database at this company:</b><ul>${matches.map((m) =>
      `<li><a data-open="${m.id}">${esc(m.title)}</a> · ${esc(m.company)} · ${esc(STATUSES[m.status])}${m.applied_at ? " " + fmtDate(m.applied_at) : ""}</li>`).join("")}</ul>`;
    box.classList.remove("hidden");
    $$("[data-open]", box).forEach((a) => a.onclick = () => { closeAdd(); openRole(+a.dataset.open); });
  }

  async function submitAdd(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = {};
    for (const [k, v] of fd.entries()) body[k] = v;
    for (const k of ["pay_min", "pay_max", "years_min", "years_max"]) body[k] = body[k] === "" ? null : Number(body[k]);
    body.is_remote = fd.get("is_remote") ? 1 : 0;
    const screen = !!body.screen_after && aiAvailable() && (body.description || "").trim();
    if (ruleNote && body.status === "not_a_fit") body.status_note = ruleNote;
    body.screen_after = undefined;   // not a role field (JSON leaves it out)
    try {
      const r = await api("/api/roles", { method: "POST", body });
      closeAdd();
      toast(`Added “${r.title}” at ${r.company}${screen ? ". AI screening it…" : ""}`, "success");
      await load();
      openRole(r.id);
      if (screen) {
        try {
          const res = await api(`/api/roles/${r.id}/screen`, { method: "POST" });
          if (current && current.id === r.id) { current = res.role; renderDrawer(); }
          const lv = matchInfo(res.role.ai_match);
          toast(`Assessed fit: ${lv.label}${res.role.ai_reason ? ` (${res.role.ai_reason})` : ""}`, "success");
          load();
        } catch (err) { toast(`Added, but the AI screen didn't run: ${err.message}`, "error"); }
      }
    } catch (err) {
      if (err.status === 409 && err.data && err.data.id) {
        closeAdd();
        toast("That role is already in your database. Opening it.", "info");
        openRole(err.data.id);
      } else toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------------ Pull / re-screen tasks
  let pollT = null;
  // "Don't show this again" on the pull confirmation is remembered in this browser;
  // Preferences → AI screening → "Ask before each pull" turns it back on.
  const askBeforePull = () => !store.get("skipPullConfirm", false);

  async function startScrape() {
    if (!askBeforePull()) return runPull();
    let runs = { month_cost: 0, runs: [] };
    try { runs = await api("/api/runs"); } catch { /* ignore */ }
    const lastPull = runs.runs.find((r) => r.kind === "pull" && r.status !== "running");
    const aiOn = META.ai.configured && META.ai_enabled;
    const ok = await confirmDialog({
      title: "Pull new roles?",
      ok: "Pull now",
      body: `
        <div class="callout warn" style="margin-bottom:0">
          <svg width="16" height="16" viewBox="0 0 16 16"><path fill="currentColor" d="M8 1 15 14H1L8 1Zm-.75 5v4h1.5V6h-1.5Zm0 5v1.5h1.5V11h-1.5Z"/></svg>
          <div><b>This incurs LLM cost, I recommend running this max once a day.</b><br>
          New roles are screened by AI (${esc(META.model || "claude-haiku-5-5")}) after scraping, roughly $0.0005 per role.</div>
        </div>
        <dl class="confirm-meta">
          <dt>Last pull</dt><dd>${lastPull ? `${ago(lastPull.finished_at || lastPull.started_at)} · +${lastPull.added ?? 0} roles` : "never"}</dd>
          <dt>AI spend this month</dt><dd>${dollars(runs.month_cost, 4)}</dd>
          <dt>AI screening</dt><dd>${aiOn ? '<span class="ok-text">on</span>' : '<span class="bad-text">off: ' + (META.ai.configured ? "disabled in Preferences" : "no API key yet, so this pull costs nothing") + "</span>"}</dd>
          <dt>Takes</dt><dd>about 2–5 minutes</dd>
        </dl>
        <label class="switch" style="padding:12px 0 0"><input type="checkbox" id="skipPullConfirm"><span class="track"></span>
          <span>Don't show this again <span class="cell-dim">(turn it back on in Preferences → AI screening)</span></span></label>`,
    });
    if (!ok) return;   // cancelling never saves "don't show again"
    if ($("#skipPullConfirm")?.checked) store.set("skipPullConfirm", true);
    return runPull();
  }

  async function runPull() {
    try { await api("/api/scrape", { method: "POST" }); }
    catch (e) { if (e.status !== 409) { toast(e.message, "error"); return; } }
    showTask(true, "pull");
    pollTask();
  }

  async function startRescreen(scope) {
    const n = scope === "all" ? "every “Not applied” role, plus roles it marked “Not a fit” earlier (any that now fit go back to Not applied),"
      : "“Not applied” roles that haven't been screened yet";
    const ok = await confirmDialog({
      title: "Re-screen with AI?",
      ok: "Re-screen",
      body: `<div class="callout warn" style="margin-bottom:0"><svg width="16" height="16" viewBox="0 0 16 16"><path fill="currentColor" d="M8 1 15 14H1L8 1Zm-.75 5v4h1.5V6h-1.5Zm0 5v1.5h1.5V11h-1.5Z"/></svg>
        <div><b>This incurs LLM cost.</b> Claude will re-read ${n} against your current preferences (about $0.0005 per role) and auto-mark clear misses as “Not a fit”.</div></div>`,
    });
    if (!ok) return;
    try { await api("/api/ai/rescreen", { method: "POST", body: { scope } }); }
    catch (e) { if (e.status !== 409) { toast(e.message, "error"); return; } }
    location.hash = "#/";
    showTask(true, "rescreen");
    pollTask();
  }

  // Pull new roles ▾ → Fetch missing descriptions: re-reads the job page of roles you
  // already have that came without a description, then (optionally) AI-assesses them.
  async function startDescriptions() {
    let counts;
    try { counts = await api("/api/descriptions"); }
    catch (e) { toast(e.message, "error"); return; }
    const aiOn = META.ai.configured && META.ai_enabled;
    const line = (c) => !c.total ? "Every role in this selection already has a description."
      : `<b>${c.total.toLocaleString()}</b> role${c.total === 1 ? "" : "s"} without a description; `
        + `<b>${c.fetchable.toLocaleString()}</b> can be fetched again (LinkedIn and ZipRecruiter).`
        + (c.total > c.fetchable ? ` <span class="cell-dim">The rest are from boards whose pages can't be re-read (or were added by you); add those descriptions by hand.</span>` : "");
    const pending = confirmDialog({
      title: "Fetch missing descriptions?",
      ok: "Fetch descriptions",
      body: `
        <div style="font-size:13px;line-height:1.5" id="descCount">${line(counts.open)}</div>
        <div class="desc-options">
          <label class="switch" style="padding:0"><input type="checkbox" id="descUseAi" ${aiOn ? "checked" : "disabled"}><span class="track"></span>
            <span>Assess with AI<small>${aiOn ? `Screens each role that gets a description (${esc(META.model || "claude-haiku-5-5")}, about $0.0005 per role); a role the AI had marked Not a fit goes back to Not applied if it now fits.`
              : (META.ai.configured ? "AI screening is turned off in Preferences." : "No API key yet, so the fit can't be assessed.")}</small></span></label>
          <label class="switch" style="padding:0"><input type="checkbox" id="descOpenOnly" checked><span class="track"></span>
            <span>Only open roles<small>Leaves out Not a fit and archived roles.</small></span></label>
        </div>
        <div class="cell-dim" style="font-size:12px;padding-top:12px">Pages are fetched a few seconds apart so the boards don't block it, at most 100 per board per run; it can take several minutes.</div>`,
    });
    const openOnly = $("#descOpenOnly"), useAi = $("#descUseAi");
    openOnly.onchange = () => { $("#descCount").innerHTML = line(openOnly.checked ? counts.open : counts.all); };
    if (!(await pending)) return;
    const choice = { open_only: openOnly.checked, use_ai: aiOn && useAi.checked };   // the closed dialog keeps its body
    if (!(choice.open_only ? counts.open : counts.all).fetchable) { toast("Nothing to fetch: no role in that selection is on LinkedIn or ZipRecruiter.", ""); return; }
    try { await api("/api/descriptions", { method: "POST", body: choice }); }
    catch (e) { if (e.status !== 409) { toast(e.message, "error"); return; } }
    location.hash = "#/";
    showTask(true, "descriptions");
    pollTask();
  }

  function showDescriptionsSummary(s) {
    const r = s.result || {}, ai = r.ai;
    const n = (x) => (x ?? 0).toLocaleString();
    const LABELS = { linkedin: "LinkedIn", zip_recruiter: "ZipRecruiter" };
    const perSite = Object.entries(r.sites || {}).map(([k, v]) => `${esc(LABELS[k] || k)} ${n(v.fetched)} of ${n(v.tried)}`
      + (v.error ? ` <span class="bad-text">(couldn't start: ${esc(v.error)})</span>` : v.stopped_early ? ' <span class="bad-text">(kept refusing, stopped early)</span>' : "")).join("<br>");
    const aiLine = !r.use_ai ? "Off for this run"
      : !ai ? "Nothing new to assess"
      : ai.skipped ? esc(ai.skipped)
      : `${n(ai.screened)} assessed · ${n(ai.rejected)} marked Not a fit · ${n(ai.restored)} back to Not applied · ${dollars(ai.cost, 4)}`
        + (ai.errors ? ` · <span class="bad-text">${n(ai.errors)} failed</span>` : "");
    confirmDialog({ title: "Fetch missing descriptions: done", ok: "OK", cancel: null, body: `
      <div class="run-hero"><b>${n((r.described || []).length)}</b> role${(r.described || []).length === 1 ? "" : "s"} got a description</div>
      <dl class="confirm-meta">
        <dt>Without a description</dt><dd>${n(r.missing)}${r.open_only ? " open" : ""} role${r.missing === 1 ? "" : "s"}${r.unsupported ? `<div class="cell-dim">${n(r.unsupported)} on boards whose pages can't be re-read</div>` : ""}</dd>
        <dt>Fetched</dt><dd>${perSite || "—"}</dd>
        <dt>AI assessment</dt><dd>${aiLine}</dd>
      </dl>
      ${(r.fetched ?? 0) < (r.fetchable ?? 0) ? '<div class="cell-dim" style="font-size:12px;padding-top:8px">Some are still missing; run it again later for the rest.</div>' : ""}` });
  }

  function showTask(on, kind) {
    $("#scrapeBanner").classList.toggle("hidden", !on);
    $("#scrapeTitle").textContent = kind === "rescreen" ? "AI re-screening roles"
      : kind === "descriptions" ? "Fetching missing descriptions"
      : "Pulling new roles, then AI screening them";
    $("#pullMore").disabled = on;
    const b = $("#scrapeBtn");
    b.disabled = on;
    b.classList.toggle("busy", on);
    $("span", b).textContent = on ? "Working…" : "Pull new roles";
    if (!on) $("#scrapeLog").classList.add("hidden");
  }

  let pollFails = 0;
  async function pollTask() {
    clearTimeout(pollT);
    let s;
    try {
      s = await api("/api/scrape");
      pollFails = 0;
    } catch (e) {   // tracker restarting or briefly unreachable: keep trying
      pollFails++;
      $("#scrapeLine").textContent = `Can't reach the tracker (${e.message}). Retrying…`;
      pollT = setTimeout(pollTask, Math.min(30000, 2000 * pollFails));
      return;
    }
    const log = s.log || [];
    $("#scrapeLine").textContent = log.length ? log[log.length - 1] : "Starting…";
    $("#scrapeLog").textContent = log.join("\n");
    $("#scrapeLog").scrollTop = 1e9;
    renderLastRun(s.last_run);
    if (s.running) { showTask(true, s.kind); pollT = setTimeout(pollTask, 2000); return; }
    if (!$("#scrapeBanner").classList.contains("hidden")) {
      showTask(false);
      if (s.error) {
        if (s.kind === "pull") showPullSummary(s);
        else toast("Failed: " + s.error, "error");
      } else if (s.result) {
        if (s.kind === "pull") showPullSummary(s);
        else if (s.kind === "descriptions") showDescriptionsSummary(s);
        else {
          const ai = s.result.ai || {};
          let msg = `Re-screened ${ai.screened || 0} roles: ${ai.rejected || 0} marked Not a fit, ${ai.restored || 0} restored (${dollars(ai.cost, 4)})`;
          if (ai.skipped) msg += ". " + ai.skipped;
          toast(msg, "success");
        }
      } else {
        toast("The run stopped before finishing, probably because the tracker restarted. "
          + "Anything it saved is kept; start it again to finish.", "error");
      }
      load();
      if (route() === "preferences") renderRunsCard();
    }
  }

  // Job pages a pull opened for descriptions (LinkedIn, ZipRecruiter): only for new listings
  // that pass your rules, or roles you have that still lack one.
  function descriptionRows(r) {
    const n = (x) => (x ?? 0).toLocaleString();
    const LABELS = { linkedin: "LinkedIn", zip_recruiter: "ZipRecruiter" };
    const per = r.descriptions || (r.zip_descriptions ? { zip_recruiter: r.zip_descriptions } : {});
    return Object.entries(per).filter(([, v]) => v && (v.tried || v.error)).map(([site, v]) => {
      const name = LABELS[site] || site;
      return `<dt>${esc(name)} descriptions</dt><dd>${n(v.fetched)} of ${n(v.tried)} fetched${
        v.error ? `<div class="cell-dim">Couldn't start: ${esc(v.error)}</div>`
        : v.stopped_early ? `<div class="cell-dim">${esc(name)} kept refusing, so it stopped early. Use <b>Pull new roles ▾ → Fetch missing descriptions</b> later for the rest.</div>` : ""}</dd>`;
    }).join("");
  }

  // After a pull: what it searched, what it found, and what happened to each posting.
  async function showPullSummary(s) {
    const r = s.result || {}, ai = r.ai || {};
    const secs = s.started_at && s.finished_at ? Math.max(0, (new Date(s.finished_at) - new Date(s.started_at)) / 1000) : null;
    const took = secs == null ? "" : secs < 90 ? `${Math.round(secs)} s` : `${Math.round(secs / 60)} min`;
    const n = (x) => (x ?? 0).toLocaleString();
    const rejected = Object.entries(r.rejected || {}).sort((a, b) => b[1] - a[1]);
    const already = (r.rejected || {})["already in DB / seen before"] || 0;
    const filtered = rejected.filter(([k]) => k !== "already in DB / seen before");
    const filteredTotal = filtered.reduce((a, [, v]) => a + v, 0);
    const siteLine = Object.entries(r.sites || {}).sort((a, b) => b[1] - a[1])
      .map(([k, v]) => `${esc(SITE_LABELS[k] || k)} ${n(v)}`).join(" · ");
    let aiLine;
    if (ai.skipped) aiLine = esc(ai.skipped);
    else if (!ai.screened) aiLine = "Nothing new to screen";
    else aiLine = `${n(ai.screened)} screened · ${n(ai.rejected)} marked Not a fit · ${dollars(ai.cost, 4)}`
      + (ai.errors ? ` · <span class="bad-text">${n(ai.errors)} failed</span>` : "");
    // Boards that refused every search in this pull (blocked), e.g. "ZipRecruiter blocked your searches".
    const BOARD = { linkedin: "LinkedIn", indeed: "Indeed", zip_recruiter: "ZipRecruiter", glassdoor: "Glassdoor" };
    const blocked = Object.entries(r.blocked || {}).map(([site, why]) =>
      `<div><b>${esc(BOARD[site] || site)} blocked your searches.</b>${why ? ` <span class="cell-dim">(${esc(why)})</span>` : ""}</div>`).join("");
    const body = s.error
      ? `<div class="callout warn" style="margin-bottom:0"><div><b>The pull failed.</b><br>${esc(s.error)}</div></div>`
      : `<div class="run-hero"><b>${n(r.added)}</b> new role${r.added === 1 ? "" : "s"} added</div>
        ${blocked ? `<div class="callout warn blocked-note"><div>${blocked}<div class="cell-dim">None of its results are in this pull. It usually clears up after a while; if it keeps happening, the board may have changed how it answers.</div></div></div>` : ""}
        <dl class="confirm-meta">
          <dt>Searched</dt><dd>${n(r.searches)} search${r.searches === 1 ? "" : "es"}${took ? ` · took ${took}` : ""}${
            (r.failed_searches || []).length ? ` · <span class="bad-text">${r.failed_searches.length} failed</span>` : ""}</dd>
          <dt>Postings found</dt><dd>${n(r.scraped)}${siteLine ? `<div class="cell-dim">${siteLine}</div>` : ""}</dd>
          <dt>Already tracked</dt><dd>${n(already)}${r.details_filled ? `<div class="cell-dim">filled in details on ${n(r.details_filled)}`
            + `${r.descriptions_added ? ` (${n(r.descriptions_added)} got a description)` : ""}</div>` : ""}</dd>
          <dt>Filtered out</dt><dd>${n(filteredTotal)}${filtered.length ? `<div class="cell-dim">${filtered.slice(0, 5)
            .map(([k, v]) => `${esc(k)}: ${n(v)}`).join("<br>")}${filtered.length > 5 ? "<br>…" : ""}</div>` : ""}</dd>
          ${descriptionRows(r)}
          <dt>Passed filters</dt><dd>${n(r.kept)}</dd>
          <dt>AI screen</dt><dd>${aiLine}</dd>
        </dl>
        ${(r.failed_searches || []).length ? `<details class="run-fails"><summary>Failed searches</summary>
          <div class="cell-dim">${r.failed_searches.map(esc).join("<br>")}</div></details>` : ""}`;
    const showNew = !s.error && r.added > 0;
    const go = await confirmDialog({ title: s.error ? "Pull failed" : "Pull finished", body,
      ok: showNew ? "Show new roles" : "OK", cancel: showNew ? "Close" : null });
    if (go && showNew) {
      Object.assign(state, structuredClone(FILTER_DEFAULTS), { statuses: ["not_applied"], added: "h1" });
      location.hash = "#/";
      refilter();
    }
  }

  function renderLastRun(r) {
    $("#lastRun").textContent = r ? `Last pull ${ago(r.finished_at || r.started_at)} · +${r.added ?? 0} roles` : "No pulls yet";
  }

  // ================================================================== PREFERENCES PAGE
  let prefs = null, savedPrefs = null, defaults = null;
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const SEARCH_SUGGESTIONS = ["AI engineer", "AI software engineer", "applied AI engineer", "machine learning engineer",
    "LLM engineer", "generative AI engineer", "software engineer AI", "forward deployed engineer", "AI platform engineer",
    "ML platform engineer", "MLOps engineer", "AI product engineer", "backend engineer", "full stack engineer", "software engineer"];
  const INTEREST_SUGGESTIONS = ["AI", "LLMs", "Machine learning", "Agents", "Developer tools", "Infrastructure", "Data",
    "Fintech", "Healthcare", "Climate", "Robotics", "Security", "Consumer", "B2B SaaS", "Education", "Startups"];
  const SITES = [["linkedin", "LinkedIn"], ["indeed", "Indeed"], ["zip_recruiter", "ZipRecruiter"], ["glassdoor", "Glassdoor"]];
  const JOB_TYPES = [["fulltime", "Full-time"], ["contract", "Contract"], ["parttime", "Part-time"], ["internship", "Internship"]];
  const WORKPLACES = [["onsite", "On-site"], ["hybrid", "Hybrid"], ["remote", "Remote"]];
  const SIZES = [["startup", "Startup (<50)"], ["small", "Small (50–200)"], ["mid", "Mid-size (200–1k)"], ["large", "Large (1k–10k)"], ["enterprise", "Enterprise (10k+)"]];
  const MODELS = [["claude-haiku-5-5", "Claude Haiku 5.5 (recommended, ~$0.0005/role)"], ["claude-sonnet-5-5", "Claude Sonnet 5.5 (~$0.01/role)"], ["claude-opus-5-5", "Claude Opus 5.5 (~$0.02/role)"]];

  const route = () => (location.hash.startsWith("#/preferences") ? "preferences"
    : location.hash.startsWith("#/insights") ? "insights" : "roles");

  // "Checked by" line: where each preference is enforced.
  function badges(list) {
    return `<div class="checked-by" title="Search = sent to the job boards. Rules = exact checks on scraped data. AI = Claude reads the description.">
      <span class="cb-label">Checked by</span>${list.map((b) => `<span class="${b === "AI" ? "ai" : ""}">${b}</span>`).join('<span class="sep">·</span>')}</div>`;
  }
  // Must have / Nice to have segmented control.
  function mandatoryToggle(path, on) {
    return `<div class="req-toggle" data-req="${path}" role="radiogroup" aria-label="Requirement level">
      <button type="button" class="${on ? "on must" : ""}" data-v="1" title="Roles that clearly break this are removed (marked Not a fit).">Must have</button>
      <button type="button" class="${on ? "" : "on"}" data-v="0" title="Never removes a role; only affects its match level.">Nice to have</button></div>`;
  }
  function row(label, help, ctl, side = "") {
    return `<div class="prow"><div class="prow-head"><div class="prow-label">${label}</div>${help ? `<div class="prow-help">${help}</div>` : ""}
      ${side ? `<div class="prow-meta">${side}</div>` : ""}</div>
      <div class="prow-ctl">${ctl}</div></div>`;
  }
  function tagInput(path, values, placeholder) {
    return `<div class="taginput" data-tags="${path}">${values.map((v, i) => `<span class="chip">${esc(v)}<span class="x" data-rm="${i}">×</span></span>`).join("")}
      <input placeholder="${esc(placeholder)}"></div>`;
  }
  function checks(path, options, selected) {
    return `<div class="checks">${options.map(([v, l]) => `<label><input type="checkbox" data-check="${path}" value="${v}" ${selected.includes(v) ? "checked" : ""}>${esc(l)}</label>`).join("")}</div>`;
  }
  function getPath(path) { return path.split(".").reduce((o, k) => o[k], prefs); }
  function setPath(path, v) { const ks = path.split("."); const last = ks.pop(); ks.reduce((o, k) => o[k], prefs)[last] = v; }

  async function openPreferences() {
    const data = await api("/api/preferences");
    prefs = data.prefs; savedPrefs = clone(prefs); defaults = data.defaults;
    renderPrefs();
  }

  function searchesPerPull() {
    const locs = prefs.locations.value.length + (prefs.locations.include_remote ? 1 : 0);
    return prefs.search_terms.length * Math.max(locs, 1);
  }

  function renderPrefs() {
    const p = prefs;
    const unusedSuggestions = SEARCH_SUGGESTIONS.filter((s) => !p.search_terms.map((t) => t.toLowerCase()).includes(s.toLowerCase()));
    const unusedInterests = INTEREST_SUGGESTIONS.filter((s) => !p.interests.map((t) => t.toLowerCase()).includes(s.toLowerCase()));
    const key = META.ai;
    $("#prefsInner").innerHTML = `
      <div class="prefs-title">Search preferences</div>
      <p class="prefs-sub">What kinds of roles you're looking for. Used by every pull and by the AI screen.</p>

      <div class="callout">
        <svg width="16" height="16" viewBox="0 0 16 16"><path fill="currentColor" d="M8 1a7 7 0 1 1 0 14A7 7 0 0 1 8 1Zm-.75 6v5h1.5V7h-1.5Zm0-3v1.5h1.5V4h-1.5Z"/></svg>
        <div><b>How these are used:</b> we attempt to retrieve jobs with these parameters, but not every parameter can be supported by default.
          Job boards only accept a few filters (search terms, location, date, job type), and many details are only in the job description.
          So we use AI to do a second round of elimination: after each pull, Claude reads every new posting against all of your preferences,
          and roles that clearly break a <b>Must have</b> are automatically marked <b>Not a fit</b>, with the reason saved in the role's history.
          <b>Nice to have</b> preferences never remove a role; they only move its match level (Strong → Weak).
          <div style="margin-top:6px">“Checked by” under each preference shows where it's enforced: <b>Search</b> (the job boards),
          <b>Rules</b> (exact checks on scraped data) or <b>AI</b> (Claude reading the description).</div></div>
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">What to search</div><div class="pcard-desc">These go straight to the job boards.</div></div>
        ${row("Search terms", "Each term is searched in every location below (plus remote).",
          `${tagInput("search_terms", p.search_terms, "Add a search term and press Enter")}
           <div class="suggest"><span class="suggest-label">Suggestions:</span>${unusedSuggestions.map((s) => `<button class="chip add" data-addto="search_terms" data-val="${esc(s)}">${esc(s)}</button>`).join("")}</div>
           <ul class="tips">
             <li>Use job titles employers actually post (“machine learning engineer”), not skills (“PyTorch”). Boards match titles much better than keywords.</li>
             <li>3–6 terms works best. Each term adds a search per location, so more terms mean slower pulls, more LinkedIn rate-limiting, and more roles for the AI to screen.</li>
             <li>Leave out seniority words (“senior”, “junior”). Level is handled by Experience and the title filters below.</li>
             <li>Mix specific terms (“applied AI engineer”) with broad ones (“software engineer AI”). Overlapping results are de-duplicated.</li>
           </ul>
           <div class="estimate">This setup runs <b>${searchesPerPull()}</b> searches per pull.</div>`,
          badges(["Search"]))}
        ${row("Job sites", "Glassdoor is less reliable to scrape.", checks("sites", SITES, p.sites), badges(["Search"]))}
        ${row("Posted within", "Only postings newer than this.",
          `<div class="inline"><label class="field" style="max-width:220px"><select data-num="posted_within_hours">
            ${[[6, "Last 6 hours"], [12, "Last 12 hours"], [24, "Last 24 hours"], [48, "Last 2 days"], [72, "Last 3 days"], [168, "Last week"]].map(([v, l]) => `<option value="${v}" ${+p.posted_within_hours === v ? "selected" : ""}>${l}</option>`).join("")}
          </select></label></div>`, badges(["Search"]))}
        ${row("Results per search", "Max postings pulled per search, per site.",
          `<label class="switch" style="padding-left:0"><input type="checkbox" data-invbool="results_all" ${p.results_all ? "" : "checked"}><span class="track"></span><span>Limit results per search</span></label>
           <div class="inline"><label class="field" style="max-width:120px"><input type="number" min="5" max="200" data-num="results_per_search" value="${p.results_per_search}" ${p.results_all ? "disabled" : ""}></label></div>
           ${p.results_all ? `<div class="callout warn prefs-warn">
             <svg width="16" height="16" viewBox="0 0 16 16"><path fill="currentColor" d="M8 1 15 14H1L8 1Zm-.75 5v4h1.5V6h-1.5Zm0 5v1.5h1.5V11h-1.5Z"/></svg>
             <div><b>No limit:</b> each search takes everything the job boards return for the posted-within window (up to about 1,000 per search).
             Pulls get slower, job boards (especially LinkedIn) are more likely to throttle or block requests, and AI screening costs are less predictable,
             since every new role is screened (about $0.0005 each).</div></div>` : ""}`, badges(["Search"]))}
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">Location & workplace</div></div>
        ${row("Locations", "City, state. NYC boroughs count as New York.",
          `${tagInput("locations.value", p.locations.value, "e.g. New York, NY")}
           <div class="inline">
             <label class="field" style="max-width:160px"><span>Radius (miles)</span><input type="number" min="0" max="100" data-num="locations.radius_miles" value="${p.locations.radius_miles}"></label>
             <label class="switch" style="margin-top:18px"><input type="checkbox" data-bool="locations.include_remote" ${p.locations.include_remote ? "checked" : ""}><span class="track"></span><span>Also include remote (US)</span></label>
           </div>`,
          badges(["Search", "Rules", "AI"]) + mandatoryToggle("locations.mandatory", p.locations.mandatory))}
        ${row("Workplace type", "Hybrid and on-site details are usually only in the description.",
          checks("workplace.value", WORKPLACES, p.workplace.value),
          badges(["AI"]) + mandatoryToggle("workplace.mandatory", p.workplace.mandatory))}
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">Role & level</div></div>
        ${row("Years of experience", "Required minimum the posting asks for. Roles above the max, or entry level below the min, are excluded.",
          `<div class="inline"><label class="field" style="max-width:120px"><span>Min</span><input type="number" min="0" max="30" data-num="experience.min" value="${p.experience.min}"></label>
           <label class="field" style="max-width:120px"><span>Max</span><input type="number" min="0" max="30" data-num="experience.max" value="${p.experience.max}"></label></div>`,
          badges(["Rules", "AI"]) + mandatoryToggle("experience.mandatory", p.experience.mandatory))}
        ${row("Employment type", "", checks("employment_types.value", JOB_TYPES, p.employment_types.value),
          badges(["Search", "Rules", "AI"]) + mandatoryToggle("employment_types.mandatory", p.employment_types.mandatory))}
        ${row("Title must include one of", "Keeps results to engineering roles.",
          tagInput("title_include.value", p.title_include.value, "Add a word"),
          badges(["Rules"]) + mandatoryToggle("title_include.mandatory", p.title_include.mandatory))}
        ${row("Title must not include", "Seniority and off-target words. Senior level numbers like “Engineer 5”, “E5” or “L6” are also excluded when this is a must have.",
          tagInput("title_exclude.value", p.title_exclude.value, "Add a word"),
          badges(["Rules", "AI"]) + mandatoryToggle("title_exclude.mandatory", p.title_exclude.mandatory))}
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">Compensation</div></div>
        ${row("Pay", "Annual USD. Hourly pay is converted at 2,080 hours a year.",
          `<div class="inline">
             <label class="field"><span>Top of range at least</span><input type="number" step="5000" data-num="salary.min_top" value="${p.salary.min_top ?? ""}"></label>
             <label class="field"><span>Bottom of range at least (optional)</span><input type="number" step="5000" data-num="salary.min_floor" value="${p.salary.min_floor ?? ""}"></label>
           </div>
           <label class="switch"><input type="checkbox" data-bool="salary.include_unlisted" ${p.salary.include_unlisted ? "checked" : ""}><span class="track"></span><span>Keep roles that don't list pay</span></label>`,
          badges(["Rules", "AI"]) + mandatoryToggle("salary.mandatory", p.salary.mandatory))}
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">Company</div></div>
        ${row(`Blocked companies${p.companies_exclude.value.length ? ` <span class="count-inline">(${p.companies_exclude.value.length})</span>` : ""}`,
          "Pulls skip every role from these companies. Add one here, or when you mark a role Not a fit. "
          + "“Acme” also blocks “Acme Corp”. Click × to unblock.",
          tagInput("companies_exclude.value", p.companies_exclude.value, "Add a company and press Enter"),
          badges(["Rules", "AI"]))}
        ${row("Industries", "e.g. Fintech, Healthcare, Developer tools", tagInput("industries.value", p.industries.value, "Add an industry"),
          badges(["AI"]) + mandatoryToggle("industries.mandatory", p.industries.mandatory))}
        ${row("Company size", "", checks("company_size.value", SIZES, p.company_size.value),
          badges(["AI"]) + mandatoryToggle("company_size.mandatory", p.company_size.mandatory))}
        ${row("Visa sponsorship", "Turn on if you need the employer to sponsor a visa.",
          `<label class="switch"><input type="checkbox" data-bool="visa_sponsorship.value" ${p.visa_sponsorship.value ? "checked" : ""}><span class="track"></span><span>I need visa sponsorship</span></label>`,
          badges(["AI"]) + mandatoryToggle("visa_sponsorship.mandatory", p.visa_sponsorship.mandatory))}
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">Interests</div>
          <div class="pcard-desc">Topics you'd like to work on. The AI tags matching roles and ranks them higher, and each interest becomes a filter in the Roles sidebar.
          Interests are always nice-to-haves, so they never remove roles.</div></div>
        ${row("Interests", "",
          `${tagInput("interests", p.interests, "Add an interest, e.g. AI")}
           <div class="suggest"><span class="suggest-label">Suggestions:</span>${unusedInterests.map((s) => `<button class="chip add" data-addto="interests" data-val="${esc(s)}">${esc(s)}</button>`).join("")}</div>`,
          badges(["AI"]))}
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">In your own words</div>
          <div class="pcard-desc">Anything the fields above don't capture. The AI reads this with every posting. Example: “Prefer small teams shipping LLM products; avoid ad-tech and pure research roles.”</div></div>
        <div style="padding:16px 18px"><label class="field"><textarea rows="5" data-text="prompt" placeholder="Describe the role you want…">${esc(p.prompt || "")}</textarea></label></div>
      </div>

      <div class="pcard">
        <div class="pcard-head"><div class="pcard-title">Archiving</div>
          <div class="pcard-desc">Archived roles are hidden from the list, search and counts. Turn on “Include archived” in the sidebar to see them again.</div></div>
        ${row("Archive Not a fit roles", "Counted from when a role became Not a fit. A role you unarchive yourself stays unarchived.",
          `<label class="switch" style="padding-left:0"><input type="checkbox" data-bool="auto_archive.enabled" ${p.auto_archive.enabled ? "checked" : ""}><span class="track"></span><span>Archive them automatically</span></label>
           <div class="inline"><label class="field" style="max-width:120px"><span>After (days)</span>
             <input type="number" min="1" max="365" data-num="auto_archive.days" value="${p.auto_archive.days ?? ""}" ${p.auto_archive.enabled ? "" : "disabled"}></label></div>`)}
      </div>

      <div class="pcard" id="aiCard">
        <div class="pcard-head"><div class="pcard-title">AI screening</div>
          <div class="pcard-desc">Runs after each pull, on new roles only. Every run's token usage and cost is logged below.</div></div>
        ${row("Match levels", "The AI places every role on this scale. Shown in the Fit column; sort by it to see the best matches first.",
          `<div class="match-legend">${Object.entries(META.match_levels || {}).map(([k, v]) =>
            `<div><span class="fit ${MATCH_CLS[k]}"><span class="dot"></span>${esc(v.label)}</span><span>${esc(v.desc)}</span></div>`).join("")}</div>`)}
        ${row("Enabled", "", `<label class="switch"><input type="checkbox" data-bool="ai.enabled" ${p.ai.enabled ? "checked" : ""}><span class="track"></span><span>Screen new roles with AI after each pull</span></label>`)}
        ${row("Model", "Haiku is plenty for this and costs the least.",
          `<label class="field"><select data-textsel="ai.model">${MODELS.map(([v, l]) => `<option value="${v}" ${p.ai.model === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>`)}
        ${row("API key", "Billed to your Anthropic account (console.anthropic.com).",
          `<div class="keybox">${key.configured
            ? `<span class="ok-text">✓ Key found</span> <span class="code">${esc(key.hint)}</span> <button class="btn small" id="testKey">Test key</button><span id="testResult" class="cell-dim"></span>`
            : `<span class="bad-text">No key found.</span>`}</div>
           ${key.configured ? "" : `<div class="prow-help" style="margin:0">Create the file <span class="code">${esc(key.env_file)}</span> containing one line:
             <span class="code">ANTHROPIC_API_KEY=sk-ant-…</span>, then restart the tracker. Until then, pulls still work but skip the AI screen.</div>`}`)}
        ${row("Ask before each pull", "Show the cost reminder when you click Pull new roles. Saved in this browser, right away.",
          `<label class="switch"><input type="checkbox" id="askBeforePull" ${askBeforePull() ? "checked" : ""}><span class="track"></span><span>Confirm before pulling new roles</span></label>`)}
        ${row("Re-screen", "Run the AI again on roles already in the database, e.g. after changing preferences. Save your changes first.",
          `<div class="inline"><button class="btn primary" data-rescreen="all">Re-screen all open roles</button>
           <button class="btn" data-rescreen="unscreened">Only unscreened roles</button></div>
           <div class="prow-help" style="margin:0"><b>Re-screen all open roles</b> re-checks every “Not applied” role against your current preferences
           (new misses are marked Not a fit) and gives roles the AI marked “Not a fit” earlier another look; any that now fit go back to Not applied.
           Roles you set yourself (Applied, Interviewing, Not a fit you chose, etc.) and archived roles are never touched.</div>`)}
      </div>

      <div class="pcard" id="runsCard"><div class="pcard-head"><div class="pcard-title">Pull history & AI spend</div></div><div id="runsBody" class="cell-dim" style="padding:16px 18px">Loading…</div></div>
    `;
    wirePrefs();
    buildPrefsNav();
    renderRunsCard();
    updateDirty();
  }

  function buildPrefsNav() {
    const cards = $$("#prefsInner .pcard");
    cards.forEach((c, i) => { c.id = "pref-sec-" + i; });
    $("#prefsNav").innerHTML = `<div class="section-label">Preferences</div>` + cards.map((c, i) =>
      `<a href="#" data-sec="${i}">${esc($(".pcard-title", c).textContent)}</a>`).join("");
    $$("#prefsNav a").forEach((a) => a.onclick = (e) => {
      e.preventDefault();
      $("#pref-sec-" + a.dataset.sec).scrollIntoView({ behavior: "smooth", block: "start" });
    });
    syncPrefsNav();
  }

  function syncPrefsNav() {
    const view = $("#prefsView");
    const cards = $$("#prefsInner .pcard");
    if (!cards.length) return;
    const top = view.getBoundingClientRect().top + 80;
    let cur = 0;
    cards.forEach((c, i) => { if (c.getBoundingClientRect().top <= top) cur = i; });
    if (view.scrollTop + view.clientHeight >= view.scrollHeight - 4) cur = cards.length - 1;
    $$("#prefsNav a").forEach((a) => a.classList.toggle("on", +a.dataset.sec === cur));
  }

  async function renderRunsCard() {
    const el = $("#runsBody");
    if (!el) return;
    const d = await api("/api/runs");
    const rows = d.runs.map((r) => `
      <tr><td>${fmtDate(r.started_at, true)}</td><td>${r.kind === "rescreen" ? "Re-screen" : r.kind === "descriptions" ? "Descriptions" : r.kind === "import" ? "Import" : "Pull"}</td><td>${esc(r.trigger || "")}</td>
        <td class="num">${r.scraped ?? "—"}</td><td class="num">${r.added ?? "—"}</td><td class="num">${r.ai_screened ?? 0}</td>
        <td class="num">${r.ai_rejected ?? 0}</td><td class="num">${((r.ai_input_tokens || 0) + (r.ai_output_tokens || 0)).toLocaleString()}</td>
        <td class="num">${dollars(r.ai_cost, 4)}</td><td>${esc(r.status || "")}</td></tr>`).join("");
    el.className = "";
    el.style.padding = "0";
    el.innerHTML = `
      <div class="stat-row">
        <div class="stat"><div class="v">${dollars(d.month_cost, 4)}</div><div class="l">AI spend this month</div></div>
        <div class="stat"><div class="v">${d.month_runs}</div><div class="l">Runs this month</div></div>
        <div class="stat"><div class="v">${dollars(d.total_cost, 4)}</div><div class="l">AI spend all time</div></div>
      </div>
      <div class="runs-wrap">${d.runs.length ? `<table class="runs"><thead><tr><th>When</th><th>Type</th><th>Trigger</th><th class="num">Scraped</th><th class="num">Added</th>
        <th class="num">AI screened</th><th class="num">AI → Not a fit</th><th class="num">Tokens</th><th class="num">Cost</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="cell-dim" style="padding:16px 18px">No runs yet.</div>'}</div>`;
  }

  function updateDirty() {
    const dirty = JSON.stringify(prefs) !== JSON.stringify(savedPrefs);
    $("#prefsSavebar").classList.toggle("dirty", dirty);
    $("#prefsDirty").textContent = dirty ? "Unsaved changes" : "All changes saved";
    $("#prefsSave").disabled = !dirty;
    $("#prefsReset").disabled = !dirty;
    const est = $(".estimate b");
    if (est) est.textContent = searchesPerPull();
  }

  function wirePrefs() {
    const root = $("#prefsInner");
    // tag inputs
    $$(".taginput", root).forEach((box) => {
      const path = box.dataset.tags;
      const input = $("input", box);
      const add = (raw) => {
        const vals = raw.split(",").map((s) => s.trim()).filter(Boolean);
        if (!vals.length) return;
        const cur = getPath(path);
        for (const v of vals) if (!cur.some((x) => x.toLowerCase() === v.toLowerCase())) cur.push(v);
        renderPrefsKeepFocus(path);
      };
      box.addEventListener("click", (e) => {
        const rm = e.target.closest("[data-rm]");
        if (rm) { getPath(path).splice(+rm.dataset.rm, 1); renderPrefsKeepFocus(path); return; }
        input.focus();
      });
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(input.value); }
        else if (e.key === "Backspace" && !input.value && getPath(path).length) { getPath(path).pop(); renderPrefsKeepFocus(path); }
      });
      input.addEventListener("blur", () => { if (input.value.trim()) add(input.value); });
    });
    $$("[data-addto]", root).forEach((b) => b.onclick = () => {
      const cur = getPath(b.dataset.addto);
      if (!cur.includes(b.dataset.val)) cur.push(b.dataset.val);
      renderPrefsKeepScroll();
    });
    $$("[data-check]", root).forEach((c) => c.onchange = () => {
      const path = c.dataset.check;
      setPath(path, $$(`[data-check="${path}"]`, root).filter((x) => x.checked).map((x) => x.value));
      updateDirty();
    });
    $$("[data-req]", root).forEach((g) => g.onclick = (e) => {
      const b = e.target.closest("button"); if (!b) return;
      const on = b.dataset.v === "1";
      setPath(g.dataset.req, on);
      $$("button", g).forEach((x) => { const sel = (x.dataset.v === "1") === on; x.classList.toggle("on", sel); x.classList.toggle("must", sel && on); });
      updateDirty();
    });
    $$("[data-bool]", root).forEach((c) => c.onchange = () => {
      setPath(c.dataset.bool, c.checked);
      if (c.dataset.bool === "auto_archive.enabled") renderPrefsKeepScroll();   // enables/disables the days box
      else updateDirty();
    });
    // A switch whose "on" means the setting is false (e.g. "Limit results" on = results_all off);
    // redraws so the warning and the disabled number box follow it.
    $$("[data-invbool]", root).forEach((c) => c.onchange = () => { setPath(c.dataset.invbool, !c.checked); renderPrefsKeepScroll(); });
    $$("[data-num]", root).forEach((c) => c.oninput = () => { setPath(c.dataset.num, c.value === "" ? null : Number(c.value)); updateDirty(); });
    $$("[data-text]", root).forEach((c) => c.oninput = () => { setPath(c.dataset.text, c.value); updateDirty(); });
    $$("[data-textsel]", root).forEach((c) => c.onchange = () => { setPath(c.dataset.textsel, c.value); updateDirty(); });
    $$("[data-rescreen]", root).forEach((b) => b.onclick = () => {
      if (JSON.stringify(prefs) !== JSON.stringify(savedPrefs)) { toast("Save your preference changes first.", "info"); return; }
      if (!META.ai.configured) { toast("Add an API key first (see AI screening).", "error"); return; }
      startRescreen(b.dataset.rescreen);
    });
    // A browser setting, not a search preference: applies immediately, no Save needed.
    $("#askBeforePull").onchange = (e) => {
      store.set("skipPullConfirm", !e.target.checked);
      toast(e.target.checked ? "You'll be asked before each pull." : "Pulls will start without asking.", "success");
    };
    const tk = $("#testKey");
    if (tk) tk.onclick = async () => {
      $("#testResult").textContent = "Testing…";
      try { const r = await api("/api/ai/test", { method: "POST" }); $("#testResult").innerHTML = `<span class="ok-text">Works: ${esc(r.model)} replied “${esc(r.reply)}”</span>`; }
      catch (e) { $("#testResult").innerHTML = `<span class="bad-text">${esc(e.message)}</span>`; }
    };
  }

  function renderPrefsKeepScroll() {
    const y = $("#prefsView").scrollTop;
    renderPrefs();
    $("#prefsView").scrollTop = y;
  }
  function renderPrefsKeepFocus(path) {
    renderPrefsKeepScroll();
    const inp = $(`.taginput[data-tags="${path}"] input`);
    if (inp) inp.focus();
  }

  async function savePrefs() {
    try {
      const d = await api("/api/preferences", { method: "PUT", body: prefs });
      prefs = d.prefs; savedPrefs = clone(prefs);
      META = await api("/api/meta");
      toast("Preferences saved. They apply to the next pull."
        + (d.auto_archived ? ` Archived ${d.auto_archived} Not a fit role${d.auto_archived === 1 ? "" : "s"}.` : ""), "success");
      renderPrefsKeepScroll();
    } catch (e) { toast(e.message, "error"); }
  }

  // ================================================================== INSIGHTS PAGE
  let insWeeks = 12, insData = null, editingTarget = null;
  const fmtWeek = (iso) => new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const pctOf = (n, d) => (d ? Math.round((n / d) * 100) : 0);
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

  async function openInsights() {
    insData = await api(`/api/insights?weeks=${insWeeks}`);
    renderInsights();
  }

  // ---- tooltip shared by all charts
  function showTip(e, html) {
    const tip = $("#chartTip");
    tip.innerHTML = html;
    tip.classList.remove("hidden");
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = e.clientX + 14, y = e.clientY - h - 10;
    if (x + w > innerWidth - 8) x = e.clientX - w - 14;
    if (y < 8) y = e.clientY + 16;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  const hideTip = () => $("#chartTip").classList.add("hidden");

  function niceMax(v, integer) {
    if (v <= 0) return integer ? 4 : 1;
    const m = v * 1.1, mag = 10 ** Math.floor(Math.log10(m)), n = m / mag;
    let nice = (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
    if (integer) nice = Math.max(4, Math.ceil(nice / 4) * 4);
    return nice;
  }

  // Vertical bar chart (one series, one hue). Thin bars with rounded tops anchored to the
  // baseline, recessive grid, optional dashed target line, hover tooltip per bar.
  function barChart(el, points, { target = null, targetLabel = "Target", money = false } = {}) {
    if (!points.length || points.every((p) => !p.value) && target == null) {
      el.innerHTML = '<div class="chart-empty">Nothing here yet. It fills in as you use the tracker.</div>';
      return;
    }
    const W = Math.max(280, el.clientWidth), H = 190, m = { l: money ? 46 : 30, r: 10, t: 14, b: 24 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const yMax = niceMax(Math.max(...points.map((p) => p.value), target || 0), !money);
    const y = (v) => m.t + ih - (v / yMax) * ih;
    const band = iw / points.length, bw = Math.min(36, Math.max(4, band * 0.62));
    const fmt = (v) => (money ? `$${v < 1 && v > 0 ? v.toFixed(3) : v.toFixed(2)}` : `${v}`);
    const ticks = [0, 1, 2, 3, 4].map((i) => (yMax / 4) * i);
    const every = Math.max(1, Math.ceil(points.length / Math.floor(iw / 56)));
    let svg = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img">`;
    ticks.forEach((t) => {
      svg += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}"/>
        <text class="axis-text" x="${m.l - 6}" y="${y(t) + 4}" text-anchor="end">${money ? "$" + (+t.toFixed(3)) : Math.round(t)}</text>`;
    });
    points.forEach((p, i) => {
      const x = m.l + band * i + (band - bw) / 2, top = y(p.value), h = m.t + ih - top;
      if (h > 0) {
        const r = Math.min(4, bw / 2, h);
        svg += `<path class="bar" data-i="${i}" d="M${x},${m.t + ih} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${m.t + ih} Z"/>`;
      }
      if (i % every === 0 || i === points.length - 1) {
        svg += `<text class="axis-text" x="${m.l + band * i + band / 2}" y="${H - 6}" text-anchor="middle">${esc(p.label)}</text>`;
      }
      svg += `<rect class="bar-hit" data-i="${i}" x="${m.l + band * i}" y="${m.t}" width="${band}" height="${ih}"/>`;
    });
    if (target != null && target > 0) {
      svg += `<line class="target-line" x1="${m.l}" x2="${W - m.r}" y1="${y(target)}" y2="${y(target)}"/>
        <text class="target-text" x="${W - m.r}" y="${y(target) - 5}" text-anchor="end">${esc(targetLabel)} ${fmt(target)}</text>`;
    }
    svg += "</svg>";
    el.innerHTML = svg;
    $$(".bar-hit", el).forEach((hit) => {
      const i = +hit.dataset.i, bar = $(`.bar[data-i="${i}"]`, el), p = points[i];
      hit.onmousemove = (e) => { bar?.classList.add("hover"); showTip(e, p.tip || `${esc(p.label)}: <b>${fmt(p.value)}</b>`); };
      hit.onmouseleave = () => { bar?.classList.remove("hover"); hideTip(); };
    });
  }

  // Horizontal bars (funnel, match levels): label, bar, value (+ secondary text).
  function hbars(rows) {
    const max = Math.max(1, ...rows.map((r) => r.value));
    return `<div class="hbars">${rows.map((r) => `
      <div class="hbar" title="${esc(r.title || "")}"><span class="hbar-label">${esc(r.label)}</span>
        <span class="hbar-track"><span class="hbar-fill" style="width:${r.value ? Math.max(1, (r.value / max) * 100) : 0}%"></span></span>
        <span class="hbar-val">${r.value}${r.sub ? `<small>${esc(r.sub)}</small>` : ""}</span></div>`).join("")}</div>`;
  }

  function tableHtml(headers, rows) {
    return `<table class="chart-table"><thead><tr>${headers.map((h, i) => `<th class="${i ? "num" : ""}">${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td class="${i ? "num" : ""}">${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function targetCard(key, label, valueHtml, meter, note, noteCls) {
    const t = insData.targets[key];
    const editing = editingTarget === key;
    const input = `<input type="number" min="0" id="targetInput" value="${t ?? ""}" placeholder="none">`;
    return `<div class="tcard" data-target="${key}">
      <div class="tcard-top"><span class="tcard-label">${esc(label)}</span>
        ${editing ? "" : `<button class="btn minimal small" data-edit-target="${key}">${t == null || t === "" ? "Set target" : "Edit"}</button>`}</div>
      <div class="tcard-value">${valueHtml}</div>
      ${meter != null ? `<div class="meter ${meter.cls || ""}"><span style="width:${Math.min(100, meter.pct)}%"></span></div>` : ""}
      ${editing ? `<div class="tcard-edit">${input}<button class="btn primary small" id="targetSave">Save</button>
          <button class="btn small" id="targetCancel">Cancel</button></div>`
        : `<div class="tcard-note ${noteCls || ""}">${note}</div>`}
    </div>`;
  }

  function renderInsights() {
    const d = insData, p = d.progress, t = d.targets, w = d.weekly, a = d.applying;
    // --- target cards
    const goal = t.applications_per_week, n = p.applications_this_week;
    const appNote = !goal ? "No weekly target set."
      : n >= goal ? `Goal met${p.application_streak_weeks > 1 ? ` · ${p.application_streak_weeks}-week streak` : ""}`
      : `${goal - n} to go · ${plural(p.days_left_in_week, "day")} left this week${p.application_streak_weeks ? ` · ${p.application_streak_weeks}-week streak` : ""}`;
    const im = t.inbox_max, inbox = p.inbox;
    const inboxNote = im == null ? "No target set." : inbox > im ? `Review ${inbox - im} more to get under ${im}` : "Under your target";

    // --- chart data
    const totalNew = w.reduce((s, x) => s + x.new_roles, 0);
    const totalSpend = w.reduce((s, x) => s + x.ai_cost, 0);
    const f = d.funnel;
    const funnelRows = f.map((s, i) => ({ label: s.stage, value: s.count,
      sub: i ? `${pctOf(s.count, f[i - 1].count)}%` : "",
      title: i ? `${s.count} of ${f[i - 1].count} ${f[i - 1].stage.toLowerCase()} (${pctOf(s.count, f[i - 1].count)}%)` : `${s.count} roles in your database` }));
    const openTotal = d.matches.reduce((s, x) => s + x.count, 0);
    const matchRows = d.matches.map((m) => ({ label: m.label, value: m.count, sub: openTotal ? `${pctOf(m.count, openTotal)}%` : "",
      title: (META.match_levels?.[m.level]?.desc) || "" }));
    const appliedMatchRows = a.by_match.map((m) => ({ label: m.label, value: m.count, sub: a.total ? `${pctOf(m.count, a.total)}%` : "",
      title: (META.match_levels?.[m.level]?.desc) || "Applied before AI screening was turned on" }));

    const card = (id, title, sub, headline, table) => `
      <div class="ccard"><div class="ccard-head"><span class="ccard-title">${title}</span><span class="ccard-sub">${sub}</span>
        <button class="btn minimal small" data-table="${id}">Table</button></div>
        ${headline ? `<div class="ccard-headline">${headline}</div>` : ""}
        <div class="chart" id="${id}"></div><div class="hidden" id="${id}-table">${table}</div></div>`;
    const weekRows = (k, money) => w.map((x) => [`Week of ${fmtWeek(x.week_start)}`, money ? `$${x[k].toFixed(4)}` : x[k]]);

    $("#insightsInner").innerHTML = `
      <div class="ins-head"><div><div class="prefs-title">Insights</div>
        <p class="prefs-sub">How much you're applying, and how you're tracking against your targets.</p></div>
        <div class="segmented" id="insRange">${[8, 12, 26].map((x) => `<button data-v="${x}" class="${x === insWeeks ? "on" : ""}">${x} weeks</button>`).join("")}</div></div>

      <div class="ins-section-label">Targets</div>
      <div class="target-grid">
        ${targetCard("applications_per_week", "Applications this week", `${n}<small>/ ${goal ?? "—"}</small>`,
          goal ? { pct: pctOf(n, goal), cls: n >= goal ? "done" : "" } : null, appNote, goal && n >= goal ? "good" : "")}
        ${targetCard("inbox_max", "Roles waiting for review", `${inbox}<small>${im != null ? `/ ≤ ${im}` : ""}</small>`,
          im != null ? { pct: im ? pctOf(inbox, im) : 100, cls: inbox > im ? "over" : "done" } : null, inboxNote, im == null ? "" : inbox > im ? "warn" : "good")}
        <div class="tcard"><div class="tcard-top"><span class="tcard-label">Applying · last ${insWeeks} weeks</span></div>
          <div class="stat-mini" style="margin-top:0">
            <div><div class="v">${a.total}</div><div class="l">Applications</div></div>
            <div><div class="v">${a.per_week_avg}</div><div class="l">Per week (avg)</div></div>
            <div><div class="v">${a.best_week ? a.best_week.count : "—"}</div><div class="l">Best week${a.best_week ? ` (${fmtWeek(a.best_week.week_start)})` : ""}</div></div>
            <div><div class="v">${a.avg_days_posted_to_applied ?? "—"}</div><div class="l">Days from posting to applying (avg)</div></div>
          </div></div>
      </div>

      <div class="ins-section-label">Trends · last ${insWeeks} weeks</div>
      <div class="chart-grid">
        ${card("chApplied", "Applications per week", "by date applied",
          `${a.total}<small>total · ${a.per_week_avg}/week avg</small>`, tableHtml(["Week", "Applications"], weekRows("applied")))}
        <div class="ccard"><div class="ccard-head"><span class="ccard-title">Review funnel</span><span class="ccard-sub">all roles except archived · % of the stage above</span></div>
          ${hbars(funnelRows)}</div>
        <div class="ccard"><div class="ccard-head"><span class="ccard-title">What you applied to</span><span class="ccard-sub">AI match level of roles you applied to (${a.total})</span></div>
          ${hbars(appliedMatchRows)}</div>
        <div class="ccard"><div class="ccard-head"><span class="ccard-title">Waiting for review</span><span class="ccard-sub">match levels of open roles (${openTotal})</span></div>
          ${hbars(matchRows)}</div>
        ${card("chNew", "New roles found per week", "added by pulls",
          `${totalNew}<small>roles added</small>`, tableHtml(["Week", "New roles"], weekRows("new_roles")))}
        ${card("chSpend", "AI screening spend per week", "Claude API cost",
          `$${totalSpend.toFixed(3)}<small>in ${insWeeks} weeks</small>`, tableHtml(["Week", "Spend"], weekRows("ai_cost", true)))}
      </div>`;
    drawInsightCharts();
    wireInsights();
  }

  function drawInsightCharts() {
    const w = insData.weekly, lbl = (x) => fmtWeek(x.week_start);
    const pts = (k) => w.map((x) => ({ label: lbl(x), value: x[k], tip: `Week of ${esc(fmtWeek(x.week_start))}: <b>${x[k]}</b>` }));
    barChart($("#chApplied"), pts("applied"), { target: insData.targets.applications_per_week, targetLabel: "Target" });
    barChart($("#chNew"), pts("new_roles"));
    barChart($("#chSpend"), w.map((x) => ({ label: lbl(x), value: x.ai_cost,
      tip: `Week of ${esc(fmtWeek(x.week_start))}: <b>$${x.ai_cost.toFixed(4)}</b>` })), { money: true });
  }

  function wireInsights() {
    const root = $("#insightsInner");
    $("#insRange").onclick = (e) => { const b = e.target.closest("button"); if (!b) return; insWeeks = +b.dataset.v; openInsights(); };
    $$("[data-table]", root).forEach((b) => b.onclick = () => {
      const t = $(`#${b.dataset.table}-table`), c = $(`#${b.dataset.table}`);
      const showTable = t.classList.contains("hidden");
      t.classList.toggle("hidden", !showTable); c.classList.toggle("hidden", showTable);
      b.textContent = showTable ? "Chart" : "Table";
    });
    $$("[data-edit-target]", root).forEach((b) => b.onclick = () => { editingTarget = b.dataset.editTarget; renderInsights(); $("#targetInput")?.focus(); });
    const save = async (value) => {
      try {
        const res = await api("/api/targets", { method: "PUT", body: { [editingTarget]: value } });
        insData.targets = res.targets; editingTarget = null;
        toast("Target saved", "success"); openInsights();
      } catch (e) { toast(e.message, "error"); }
    };
    if ($("#targetSave")) {
      $("#targetSave").onclick = () => save($("#targetInput").value);
      $("#targetInput").onkeydown = (e) => { if (e.key === "Enter") save($("#targetInput").value); if (e.key === "Escape") { editingTarget = null; renderInsights(); } };
      $("#targetCancel").onclick = () => { editingTarget = null; renderInsights(); };
    }
  }

  let insResizeT;
  window.addEventListener("resize", () => {
    if (route() !== "insights" || !insData) return;
    clearTimeout(insResizeT); insResizeT = setTimeout(drawInsightCharts, 120);
  });

  // ------------------------------------------------------------------ Logo menu
  function openMenu() {
    $("#navMenu").classList.remove("hidden");
    $("#brand").setAttribute("aria-expanded", "true");
    ($("#navMenu a.on") || $("#navMenu a")).focus();
  }
  function closeMenu() {
    $("#navMenu").classList.add("hidden");
    $("#brand").setAttribute("aria-expanded", "false");
  }
  function openPullMenu() {
    $("#pullMenu").classList.remove("hidden");
    $("#pullMore").setAttribute("aria-expanded", "true");
    $("#pullMenu a").focus();
  }
  function closePullMenu() {
    $("#pullMenu").classList.add("hidden");
    $("#pullMore").setAttribute("aria-expanded", "false");
  }

  // ------------------------------------------------------------------ Routing
  let shownRoute = null;
  const prefsDirty = () => prefs && JSON.stringify(prefs) !== JSON.stringify(savedPrefs);
  async function applyRoute() {
    const r = route();
    // Leaving Preferences inside the app (menu, links) with unsaved edits: ask first.
    if (shownRoute === "preferences" && r !== "preferences" && prefsDirty()
        && !confirm("You have unsaved preference changes. Leave without saving?")) {
      history.replaceState(null, "", "#/preferences");   // stay; doesn't fire hashchange
      closeMenu();
      return;
    }
    shownRoute = r;
    $$("#navMenu a").forEach((a) => a.classList.toggle("on", a.dataset.view === r));
    $("#brandPage").textContent = { preferences: "Preferences", insights: "Insights" }[r] || "Roles";
    closeMenu();
    $("#rolesView").classList.toggle("hidden", r !== "roles");
    $("#prefsView").classList.toggle("hidden", r !== "preferences");
    $("#insightsView").classList.toggle("hidden", r !== "insights");
    hideTip();
    if (r === "preferences") { closeDrawer(); await openPreferences(); }
    else if (r === "insights") { closeDrawer(); editingTarget = null; await openInsights(); }
    else {
      // A URL with filters (pasted, bookmarked, back/forward) sets them; a bare #/ keeps the current ones.
      if (stateFromUrl()) $("#q").value = state.q;
      load();
      openRoleFromUrl();
    }
  }

  // ------------------------------------------------------------------ Wiring
  function wire() {
    let qT;
    $("#q").addEventListener("input", (e) => { clearTimeout(qT); qT = setTimeout(() => { state.q = e.target.value.trim(); refilter(); }, 180); });

    $("#statusList").addEventListener("click", (e) => {
      const li = e.target.closest(".status-item");
      if (!li) return;
      const s = li.dataset.s;
      if (!s) state.statuses = [];
      else if (e.metaKey || e.ctrlKey || e.shiftKey || state.statuses.length) {
        state.statuses = state.statuses.includes(s) ? state.statuses.filter((x) => x !== s) : [...state.statuses, s];
      } else state.statuses = [s];
      refilter();
    });
    $("#tagFilter").addEventListener("click", (e) => {
      const b = e.target.closest("[data-tag]");
      if (!b) return;
      const t = b.dataset.tag;
      state.tags = state.tags.includes(t) ? state.tags.filter((x) => x !== t) : [...state.tags, t];
      refilter();
    });
    $("#stageForm").onsubmit = (e) => { e.preventDefault(); submitStage(false); };
    $("#stageSkip").onclick = () => submitStage(true);
    $$("#stageDialog [data-sclose]").forEach((b) => b.onclick = () => $("#stageDialog").classList.add("hidden"));
    for (const [id, key] of [["#fPaid", "paid"], ["#fArchived", "archived"],
      ["#fDescribed", "described"], ["#fTodo", "todo"]])
      $(id).addEventListener("change", (e) => { state[key] = e.target.checked; refilter(); });
    $("#fAdded").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; state.added = b.dataset.v; refilter(); });
    $("#fPosted").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; state.posted = b.dataset.v; refilter(); });
    $("#fMinPay").onchange = (e) => { state.minPay = e.target.value; refilter(); };
    $("#fMaxYears").onchange = (e) => { state.maxYears = e.target.value; refilter(); };
    const toggleIn = (list, v) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
    $("#fitFilter").addEventListener("click", (e) => { const b = e.target.closest("[data-fit]"); if (!b) return; state.fits = toggleIn(state.fits, b.dataset.fit); refilter(); });
    $("#siteFilter").addEventListener("click", (e) => { const b = e.target.closest("[data-site]"); if (!b) return; state.sites = toggleIn(state.sites, b.dataset.site); refilter(); });
    const addLoc = (v) => {
      v = (v || "").trim();
      if (v && !state.locs.some((l) => l.toLowerCase() === v.toLowerCase())) { state.locs = [...state.locs, v]; refilter(); }
    };
    $("#fLocInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addLoc(e.target.value); e.target.value = ""; }
    });
    $("#locFilter").addEventListener("click", (e) => {
      const add = e.target.closest("[data-loc-add]"), rm = e.target.closest("[data-loc-rm]");
      if (add) addLoc(add.dataset.locAdd);
      if (rm) { state.locs = state.locs.filter((l) => l !== rm.dataset.locRm); refilter(); }
      if (e.target.closest("[data-remote]")) { state.remote = !state.remote; refilter(); }
      if (e.target.closest("[data-places-more]")) { showAllPlaces = !showAllPlaces; renderChipFilters(); }
    });
    const addCompany = (v) => {
      v = (v || "").trim();
      const known = (META.companies || []).find((c) => c.name.toLowerCase() === v.toLowerCase());
      if (known) v = known.name;   // "initech" -> "Initech", as the employer is listed
      if (v && !state.companies.some((c) => c.toLowerCase() === v.toLowerCase())) { state.companies = [...state.companies, v]; refilter(); }
    };
    $("#fCoInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addCompany(e.target.value); e.target.value = ""; }
    });
    // Picking a suggestion from the list adds it right away.
    $("#fCoInput").addEventListener("input", (e) => {
      if (e.inputType && e.inputType !== "insertReplacementText") return;
      if ((META.companies || []).some((c) => c.name === e.target.value)) { addCompany(e.target.value); e.target.value = ""; }
    });
    $("#coFilter").addEventListener("click", (e) => {
      const add = e.target.closest("[data-co-add]"), rm = e.target.closest("[data-co-rm]");
      if (add) addCompany(add.dataset.coAdd);
      if (rm) { state.companies = state.companies.filter((c) => c !== rm.dataset.coRm); refilter(); }
      if (e.target.closest("[data-companies-more]")) { showAllCompanies = !showAllCompanies; renderCompanyFilter(); }
    });
    $("#clearFilters").onclick = () => {
      Object.assign(state, structuredClone(FILTER_DEFAULTS), { archived: false, sort: "status", order: "asc" });
      $("#q").value = ""; state.q = ""; $("#fLocInput").value = ""; $("#fCoInput").value = "";
      $$(".fgroup").forEach((d) => { d.open = false; });   // collapse every group (remembered via "toggle")
      refilter();
    };
    // Remember which filter groups are open.
    const openGroups = store.get("filterGroups", null);
    $$(".fgroup").forEach((d) => {
      if (openGroups && d.dataset.group in openGroups) d.open = openGroups[d.dataset.group];
      d.addEventListener("toggle", () => {
        store.set("filterGroups", Object.fromEntries($$(".fgroup").map((x) => [x.dataset.group, x.open])));
      });
    });

    $$(".grid th[data-sort]").forEach((th) => th.onclick = () => {
      const k = th.dataset.sort;
      if (state.sort === k) state.order = state.order === "asc" ? "desc" : "asc";
      else { state.sort = k; state.order = ["title", "company", "location", "status", "years_min", "fit"].includes(k) ? "asc" : "desc"; }
      refilter();
    });

    $("#rows").addEventListener("click", (e) => {
      const tr = e.target.closest("tr");
      if (tr) openRole(+tr.dataset.id);
    });

    $("#brand").addEventListener("click", (e) => {
      e.stopPropagation();
      $("#navMenu").classList.contains("hidden") ? openMenu() : closeMenu();
    });
    $("#navMenu").addEventListener("click", (e) => { if (e.target.closest("a")) closeMenu(); });
    document.addEventListener("click", (e) => { if (!e.target.closest(".brand-wrap")) closeMenu(); });
    // Clicking anywhere outside the open role panel closes it. Not when the click is
    // in a popup or dialog (status change, add role, confirm), a toast, or on a row
    // of the table (that opens the other role instead).
    document.addEventListener("pointerdown", (e) => {
      if (!current || !$("#drawer").classList.contains("open")) return;
      const t = e.target;
      if (!t.isConnected || t.closest("#drawer, .dialog-backdrop, .toasts, #navMenu, #rows tr")) return;
      closeDrawer();
    });
    window.addEventListener("hashchange", applyRoute);
    window.addEventListener("beforeunload", (e) => {
      if (route() === "preferences" && prefsDirty()) { e.preventDefault(); e.returnValue = ""; }
    });

    $("#prefsSave").onclick = savePrefs;
    $("#prefsView").addEventListener("scroll", syncPrefsNav, { passive: true });
    $("#prefsReset").onclick = () => { prefs = clone(savedPrefs); renderPrefsKeepScroll(); };

    $("#addBtn").onclick = openAdd;
    $$("#addDialog [data-close]").forEach((b) => b.onclick = closeAdd);
    $("#addDialog").addEventListener("mousedown", (e) => { if (e.target.id === "addDialog") closeAdd(); });
    $("#addForm").onsubmit = submitAdd;
    $("#addMode").addEventListener("click", (e) => { const b = e.target.closest("[data-mode]"); if (b) setAddMode(b.dataset.mode); });
    $("#importBtn").onclick = importFromLink;
    $("#importUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); importFromLink(); } });
    $("#addForm").company.addEventListener("input", () => { clearTimeout(dupeT); dupeT = setTimeout(checkDupes, 300); });
    $("#addForm").job_url.addEventListener("input", () => { clearTimeout(dupeT); dupeT = setTimeout(checkDupes, 300); });

    $("#scrapeBtn").onclick = startScrape;
    $("#pullMore").addEventListener("click", (e) => {
      e.stopPropagation();
      $("#pullMenu").classList.contains("hidden") ? openPullMenu() : closePullMenu();
    });
    $("#descFetchBtn").onclick = (e) => { e.preventDefault(); closePullMenu(); startDescriptions(); };
    document.addEventListener("click", (e) => { if (!e.target.closest("#pullGroup")) closePullMenu(); });
    $("#scrapeLogBtn").onclick = () => $("#scrapeLog").classList.toggle("hidden");

    $("#themeBtn").onclick = () => {
      const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = t;
      store.set("theme", t);
    };

    document.addEventListener("keydown", (e) => {
      const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      if (e.key === "Escape") {
        if (!$("#navMenu").classList.contains("hidden")) closeMenu();
        else if (!$("#pullMenu").classList.contains("hidden")) closePullMenu();
        else if (!$("#confirmDialog").classList.contains("hidden")) $("#confirmDialog [data-cclose]").click();
        else if (!$("#stageDialog").classList.contains("hidden")) $("#stageDialog").classList.add("hidden");
        else if (!$("#addDialog").classList.contains("hidden")) closeAdd();
        else if (typing) document.activeElement.blur();
        else if (current) closeDrawer();
        return;
      }
      if (route() !== "roles") return;
      if (e.key === "/" && !typing) { e.preventDefault(); $("#q").focus(); }
      if (!typing && current && (e.key === "j" || e.key === "k")) {
        const i = roles.findIndex((r) => r.id === current.id);
        const next = roles[i + (e.key === "j" ? 1 : -1)];
        if (next) { openRole(next.id); $(`#rows tr[data-id="${next.id}"]`)?.scrollIntoView({ block: "nearest" }); }
        else {
          const btn = $(`#pager [data-page="${state.page + (e.key === "j" ? 1 : -1)}"]:not([disabled])`);
          if (btn) { const fwd = e.key === "j"; state.page += fwd ? 1 : -1;
            load().then(() => { const r = fwd ? roles[0] : roles[roles.length - 1]; if (r) openRole(r.id); }); }
        }
      }
    });
  }

  async function init() {
    document.documentElement.dataset.theme = store.get("theme", "dark");
    META = await api("/api/meta");
    STATUSES = META.statuses;
    $("#dbPath").textContent = META.db_path;
    // The running version ("local" = this folder's code with unreleased changes; see CHANGELOG.md).
    if (META.version) $("#appVersion").textContent = META.launched_as === "local"
      ? `Version ${META.version} + local changes` : `Version ${META.launched_as || META.version}`;
    RESUMES = (await api("/api/resumes")).files;
    wire();
    await applyRoute();
    pollTask();
  }

  init().catch((e) => toast("Could not reach the server: " + e.message, "error"));
})();
