/* Film Club Tracker — vanilla SPA. No build step. */
(() => {
  "use strict";

  // ---------- tiny helpers ----------
  const $ = (sel, el = document) => el.querySelector(sel);
  const app = $("#app");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // Stable per-tab id, sent on every mutating request so the live-update stream
  // can tell us to ignore the echo of our own change (we already updated in place).
  const CLIENT_ID = (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : "c" + Math.random().toString(36).slice(2) + Date.now().toString(36);

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID, ...(opts.headers || {}) },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    if (res.status === 401) {
      // Clear stale identity-only sessions before showing login. The next normal
      // Plex login stores the per-user token needed for rating synchronization.
      await fetch("/auth/logout", { method: "POST" }).catch(() => {});
      state.me = null;
      disconnectEvents();
      render();
      throw new Error("unauth");
    }
    let data = null;
    try { data = await res.json(); } catch { /* no body */ }
    if (!res.ok) {
      const error = new Error((data && data.detail) || `HTTP ${res.status}`);
      error.data = data;
      throw error;
    }
    return data;
  }

  function toast(msg, isErr = false) {
    const t = document.createElement("div");
    t.className = "toast" + (isErr ? " err" : "");
    t.textContent = msg;
    $("#toast-root").appendChild(t);
    setTimeout(() => t.remove(), 3200);
  }

  const state = {
    me: null,
    route: null,
    currentHash: null,
    memberBackHash: "#/stats",
    todo: { backlog: 0, watched: 0 },
    setupRequired: false,
  };

  // Per-member reminder counts driving the nav badges. Refreshed at boot and
  // after any action that changes what the member still needs to mark/rate.
  async function refreshTodo() {
    try { state.todo = await api("/api/me/todo"); } catch (e) { /* keep last */ }
    updateNavBadges();
  }
  function updateNavBadges() {
    ["backlog", "watched"].forEach((view) => {
      const link = app.querySelector(`.nav a[href="#/${view}"]`);
      if (!link) return;
      let badge = link.querySelector(".nav-badge");
      const n = state.todo[view] || 0;
      if (n > 0) {
        if (!badge) { badge = document.createElement("span"); badge.className = "nav-badge"; link.appendChild(badge); }
        badge.textContent = n > 99 ? "99+" : String(n);
      } else if (badge) {
        badge.remove();
      }
    });
  }

  // ---------- live updates (Server-Sent Events) ----------
  // A single stream pushes a "something changed" ping whenever anyone mutates
  // data. We respond by refreshing the badges and re-rendering the current view,
  // so remote changes (a new backlog film, a vote, a rating) appear without a
  // manual refresh. Our own changes are filtered out server-side by client id.
  let _es = null, _esOpened = false, _remoteTimer = null, _remoteRefreshRunning = false;

  function connectEvents() {
    if (_es || !window.EventSource) return;
    _es = new EventSource("/api/events");
    _es.onmessage = (e) => {
      let msg = null;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg && msg.client === CLIENT_ID) return; // echo of my own action
      scheduleRemoteRefresh();
    };
    _es.onopen = () => {
      // On a *re*connect we may have missed events while offline — resync.
      if (_esOpened) scheduleRemoteRefresh();
      _esOpened = true;
    };
    // onerror: EventSource reconnects on its own; nothing to do.
  }

  function disconnectEvents() {
    if (_es) { _es.close(); _es = null; _esOpened = false; }
  }

  // Debounced so a burst of events causes a single refresh.
  function scheduleRemoteRefresh(delay = 250) {
    clearTimeout(_remoteTimer);
    _remoteTimer = setTimeout(async () => {
      if (!state.me) return;
      // Don't clobber anything the user is mid-edit (rating note, search box,
      // display-name field) or an open modal. Keep the refresh pending so the
      // remote change appears as soon as the interaction is finished.
      const ae = document.activeElement;
      const editing = ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName);
      if (editing || $("#modal-root").hasChildNodes() || _remoteRefreshRunning) {
        scheduleRemoteRefresh(750);
        return;
      }
      _remoteRefreshRunning = true;
      const y = window.scrollY;
      const hash = location.hash;
      coverageMemberIndex = {};
      try {
        await refreshTodo();
        await render({ preserve: true });
        if (location.hash === hash) requestAnimationFrame(() => window.scrollTo(0, y));
      } finally {
        _remoteRefreshRunning = false;
      }
    }, delay);
  }

  // ---------- shared render bits ----------
  function avatar(member, cls = "") {
    if (!member) return `<span class="avatar ${cls}">?</span>`;
    const initials = (member.username || "?").slice(0, 2).toUpperCase();
    // Always render the coloured-initials style (ignore Plex thumbs) so every
    // avatar looks the same — placeholder and real accounts alike.
    const style = `background:${member.color};border-color:${member.color}`;
    return `<span class="avatar ${cls}" style="${style}" title="${esc(member.username)}">${esc(initials)}</span>`;
  }

  function oneStar(frac) {
    // a single star filled `frac` (0..1) of the way with the star colour
    const pct = Math.max(0, Math.min(1, frac)) * 100;
    const gid = `g_${Math.random().toString(36).slice(2, 8)}`;
    return `<svg viewBox="0 0 24 24"><defs><linearGradient id="${gid}">`
      + `<stop offset="${pct}%" stop-color="var(--star)"/>`
      + `<stop offset="${pct}%" stop-color="#3a3a44"/></linearGradient></defs>`
      + `<path fill="url(#${gid})" d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/></svg>`;
  }

  function starsSvg(score, max = 5) {
    // read-only star row with half support
    let out = `<span class="stars" aria-label="${score} of ${max}">`;
    for (let i = 1; i <= max; i++) out += oneStar(score - (i - 1));
    return out + "</span>";
  }

  function posterEl(m, cls = "") {
    if (m.poster_url) return `<img src="${esc(m.poster_url)}" alt="${esc(m.title)}" loading="lazy" class="${cls}">`;
    return `<div class="poster-fallback"><span class="pf-title">${esc(m.title)}</span><span>${m.year || ""}</span></div>`;
  }

  function fmtRuntime(mins) {
    if (!mins) return "";
    const h = Math.floor(mins / 60), m = mins % 60;
    return h ? `${h}h ${m}m` : `${m}m`;
  }

  function rottenTomatoes(m, extra = "") {
    const rt = m.library && m.library.rotten_tomatoes;
    if (!rt) return "";
    const critic = rt.critic != null
      ? `<span class="rt-score rt-critic" title="Rotten Tomatoes critics">🍅 ${rt.critic}%</span>` : "";
    const audience = rt.audience != null
      ? `<span class="rt-score rt-audience" title="Rotten Tomatoes audience">🍿 ${rt.audience}%</span>` : "";
    return critic || audience ? `<span class="rt-scores ${extra}">${critic}${audience}</span>` : "";
  }

  function cardFilmMetadata(m) {
    const language = m.language ? `<span>${esc(m.language)}</span>` : "";
    const rt = rottenTomatoes(m, "compact");
    return `<div class="film-metadata">${language}${rt}</div>`;
  }

  function fmtDate(s) {
    if (!s) return "";
    const d = new Date(s.length <= 10 ? s + "T00:00:00" : s);
    if (isNaN(d)) return s.slice(0, 10);
    return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }

  // ---------- app shell ----------
  const NAV_ICONS = {
    thisweek: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="16" rx="2.5"/><path d="M3 9h18M8 2.5v4M16 2.5v4"/><path d="M9.5 14.5l2 2 3.5-4" stroke-width="1.7"/></svg>`,
    backlog: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7l16 0M4 12l16 0M4 17l10 0"/></svg>`,
    watched: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/></svg>`,
    stats: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M5 20V10M12 20V4M19 20v-7"/></svg>`,
    admin: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3.2"/><path d="M12 2.5v2.4M12 19.1v2.4M4.2 4.2l1.7 1.7M18.1 18.1l1.7 1.7M2.5 12h2.4M19.1 12h2.4M4.2 19.8l1.7-1.7M18.1 5.9l1.7-1.7"/></svg>`,
  };
  function navLink(active, view, label, badge = 0) {
    const b = badge > 0 ? `<span class="nav-badge">${badge > 99 ? "99+" : badge}</span>` : "";
    return `<a href="#/${view}" class="${active === view ? "active" : ""}">
      <span class="nav-ico">${NAV_ICONS[view]}</span><span class="nav-label">${label}</span>${b}</a>`;
  }
  function shell(active, body) {
    const m = state.me;
    return `
    <div class="appbar"><div class="appbar-inner">
      <div class="brand"><span class="dot">●</span> <span class="brand-name">Film Club</span></div>
      <nav class="nav">
        ${navLink(active, "thisweek", "This week")}
        ${navLink(active, "backlog", "Backlog", state.todo.backlog)}
        ${navLink(active, "watched", "Watched", state.todo.watched)}
        ${navLink(active, "stats", "Stats")}
        ${m && m.is_admin ? navLink(active, "admin", "Admin") : ""}
      </nav>
      <span class="spacer"></span>
      <div class="me">
        <button class="me-btn" id="me-btn" aria-haspopup="true" aria-expanded="false">
          ${avatar(m)}<span class="me-name">${esc(m ? m.username : "")}</span>
          <span class="me-caret">▾</span>
        </button>
        <div class="me-menu" id="me-menu" hidden>
          <a class="me-menu-item" href="#/profile" id="menu-profile">Profile</a>
          <button class="me-menu-item" id="logout-btn">Sign out</button>
        </div>
      </div>
    </div></div>
    <main>${body}</main>`;
  }

  // Remote updates repaint only the page body. Keeping the existing app bar
  // and skipping loading placeholders prevents the whole viewport from
  // disappearing whenever another member changes something.
  function paintView(active, body, preserve = false) {
    const main = preserve ? app.querySelector("main") : null;
    if (main) main.innerHTML = body;
    else app.innerHTML = shell(active, body);
    app.querySelectorAll(".nav a").forEach(link =>
      link.classList.toggle("active", !!active && link.getAttribute("href") === `#/${active}`));
    updateNavBadges();
  }

  function paintError(active, error, preserve = false) {
    if (preserve) toast("Couldn't refresh: " + error.message, true);
    else paintView(active, errBox(error));
  }

  // ---------- grid / list view toggle (backlog + watched) ----------
  const VIEW_ICONS = {
    grid: `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6"/></svg>`,
    list: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M8.5 6h12M8.5 12h12M8.5 18h12"/><circle cx="4" cy="6" r="1.5" fill="currentColor" stroke="none"/><circle cx="4" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="4" cy="18" r="1.5" fill="currentColor" stroke="none"/></svg>`,
  };
  const VIEW_KEY = "fc_view";
  let viewMode = localStorage.getItem(VIEW_KEY) === "list" ? "list" : "grid";
  function setViewMode(v) {
    viewMode = v === "list" ? "list" : "grid";
    try { localStorage.setItem(VIEW_KEY, viewMode); } catch (e) {}
  }
  function viewToggle() {
    return `<div class="view-toggle" role="group" aria-label="View mode">${["grid", "list"].map(v =>
      `<button type="button" class="vt-btn ${viewMode === v ? "active" : ""}" data-view="${v}" aria-pressed="${viewMode === v}" title="${v === "grid" ? "Grid view" : "List view"}" aria-label="${v === "grid" ? "Grid view" : "List view"}">${VIEW_ICONS[v]}</button>`).join("")}</div>`;
  }
  function wireViewToggle(rerender) {
    app.querySelectorAll(".vt-btn").forEach(b => b.onclick = () => {
      if (b.dataset.view !== viewMode) { setViewMode(b.dataset.view); rerender(); }
    });
  }
  // ---------- login ----------
  const SETTING_FIELDS = [
    ["APP_URL", "Film Club URL", "The exact address members open in their browser."],
    ["TMDB_API_KEY", "TMDB API key", "Required for film search and metadata."],
    ["PLEX_URL", "Plex server URL", "An http(s) address reachable from this container."],
    ["PLEX_TOKEN", "Plex owner token", "Used for library enrichment; stored encrypted."],
    ["PLEX_MACHINE_ID", "Plex machine identifier", "Authorizes accounts with access to this server."],
    ["PLEX_WEBHOOK_SECRET", "Plex webhook secret", "Optional. Enables inbound rating sync."],
    ["PLEX_REFRESH_INTERVAL", "Plex refresh interval", "Seconds between library refreshes (minimum 60)."],
    ["SEERR_URL", "Seerr URL", "Optional Overseerr/Jellyseerr server address."],
    ["SEERR_API_KEY", "Seerr API key", "Optional; required when a Seerr URL is set."],
    ["SEERR_TIMEOUT", "Seerr timeout", "Request timeout in seconds."],
  ];

  function settingsFields(settings, setup = false) {
    return SETTING_FIELDS.map(([key, label, hint]) => {
      const meta = settings[key] || {};
      const type = meta.secret ? "password" : key.includes("TIMEOUT") || key.includes("INTERVAL") ? "number" : "text";
      const required = meta.required ? "required" : "";
      const locked = meta.locked ? "disabled" : "";
      const placeholder = meta.secret && meta.configured ? "Saved — leave blank to keep" : "";
      const clear = !setup && meta.secret && meta.configured && !meta.required
        ? `<span class="setup-clear"><input type="checkbox" name="clear:${key}"> Clear saved value</span>` : "";
      return `<label class="setup-field" data-setting="${key}">
        <span>${esc(label)}${meta.required ? " *" : ""}${meta.locked ? ` <small>environment override</small>` : ""}</span>
        <input class="search-input" name="${key}" type="${type}" value="${esc(meta.value || "")}" placeholder="${placeholder}" ${required} ${locked} autocomplete="off">
        <span class="setup-hint">${esc(hint)}</span>${clear}<span class="setup-error"></span>
      </label>`;
    }).join("");
  }

  function collectSettings(form) {
    const values = {};
    const clear = [];
    new FormData(form).forEach((value, key) => {
      if (key.startsWith("clear:")) clear.push(key.slice(6));
      else values[key] = value;
    });
    if (clear.length) values.clear_secrets = clear;
    ["PLEX_REFRESH_INTERVAL", "SEERR_TIMEOUT"].forEach(key => {
      if (values[key] !== "") values[key] = Number(values[key]);
    });
    return values;
  }

  function showSettingsErrors(form, errors = {}) {
    form.querySelectorAll(".setup-field").forEach(field => {
      const msg = errors[field.dataset.setting] || "";
      field.classList.toggle("invalid", !!msg);
      field.querySelector(".setup-error").textContent = msg;
    });
  }

  async function renderSetup() {
    let status;
    try { status = await api("/api/setup/status"); }
    catch (e) { app.innerHTML = `<div class="login-wrap"><div class="login-card">${esc(e.message)}</div></div>`; return; }
    if (!status.required) { state.setupRequired = false; bootAuthenticated(); return; }
    if (status.settings.APP_URL && !status.settings.APP_URL.locked) status.settings.APP_URL.value = window.location.origin;
    app.innerHTML = `<div class="setup-wrap"><form class="setup-card" id="setup-form">
      <div class="setup-kicker">First-run setup</div><h1>Set up Film Club</h1>
      <p>Enter the one-time setup code shown in <code>docker compose logs filmclub</code>, then connect your services. Secrets are encrypted before they are stored.</p>
      <label class="setup-field"><span>Setup code *</span><input class="search-input" name="setup_code" required autocomplete="off" placeholder="XXXX-XXXX-XXXX"></label>
      <div class="setup-grid">${settingsFields(status.settings, true)}</div>
      <div class="setup-actions"><span id="setup-message"></span><button class="btn btn-primary" type="submit">Validate and finish setup</button></div>
    </form></div>`;
    const form = $("#setup-form");
    form.onsubmit = async (event) => {
      event.preventDefault(); showSettingsErrors(form);
      const button = form.querySelector("button[type=submit]");
      button.disabled = true; button.textContent = "Validating…";
      $("#setup-message").textContent = "Checking Plex and TMDB…";
      try {
        await api("/api/setup", { method: "POST", body: collectSettings(form) });
        location.href = "/auth/login";
      } catch (e) {
        showSettingsErrors(form, (e.data && e.data.errors) || {});
        $("#setup-message").textContent = e.message;
        button.disabled = false; button.textContent = "Validate and finish setup";
      }
    };
  }

  function renderLogin() {
    app.innerHTML = `<div class="login-wrap"><div class="login-card">
      <h1>🎬 Film Club</h1>
      <p>Sign in with your Plex account. You'll need access to the club's Plex server.</p>
      <a class="login-btn" href="/auth/login">Sign in with Plex</a>
    </div></div>`;
  }

  // ---------- profile ----------
  async function renderProfile(preserve = false) {
    const m = state.me;
    if (!preserve) paintView(null, `<div class="empty">Loading profile…</div>`);
    let p;
    try { p = await api(`/api/members/${m.id}/profile`); }
    catch (e) { if (e.message !== "unauth") paintError(null, e, preserve); return; }
    const plexName = m.plex_username || m.username;
    const current = m.display_name || "";
    const s = p.stats;
    const connected = !!m.plex_rating_sync_connected;
    const syncEnabled = connected && m.plex_rating_sync_enabled !== false;
    const isDev = (m.plex_id || "").startsWith("dev:");
    const syncTitle = isDev ? "Plex sync unavailable in development" : syncEnabled ? "Plex ratings sync on" : connected ? "Plex ratings sync off" : "Plex ratings sync unavailable";
    const recentRatings = p.ratings.slice(0, 4);
    const recentSuggestions = p.suggestions.slice(0, 4);
    const avg = s.mean_score_given != null ? s.mean_score_given.toFixed(2) : "—";
    const body = `
      <header class="profile-hero">
        ${avatar(m, "xl")}
        <div class="profile-hero-copy">
          <div class="profile-eyebrow">Your Film Club profile</div>
          <h1>${esc(m.username)}</h1>
          <div class="profile-sub">Plex account: ${esc(plexName)}</div>
        </div>
        <a class="btn profile-public" href="#/member/${m.id}">View public profile</a>
      </header>
      <div class="profile-kpis" aria-label="Your Film Club activity">
        <div><strong>${s.suggested}</strong><span>Suggested</span></div>
        <div><strong>${s.suggested_watched}</strong><span>Picked & watched</span></div>
        <div><strong>${s.ratings_count}</strong><span>Ratings given</span></div>
        <div><strong>${avg}</strong><span>Average score</span></div>
      </div>
      <div class="profile-grid">
        <section class="profile-section profile-settings">
          <div class="section-heading"><div><h2>Account settings</h2><p>Choose how your name appears to the club.</p></div></div>
          <label class="profile-field">
            <span class="profile-label">Display name</span>
            <input type="text" id="display-name" class="search-input" maxlength="40"
              placeholder="${esc(plexName)}" value="${esc(current)}" autocomplete="off">
            <span class="profile-hint">Leave blank to use your Plex username, “${esc(plexName)}”.</span>
          </label>
          <div class="profile-actions"><button class="btn btn-primary" id="save-profile">Save changes</button></div>
          <div class="profile-sync ${syncEnabled ? "connected" : ""}">
            <span class="profile-sync-dot" aria-hidden="true"></span>
            <div><strong>${syncTitle}</strong></div>
            ${!isDev && connected ? `<label class="profile-sync-switch" title="Turn Plex ratings sync ${syncEnabled ? "off" : "on"}">
              <input type="checkbox" id="plex-sync-toggle" role="switch" aria-label="Plex ratings sync" ${syncEnabled ? "checked" : ""}>
              <span aria-hidden="true"></span>
            </label>` : !isDev ? `<a class="profile-sync-link" href="/auth/login">Connect</a>` : ""}
          </div>
        </section>
        <section class="profile-section">
          <div class="section-heading"><div><h2>Recent activity</h2><p>Your latest ratings and suggestions.</p></div></div>
          <div class="profile-activity">
            ${recentRatings.length ? `<div><h3>Ratings</h3>${recentRatings.map(pfRatingRow).join("")}</div>` : ""}
            ${recentSuggestions.length ? `<div><h3>Suggestions</h3>${recentSuggestions.map(pfSuggestionRow).join("")}</div>` : ""}
            ${!recentRatings.length && !recentSuggestions.length ? `<div class="empty">Your activity will appear here.</div>` : ""}
          </div>
        </section>
      </div>`;
    paintView(null, body, preserve);
    const input = $("#display-name");
    $("#save-profile").onclick = saveProfile;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") saveProfile(); });
    const syncToggle = $("#plex-sync-toggle");
    if (syncToggle) syncToggle.onchange = () => setRatingSync(syncToggle);
  }

  async function setRatingSync(toggle) {
    const enabled = toggle.checked;
    toggle.disabled = true;
    try {
      state.me = await api("/api/me/plex-rating-sync", { method: "PATCH", body: { enabled } });
      toast(`Plex ratings sync ${enabled ? "enabled" : "paused"}`);
      renderProfile(true);
    } catch (e) {
      toggle.checked = !enabled;
      toggle.disabled = false;
      toast("Couldn't update Plex sync: " + e.message, true);
    }
  }

  async function saveProfile() {
    const val = $("#display-name").value.trim();
    try {
      const updated = await api("/api/me", { method: "PATCH", body: { display_name: val || null } });
      state.me = updated;
      coverageMemberIndex = {}; // names may have changed; force a member re-fetch
      toast("Profile saved");
      renderProfile(true);
    } catch (e) { toast("Couldn't save: " + e.message, true); }
  }

  // ---------- public member profile (#/member/<id>) ----------
  const PF_STATUS = {
    watched: `<span class="pf-status watched">Watched</span>`,
    scheduled: `<span class="pf-status sched">This week</span>`,
    suggested: `<span class="pf-status backlog">Backlog</span>`,
  };

  function pfPoster(mv) {
    return `<span class="pf-poster">${mv.poster_url ? `<img src="${esc(mv.poster_url)}" alt="" loading="lazy">` : ""}</span>`;
  }

  function pfSuggestionRow(mv) {
    const rating = mv.avg_rating != null ? `<span class="pf-rate">★ ${mv.avg_rating.toFixed(1)}</span>` : "";
    return `<a class="pf-film" href="#/movie/${mv.id}">${pfPoster(mv)}
      <span class="pf-film-body">
        <span class="pf-film-title">${esc(mv.title)} <span class="yr">${mv.year || ""}</span></span>
        <span class="pf-film-sub">${PF_STATUS[mv.status] || ""}${rating}</span>
        ${profileFilmMetadata(mv)}
      </span></a>`;
  }

  function profileFilmMetadata(mv) {
    const language = mv.language ? `<span>${esc(mv.language)}</span>` : "";
    const rt = rottenTomatoes(mv, "compact");
    return language || rt ? `<span class="pf-film-metadata">${language}${rt}</span>` : "";
  }

  function pfRatingRow(r) {
    const mv = r.movie;
    return `<a class="pf-film" href="#/movie/${mv.id}">${pfPoster(mv)}
      <span class="pf-film-body">
        <span class="pf-film-title">${esc(mv.title)} <span class="yr">${mv.year || ""}</span></span>
        <span class="pf-film-sub">${starsSvg(r.score)} <b class="pf-score">${r.score.toFixed(1)}</b>${r.seen_before ? ` <span class="rr-tag">rewatch</span>` : ""}</span>
        ${profileFilmMetadata(mv)}
        ${r.note ? `<span class="pf-note">${esc(r.note)}</span>` : ""}
      </span></a>`;
  }

  async function renderMemberProfile(id, preserve = false) {
    if (!preserve) paintView(null, `<div class="empty">Loading…</div>`);
    let p;
    try { p = await api(`/api/members/${id}/profile`); }
    catch (e) { if (e.message !== "unauth") paintError(null, e, preserve); return; }

    const m = p.member;
    const s = p.stats;
    const isMe = m.id === state.me.id;
    const meta = [
      p.created_at && !p.is_placeholder ? `Member since ${fmtDate(p.created_at)}` : "",
      m.is_admin ? "Admin" : "",
    ].filter(Boolean).join("  ·  ");
    const fx = (v) => v != null ? v.toFixed(2) : "—";
    const kpis = [
      ["Suggested", s.suggested],
      ["Picked & watched", s.suggested_watched],
      ["Ratings given", s.ratings_count],
      ["Avg score they give", fx(s.mean_score_given)],
      ["Their picks' avg", fx(s.picks_mean_received)],
    ];

    const body = `
      <div class="detail-topbar">
        <a class="back-link" href="${esc(state.memberBackHash || "#/stats")}">← Back</a>
        ${isMe ? `<a class="btn" href="#/profile">Edit profile</a>` : ""}
      </div>
      <div class="pf-head">${avatar(m, "xl")}
        <div><h1>${esc(m.username)}</h1>${meta ? `<div class="pf-sub">${meta}</div>` : ""}</div>
      </div>
      <div class="stat-card wide" style="margin-bottom:1.3rem"><div class="kpi-row">
        ${kpis.map(k => `<div class="kpi"><div class="n">${k[1]}</div><div class="l">${k[0]}</div></div>`).join("")}
      </div></div>
      <div class="stats-grid">
        <div class="stat-card"><h3>Suggestions <span class="pf-h-count">${p.suggestions.length}</span></h3>
          ${p.suggestions.length ? p.suggestions.map(pfSuggestionRow).join("") : `<div class="empty" style="padding:1.5rem">No suggestions yet.</div>`}
        </div>
        <div class="stat-card"><h3>Ratings <span class="pf-h-count">${p.ratings.length}</span></h3>
          ${p.ratings.length ? p.ratings.map(pfRatingRow).join("") : `<div class="empty" style="padding:1.5rem">No ratings yet.</div>`}
        </div>
      </div>`;
    paintView(null, body, preserve);
  }

  // A clickable member chip (avatar + name) that links to the public profile.
  function memberLink(m, extra = "") {
    if (!m) return `<span class="member-cell">${avatar(null, "sm")}—</span>`;
    return `<a class="member-cell member-link" href="#/member/${m.id}">${avatar(m, "sm")}${esc(m.username)}${extra}</a>`;
  }

  // Compact suggester treatment used on movie cards and the This week hero.
  function suggesterLink(m, prefix = "") {
    const label = prefix ? `<span class="suggester-prefix">${esc(prefix)}</span>` : "";
    if (!m) return `${label}<span class="suggester-person">${avatar(null, "sm")}<span class="who">—</span></span>`;
    return `${label}<span class="suggester-person">${avatar(m, "sm")}<a class="member-link who" href="#/member/${m.id}">${esc(m.username)}</a></span>`;
  }

  // ---------- shared: seen/not-seen segmented control ----------
  const STATE_TO_SEEN = { seen: true, notseen: false, unknown: null };

  // Two-segment control: tap "Seen it" or "Not seen" to set that state directly;
  // tap the already-active one to clear back to unknown. Clearer than a cycle.
  function seenControl(id, myState) {
    return `<div class="seen-seg" data-seen="${id}" data-state="${myState}">
      <button type="button" class="seg" data-set="seen">Seen it</button>
      <button type="button" class="seg" data-set="notseen">Not seen</button>
    </div>`;
  }

  function coverageLineHtml(c, mode) {
    const total = c.total_members;
    if (mode === "thisweek") {
      return `<b>${c.seen_ids.length} of ${total}</b> watched`;
    }
    return `<b>${c.unseen_count} of ${total}</b> haven't seen this${c.unknown_count ? ` · <span style="color:var(--text-faint)">${c.unknown_count} unknown</span>` : ""}`;
  }

  async function setSeen(seg, target) {
    const id = seg.dataset.seen;
    const cur = seg.dataset.state;
    const next = cur === target ? "unknown" : target;
    seg.dataset.state = next; // optimistic highlight
    try {
      const res = await api(`/api/movies/${id}/prior_view`, { method: "POST", body: { seen: STATE_TO_SEEN[next] } });
      updateCardCoverage(id, res.coverage);
    } catch (e) { seg.dataset.state = cur; toast("Couldn't save: " + e.message, true); }
  }

  function wireSeenControls() {
    app.querySelectorAll(".seen-seg .seg").forEach(btn => btn.onclick = (e) => {
      e.stopPropagation();
      setSeen(btn.closest(".seen-seg"), btn.dataset.set);
    });
  }

  // ================= THIS WEEK (home) =================
  async function renderThisWeek(preserve = false) {
    if (!preserve) paintView("thisweek", `<div class="empty">Loading…</div>`);
    let data;
    try { data = await api("/api/thisweek"); }
    catch (e) { if (e.message !== "unauth") paintError("thisweek", e, preserve); return; }

    const items = data.items;
    const body = items.length
      ? items.map(thisWeekHero).join("")
      : `<div class="tw-empty">
           <div class="tw-empty-emoji">🍿</div>
           <h2>Nothing picked for this week yet</h2>
           <p>Open a film in the <a href="#/backlog">Backlog</a> and choose “Pick as this week’s movie.”</p>
         </div>`;
    paintView("thisweek", body, preserve);
    wireThisWeekCards();
  }

  // Full-bleed single-film hero for the current pick (usually one): backdrop
  // banner with poster + synopsis, the editable discussion date, who's-watched
  // avatars next to the suggester, and your own seen control — all in one panel.
  function thisWeekHero(m) {
    const c = m.coverage;
    const myState = c.seen_ids.includes(state.me.id) ? "seen"
      : c.not_seen_ids.includes(state.me.id) ? "notseen" : "unknown";
    const date = m.watched_at ? fmtDate(m.watched_at) : "";
    const facts = [
      m.year || "",
      m.director ? esc(m.director) : "",
      m.language ? esc(m.language) : "",
      fmtRuntime(m.runtime),
    ].filter(Boolean).join("  ·  ");

    return `<article class="tw-hero">
      ${m.backdrop_url ? `<img class="tw-backdrop" src="${esc(m.backdrop_url)}" alt="">` : ""}
      <div class="tw-scrim"></div>
      <div class="tw-hero-inner">
        <div class="tw-poster" data-nav="${m.id}" title="Open film page">${posterEl(m)}</div>
        <div class="tw-main">
          <div class="tw-eyebrow">This week's pick</div>
          <h1>${esc(m.title)}</h1>
          <div class="tw-facts">${facts}</div>
          ${rottenTomatoes(m, "hero")}
          ${date ? `<div class="tw-date-wrap">
            <button type="button" class="tw-date" data-date-edit="${m.id}" title="Change discussion date">🗓 Discussing <b class="tw-date-val">${date}</b> <span class="tw-date-caret">▾</span></button>
            <input type="date" class="tw-date-input" data-date-input="${m.id}" value="${(m.watched_at || "").slice(0, 10)}" aria-label="Discussion date">
          </div>` : ""}
          ${(m.genres || []).length ? `<div class="genre-chips">${m.genres.map(g => `<span class="chip">${esc(g)}</span>`).join("")}</div>` : ""}
          <p class="tw-synopsis">${esc(m.overview || "No synopsis available.")}</p>
          ${m.library && m.library.deep_link ? `<div class="tw-actions"><a class="tw-plex-btn" href="${esc(m.library.deep_link)}" target="_blank" rel="noopener">▶ Watch on Plex</a></div>` : ""}
          <div class="tw-hero-foot" data-id="${m.id}" data-cov-mode="thisweek">
            <div class="tw-suggester">${suggesterLink(m.suggester, "Suggested by ")}</div>
            <div class="tw-watchers">
              <span class="coverage-line tw-watchers-label">${coverageLineHtml(c, "thisweek")}</span>
              <div class="cov-avatars">${coverageAvatars(c)}</div>
            </div>
          </div>
          <div class="tw-you-bar">
            <span class="tw-you-label">Have you watched it?</span>
            ${seenControl(m.id, myState)}
            <a class="tw-rate-link" href="#/movie/${m.id}">Rate &amp; discuss →</a>
          </div>
        </div>
      </div>
    </article>`;
  }

  function wireThisWeekCards() {
    app.querySelectorAll("[data-nav]").forEach(el =>
      el.onclick = () => { location.hash = `#/movie/${el.dataset.nav}`; });
    wireSeenControls();
    app.querySelectorAll("[data-date-edit]").forEach(btn => {
      const input = app.querySelector(`[data-date-input="${btn.dataset.dateEdit}"]`);
      if (!input) return;
      btn.onclick = () => { try { input.showPicker(); } catch { input.focus(); } };
      input.onchange = () => setDiscussDate(btn.dataset.dateEdit, input.value, btn);
    });
  }

  async function setDiscussDate(id, isoDate, btn) {
    if (!isoDate) return;
    try {
      const res = await api(`/api/movies/${id}/discuss_date`, { method: "POST", body: { date: isoDate } });
      const valEl = btn.querySelector(".tw-date-val");
      if (valEl) valEl.textContent = fmtDate(res.date || isoDate);
      toast("Discussion date updated");
    } catch (e) { toast("Couldn't update date: " + e.message, true); }
  }

  // ================= BACKLOG =================
  const backlogState = { sort: "seconds", suggester: null, query: "" };
  let backlogItems = [];  // full fetched list; filtering happens client-side

  async function renderBacklog(preserve = false) {
    if (!preserve) paintView("backlog", skeletonGrid());
    let data;
    try {
      data = await api(`/api/backlog?sort=${backlogState.sort}`);
    } catch (e) { if (e.message !== "unauth") paintError("backlog", e, preserve); return; }
    backlogItems = data.items;
    paintBacklog(preserve);
  }

  const isFilteringBacklog = () =>
    backlogState.suggester != null || backlogState.query.trim() !== "";

  function filterBacklog(items) {
    const q = backlogState.query.trim().toLowerCase();
    const sid = backlogState.suggester;
    return items.filter(m => {
      if (sid != null && (!m.suggester || m.suggester.id !== sid)) return false;
      if (q && !(m.title || "").toLowerCase().includes(q)) return false;
      return true;
    });
  }

  // "Suggested by" options built from the full (unfiltered) list, so every
  // suggester stays selectable regardless of the active filter.
  function backlogSuggesterOptions() {
    const counts = new Map();
    backlogItems.forEach(m => {
      if (!m.suggester) return;
      const e = counts.get(m.suggester.id) || { member: m.suggester, n: 0 };
      e.n++; counts.set(m.suggester.id, e);
    });
    const sel = (v) => backlogState.suggester === v ? "selected" : "";
    let opts = `<option value="" ${sel(null)}>All suggesters</option>`;
    const mine = counts.get(state.me.id);
    if (mine) opts += `<option value="${state.me.id}" ${sel(state.me.id)}>Your suggestions (${mine.n})</option>`;
    [...counts.values()]
      .filter(e => e.member.id !== state.me.id)
      .sort((a, b) => a.member.username.localeCompare(b.member.username))
      .forEach(e => { opts += `<option value="${e.member.id}" ${sel(e.member.id)}>${esc(e.member.username)} (${e.n})</option>`; });
    return opts;
  }

  const SORT_LABELS = { seconds: "Most seconded", unseen: "Unseen count", date: "Date suggested", title: "Title", year: "Year", runtime: "Runtime" };

  function backlogCountText() {
    const total = backlogItems.length;
    return isFilteringBacklog() ? `${filterBacklog(backlogItems).length} of ${total}` : `${total} suggested`;
  }

  const ICON_FILTER = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 5h18M6 12h12M10 19h4"/></svg>`;

  function backlogGridHtml() {
    const filtered = filterBacklog(backlogItems);
    if (filtered.length) {
      return `<div class="${viewMode === "list" ? "list" : "grid"}">${filtered.map(viewMode === "list" ? backlogRow : backlogCard).join("")}</div>`;
    }
    return (backlogItems.length && isFilteringBacklog())
      ? `<div class="empty">No films match your filters. <button class="btn-link" id="clear-filters" type="button">Clear filters</button></div>`
      : `<div class="empty">Nothing here yet. Add the first suggestion.</div>`;
  }

  // Chips showing which filters are active — each individually clearable, plus
  // a "Clear all". Only rendered while a filter is active.
  function activeFiltersBar() {
    if (!isFilteringBacklog()) return "";
    const chips = [];
    if (backlogState.suggester != null) {
      const name = backlogState.suggester === state.me.id ? "Your suggestions" : (suggesterName(backlogState.suggester) || "Suggester");
      chips.push(`<button type="button" class="filter-chip" data-clear="suggester" title="Clear this filter"><span>By ${esc(name)}</span><span class="fc-x" aria-hidden="true">×</span></button>`);
    }
    const q = backlogState.query.trim();
    if (q) chips.push(`<button type="button" class="filter-chip" data-clear="query" title="Clear search"><span>“${esc(q)}”</span><span class="fc-x" aria-hidden="true">×</span></button>`);
    return `<div class="active-filters"><span class="af-label">Filtered:</span>${chips.join("")}<button type="button" class="btn-link" id="clear-all-filters">Clear all</button></div>`;
  }

  function backlogResults() { return activeFiltersBar() + backlogGridHtml(); }

  function paintBacklog(preserve = false) {
    const sortOpts = Object.keys(SORT_LABELS).map(o => `<option value="${o}" ${backlogState.sort === o ? "selected" : ""}>${SORT_LABELS[o]}</option>`).join("");
    const body = `
      <div class="page-head backlog-head">
        <div class="ph-title"><h1>Backlog</h1><span class="count" id="backlog-count">${backlogCountText()}</span></div>
        <div class="toolbar">
          <input type="search" id="backlog-search" class="ctl-search grow" placeholder="Find a film…" value="${esc(backlogState.query)}" autocomplete="off" aria-label="Find a film by title">
          <div class="filterbar">
            <button type="button" class="filterbar-toggle btn ${backlogState.suggester != null ? "has-active" : ""}" id="filter-toggle" aria-expanded="false" aria-controls="filter-panel">${ICON_FILTER}<span>Filters</span></button>
            <div class="filterbar-body" id="filter-panel">
              ${viewToggle()}
              <label>Sort <select id="sort-sel">${sortOpts}</select></label>
              <label>By <select id="suggester-sel">${backlogSuggesterOptions()}</select></label>
            </div>
          </div>
          <button class="btn btn-primary" id="add-btn"><span class="add-full">+ Add suggestion</span><span class="add-short">+ Add</span></button>
        </div>
      </div>
      <div id="backlog-results">${backlogResults()}</div>`;
    paintView("backlog", body, preserve);

    $("#sort-sel").onchange = (e) => { backlogState.sort = e.target.value; renderBacklog(true); };
    $("#suggester-sel").onchange = (e) => { backlogState.suggester = e.target.value ? parseInt(e.target.value, 10) : null; refreshBacklogResults(); };
    $("#backlog-search").oninput = () => { backlogState.query = $("#backlog-search").value; refreshBacklogResults(); };
    $("#add-btn").onclick = openSearchModal;
    wireViewToggle(() => renderBacklog(true));
    wireFilterPanel();
    wireBacklogResults();
  }

  // The filter group shows inline on desktop (CSS); on mobile the toggle opens it
  // as a popover panel. One outside-click listener closes it.
  function wireFilterPanel() {
    const toggle = $("#filter-toggle"), panel = $("#filter-panel");
    if (!toggle || !panel) return;
    toggle.onclick = (e) => {
      e.stopPropagation();
      const open = panel.classList.toggle("open");
      toggle.setAttribute("aria-expanded", String(open));
    };
    // Keep panel interactions from bubbling to the document closer.
    panel.onclick = (e) => e.stopPropagation();
    if (!wireFilterPanel._doc) {
      wireFilterPanel._doc = true;
      document.addEventListener("click", () => {
        const p = $("#filter-panel"), t = $("#filter-toggle");
        if (p && p.classList.contains("open")) { p.classList.remove("open"); if (t) t.setAttribute("aria-expanded", "false"); }
      });
    }
  }

  // Re-render only the results + count in place (no refetch) so the search box
  // keeps its focus and caret while the user is typing.
  function refreshBacklogResults() {
    const res = $("#backlog-results");
    if (res) res.innerHTML = backlogResults();
    const c = $("#backlog-count");
    if (c) c.textContent = backlogCountText();
    wireBacklogResults();
  }

  function wireBacklogResults() {
    wireBacklogCards();
    bindClearFilters();
    app.querySelectorAll(".filter-chip[data-clear]").forEach(b => b.onclick = () => {
      if (b.dataset.clear === "suggester") backlogState.suggester = null;
      else if (b.dataset.clear === "query") { backlogState.query = ""; const s = $("#backlog-search"); if (s) s.value = ""; }
      syncSuggesterSelect(); refreshBacklogResults();
    });
    const clrAll = $("#clear-all-filters");
    if (clrAll) clrAll.onclick = clearBacklogFilters;
    updateFilterDot();
  }

  function clearBacklogFilters() {
    backlogState.suggester = null; backlogState.query = "";
    const s = $("#backlog-search"); if (s) s.value = "";
    syncSuggesterSelect(); refreshBacklogResults();
  }
  function syncSuggesterSelect() {
    const s = $("#suggester-sel");
    if (s) s.value = backlogState.suggester != null ? String(backlogState.suggester) : "";
  }
  function updateFilterDot() {
    const t = $("#filter-toggle");
    if (t) t.classList.toggle("has-active", backlogState.suggester != null);
  }
  function bindClearFilters() {
    const clr = $("#clear-filters");
    if (clr) clr.onclick = clearBacklogFilters;
  }

  function myCovState(c) {
    return c.seen_ids.includes(state.me.id) ? "seen"
      : c.not_seen_ids.includes(state.me.id) ? "notseen" : "unknown";
  }

  function backlogCard(m) {
    const c = m.coverage;
    const myState = myCovState(c);
    const needsMe = myState === "unknown";
    const cls = "card" + (needsMe ? " needs-me" : "");
    const titleHint = m.year ? `${m.title} (${m.year})` : m.title;
    return `<div class="${cls}" data-id="${m.id}" data-cov-mode="backlog">
      <div class="poster-wrap" data-nav="${m.id}">
        ${statusIcon(m)}
        ${posterEl(m)}
        ${voteControl(m, "card")}
      </div>
      <div class="accent-bar" style="background:${m.suggester ? m.suggester.color : "var(--line)"}"></div>
      <div class="backlog-card-details">
        <div class="card-title" title="${esc(titleHint)}">${esc(m.title)}</div>
        <div class="backlog-card-meta">
          <div class="card-meta">${suggesterLink(m.suggester)}</div>
          <div class="cov-summary">${backlogSeenSummary(c)}</div>
        </div>
        <div class="card-actions">${seenControl(m.id, myState)}</div>
      </div>
    </div>`;
  }

  function backlogRow(m) {
    const c = m.coverage;
    const myState = myCovState(c);
    const needsMe = myState === "unknown";
    const cls = "card list-row" + (needsMe ? " needs-me" : "");
    const titleHint = m.year ? `${m.title} (${m.year})` : m.title;
    return `<div class="${cls}" data-id="${m.id}" data-cov-mode="backlog">
      <div class="lr-poster" data-nav="${m.id}">${statusIcon(m)}${posterEl(m)}</div>
      <div class="lr-content">
        <div class="lr-body" data-nav="${m.id}">
          <div class="lr-title" title="${esc(titleHint)}">${esc(m.title)}</div>
          <div class="lr-meta">${suggesterLink(m.suggester)}</div>
          <div class="lr-cov cov-summary">${backlogSeenSummary(c)}</div>
        </div>
        <div class="lr-actions">${voteControl(m, "list")}${seenControl(m.id, myState)}</div>
      </div>
    </div>`;
  }

  // Seerr-style corner status icon. Green check = the film is on your Plex
  // server (any library film, refreshed on the Plex interval); blue clock =
  // requested from Seerr but not on the server yet. `m.library` is the live
  // membership signal and wins, so a requested film flips to the check as soon
  // as it lands on Plex — a stale 'requested' seerr_status can't override it.
  const ICON_CHECK = `<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.2 4.2L19 7"/></svg>`;
  const ICON_CLOCK = `<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>`;

  function statusIcon(m) {
    const onServer = !!m.library || m.seerr_status === "available";
    if (onServer) {
      return `<span class="status-icon on-server" title="On your Plex server" aria-label="On your Plex server">${ICON_CHECK}</span>`;
    }
    if (m.seerr_status === "requested") {
      return `<span class="status-icon requested" title="Requested — waiting for it to download" aria-label="Requested from Seerr">${ICON_CLOCK}</span>`;
    }
    return "";
  }

  // ---------- voting (compact "+1": I also want the club to watch it) ----------
  function secondsCaption(count) {
    if (!count) return "";
    return count === 1 ? "1 person wants to watch this" : `${count} people want to watch this`;
  }

  // Visible content of a clickable +1 chip. Once votes exist, keep the plus
  // prefix so the tally still reads as an upvote count in every visual state.
  function voteChipInner(count, variant) {
    return `<span class="pv-lbl">${count > 0 ? `+${count}` : "+1"}</span>`;
  }

  // A compact +1 chip. Returns "" when there's nothing to show (suggester of a
  // film with no seconds). Suggesters can't vote their own pick — they get a
  // static count (no "+1"); everyone else gets a clickable chip.
  function voteControl(m, variant = "card") {
    const count = m.vote_count || 0;
    if (!m.can_vote) {
      if (count <= 0) return "";
      return `<span class="pv pv-${variant} pv-static" title="${secondsCaption(count)}" aria-label="${secondsCaption(count)}"><span class="pv-lbl">+${count}</span></span>`;
    }
    const title = m.voted ? "You +1'd this — click to undo" : "+1 · you'd also watch this";
    return `<button type="button" class="pv pv-${variant} ${m.voted ? "voted" : ""}" data-vote="${m.id}" data-voted="${m.voted ? "1" : "0"}" data-variant="${variant}" aria-pressed="${m.voted}" title="${title}" aria-label="${title}">${voteChipInner(count, variant)}</button>`;
  }

  // Caption shown beside the chip on the movie detail page (which has room).
  function detailCaption(count, canVote) {
    if (count > 0) return secondsCaption(count);
    return canVote ? "Be the first to +1 this" : "";
  }
  function detailVoteBlock(m) {
    if (m.status !== "suggested") return "";
    const chip = voteControl(m, "detail");
    const capText = detailCaption(m.vote_count || 0, m.can_vote);
    if (!chip && !capText) return "";
    const cap = capText ? `<span class="vote-caption" data-vote-cap="${m.id}">${capText}</span>` : "";
    return `<div class="detail-vote">${chip}${cap}</div>`;
  }

  function wireVoteButtons() {
    app.querySelectorAll(".pv[data-vote]").forEach(bindVoteButton);
  }
  function bindVoteButton(btn) {
    btn.onclick = (e) => { e.stopPropagation(); toggleVote(btn); };
  }

  async function toggleVote(btn) {
    const id = btn.dataset.vote;
    const next = btn.dataset.voted !== "1";
    try {
      const res = await api(`/api/movies/${id}/vote`, { method: "POST", body: { voted: next } });
      updateVoteControl(id, res);
    } catch (e) { toast("Couldn't save: " + e.message, true); }
  }

  // Re-render every vote chip (and the detail caption) for this movie after a toggle.
  function updateVoteControl(id, res) {
    app.querySelectorAll(`.pv[data-vote="${id}"]`).forEach((btn) => {
      btn.dataset.voted = res.voted ? "1" : "0";
      btn.classList.toggle("voted", res.voted);
      btn.setAttribute("aria-pressed", String(res.voted));
      const title = res.voted ? "You +1'd this — click to undo" : "+1 · you'd also watch this";
      btn.title = title;
      btn.setAttribute("aria-label", title);
      btn.innerHTML = voteChipInner(res.vote_count, btn.dataset.variant || "card");
    });
    const cap = app.querySelector(`.vote-caption[data-vote-cap="${id}"]`);
    if (cap) cap.textContent = detailCaption(res.vote_count, true);
  }

  // Corner "needs your attention" pill on a poster. Absolutely positioned, so it
  // never affects card height (keeps the grid even). kind: "seen" | "rate".
  function attnMarker(kind) {
    const label = kind === "rate" ? "Rate it" : "Mark it";
    const title = kind === "rate"
      ? "You haven't rated this yet"
      : "You haven't marked whether you've seen this";
    return `<span class="attn-marker attn-${kind}" title="${title}">${label}</span>`;
  }

  function coverageAvatars(c) {
    const byId = coverageMemberIndex;
    const chunk = (ids, klass) => ids.map(id => {
      const mem = byId[id];
      const init = mem ? mem.username.slice(0, 2).toUpperCase() : "?";
      const style = klass === "unknown" ? "" : `background:${mem ? mem.color : "#555"};border-color:${mem ? mem.color : "#555"}`;
      const av = `<span class="avatar ${klass}" style="${style}" title="${esc(mem ? mem.username : "?")} — ${klass}">${klass === "unknown" ? "?" : esc(init)}</span>`;
      return mem ? `<a class="member-link cov-member" href="#/member/${mem.id}" aria-label="View ${esc(mem.username)}'s profile">${av}</a>` : av;
    }).join("");
    return chunk(c.not_seen_ids, "unseen") + chunk(c.unknown_ids, "unknown") + chunk(c.seen_ids, "seen");
  }

  function backlogSeenSummary(c) {
    return `<span class="cov-sum"><span class="cov-sum-label"><b>${c.seen_ids.length}/${c.total_members}</b> seen</span></span>`;
  }

  function suggesterName(id) {
    const m = backlogItems.find(x => x.suggester && x.suggester.id === id);
    return m ? m.suggester.username : null;
  }

  let coverageMemberIndex = {};
  async function ensureMembers() {
    if (Object.keys(coverageMemberIndex).length) return;
    const ms = await api("/api/members");
    coverageMemberIndex = Object.fromEntries(ms.map(m => [m.id, m]));
  }

  function wireBacklogCards() {
    app.querySelectorAll("[data-nav]").forEach(el =>
      el.onclick = (e) => {
        if (e.target.closest && e.target.closest(".member-link")) return;
        location.hash = `#/movie/${el.dataset.nav}`;
      });
    wireSeenControls();
    wireVoteButtons();
  }

  // Delete a backlog film. Allowed for an admin or the member who added it; the
  // backend enforces the same. The confirm() guards against an accidental click.
  async function deleteMovie(id, title, backRoute) {
    if (!confirm(`Delete "${title}"?\n\n`
      + `This permanently removes the film and any seen/rating data for it. `
      + `This can't be undone.`)) return;
    try {
      await api(`/api/movies/${id}`, { method: "DELETE" });
      toast("Deleted");
      refreshTodo();
      location.hash = `#/${backRoute}`;
    } catch (e) { toast("Couldn't delete: " + e.message, true); }
  }

  // Update a card's coverage block in place after a seen toggle. Wording depends
  // on the card's context (backlog = "haven't seen", this week = "watched it").
  function updateCardCoverage(id, c) {
    // Matches a backlog `.card` or the This-week `.tw-hero-foot` for this film.
    const card = app.querySelector(`[data-cov-mode][data-id="${id}"]`);
    if (!card) return;
    const mode = card.dataset.covMode || "backlog";
    if (mode === "thisweek") {
      // This-week hero keeps its full watched line + avatar row (preserved).
      const line = $(".coverage-line", card);
      if (line) line.innerHTML = coverageLineHtml(c, mode);
      const avs = $(".cov-avatars", card);
      if (avs) avs.innerHTML = coverageAvatars(c);
      return;
    }
    // Backlog card: refresh the simple seen tally + attention state.
    const sum = $(".cov-summary", card);
    if (sum) sum.innerHTML = backlogSeenSummary(c);
    // A film I've now marked (seen or not-seen) no longer needs my attention.
    const needsMe = !c.seen_ids.includes(state.me.id) && !c.not_seen_ids.includes(state.me.id);
    card.classList.toggle("needs-me", needsMe);
    refreshTodo();
  }

  // ================= WATCHED =================
  async function renderWatched(preserve = false) {
    if (!preserve) paintView("watched", skeletonGrid());
    let data;
    try { data = await api("/api/watched"); }
    catch (e) { if (e.message !== "unauth") paintError("watched", e, preserve); return; }
    const items = data.items;
    const body = `
      <div class="page-head"><h1>Watched</h1><span class="count">${items.length} films</span>
        <div class="controls">${viewToggle()}</div>
      </div>
      <div class="${viewMode === "list" ? "list" : "grid"}">${items.map(viewMode === "list" ? watchedRow : watchedCard).join("")}</div>
      ${items.length ? "" : `<div class="empty">No films watched yet.</div>`}`;
    paintView("watched", body, preserve);
    wireViewToggle(() => renderWatched(true));
    app.querySelectorAll("[data-nav]").forEach(el =>
      el.onclick = (e) => {
        if (e.target.closest && e.target.closest(".member-link")) return;
        location.hash = `#/movie/${el.dataset.nav}`;
      });
  }

  // The club average, and — held separately so it's easy to distinguish — the
  // current user's own rating (or a calm "Rate" invite when they haven't).
  const STAR_ICO = `<span class="star-ico">★</span>`;
  function watchedCard(m) {
    const club = m.avg_rating != null
      ? `<span class="badge club-rating" title="Club average">${STAR_ICO} ${m.avg_rating.toFixed(1)}</span>`
      : `<span class="badge" title="No ratings yet">Unrated</span>`;
    const mine = m.my_rated
      ? `<span class="badge you-rating" title="Your rating">You ${STAR_ICO} ${m.my_score.toFixed(1)}</span>`
      : `<span class="badge rate-prompt" title="You haven't rated this yet">Rate</span>`;
    return `<div class="card${m.my_rated ? "" : " to-rate"}" data-id="${m.id}">
      <div class="poster-wrap" data-nav="${m.id}">
        <div class="badges">${club}${mine}</div>
        ${posterEl(m)}
      </div>
      <div class="accent-bar" style="background:${m.suggester ? m.suggester.color : "var(--line)"}"></div>
      <div class="card-title">${esc(m.title)} <span class="yr">${m.year || ""}</span></div>
      ${cardFilmMetadata(m)}
      <div class="card-meta">${suggesterLink(m.suggester)}
        <span class="rated-count">${m.rating_count}/${m.total_members} rated</span>
      </div>
    </div>`;
  }

  function watchedRow(m) {
    const club = m.avg_rating != null
      ? `<span class="lr-rating">${starsSvg(m.avg_rating)}<span class="lr-rating-num">${m.avg_rating.toFixed(1)}</span></span>`
      : `<span class="lr-rating lr-unrated">Unrated</span>`;
    const mine = m.my_rated
      ? `<span class="lr-you" title="Your rating">You ${STAR_ICO} ${m.my_score.toFixed(1)}</span>`
      : `<span class="lr-you lr-rate" title="You haven't rated this yet">Rate</span>`;
    return `<div class="card list-row${m.my_rated ? "" : " to-rate"}" data-id="${m.id}" data-nav="${m.id}">
      <div class="lr-poster">${posterEl(m)}</div>
      <div class="lr-content">
        <div class="lr-body">
          <div class="lr-title">${esc(m.title)} <span class="yr">${m.year || ""}</span></div>
          ${cardFilmMetadata(m)}
          <div class="lr-meta">${suggesterLink(m.suggester)}<span class="rated-count">${m.rating_count}/${m.total_members} rated</span></div>
        </div>
        <div class="lr-trailing">${club}${mine}</div>
      </div>
    </div>`;
  }

  // ================= MOVIE DETAIL =================
  async function renderDetail(id, preserve = false) {
    if (!preserve) paintView(null, `<div class="empty">Loading…</div>`);
    let m;
    try { m = await api(`/api/movies/${id}`); }
    catch (e) { if (e.message !== "unauth") paintError(null, e, preserve); return; }

    const isWatched = m.status === "watched";
    const isScheduled = m.status === "scheduled";
    const canRate = isScheduled || isWatched;
    const dateLabel = isWatched ? "Watched" : isScheduled ? "Discussing" : "Suggested";
    const dateVal = isWatched ? (m.watched_at || "").slice(0, 10)
      : isScheduled ? fmtDate(m.watched_at)
      : (m.suggested_at || "").slice(0, 10);
    const facts = [
      m.year || "",
      m.director ? esc(m.director) : "",
      m.language ? esc(m.language) : "",
      fmtRuntime(m.runtime),
      dateVal ? `${dateLabel} ${dateVal}` : "",
    ].filter(Boolean).join(" · ");

    const backRoute = isWatched ? "watched" : isScheduled ? "thisweek" : "backlog";
    const canDelete = m.status === "suggested"
      && (state.me.is_admin || (m.suggester && m.suggester.id === state.me.id));
    // Action hierarchy: one contextual PRIMARY action (accent), with any
    // reversal/destructive actions tucked into an overflow menu — de-emphasised
    // but still discoverable. All lifecycle behaviour + confirmations preserved.
    const eyebrow = isWatched ? "Watched" : isScheduled ? "This week's pick" : "On the backlog";
    let primaryBtn = "";
    const menuItems = [];
    if (m.status === "suggested") {
      primaryBtn = `<button class="btn btn-primary" id="detail-pick">▶ Pick for this week</button>`;
      if (canDelete) menuItems.push(`<button type="button" class="detail-menu-item danger" id="detail-del">Delete film…</button>`);
    } else if (isScheduled) {
      primaryBtn = `<button class="btn btn-primary" id="detail-archive">✓ Move to Watched</button>`;
      menuItems.push(`<button type="button" class="detail-menu-item danger" id="detail-unschedule">Send back to backlog…</button>`);
    } else if (isWatched) {
      if (state.me.is_admin) menuItems.push(`<button type="button" class="detail-menu-item" id="detail-return-thisweek">Move back to This Week…</button>`);
      menuItems.push(`<button type="button" class="detail-menu-item danger" id="detail-unwatch">Move back to backlog…</button>`);
    }
    const overflow = menuItems.length ? `<div class="detail-overflow">
      <button type="button" class="btn icon-btn" id="detail-more" aria-haspopup="true" aria-expanded="false" aria-label="More actions">${ICON_MORE}</button>
      <div class="detail-menu" id="detail-menu" hidden>${menuItems.join("")}</div>
    </div>` : "";
    const plexLink = m.library && m.library.deep_link
      ? `<a class="tw-plex-btn detail-plex" href="${esc(m.library.deep_link)}" target="_blank" rel="noopener">▶ Watch on Plex</a>` : "";

    const body = `
      <div class="detail-topbar">
        <a class="back-link" href="#/${backRoute}">← Back</a>
        <div class="detail-actions">${primaryBtn}${overflow}</div>
      </div>
      <div class="detail-hero">
        ${m.backdrop_url ? `<img class="backdrop" src="${esc(m.backdrop_url)}" alt="">` : ""}
        <div class="scrim"></div>
        <div class="detail-inner">
          <div class="detail-poster">${posterEl(m)}</div>
          <div class="detail-main">
            <div class="detail-eyebrow">${eyebrow}</div>
            <h1>${esc(m.title)}</h1>
            <div class="detail-facts">${facts}</div>
            ${rottenTomatoes(m, "detail-rt")}
            <div class="card-meta detail-suggester">Suggested by ${m.suggester ? `<a class="member-link inline" href="#/member/${m.suggester.id}">${avatar(m.suggester, "sm")}${esc(m.suggester.username)}</a>` : "—"}</div>
            ${detailVoteBlock(m)}
            ${(m.genres || []).length ? `<div class="genre-chips">${m.genres.map(g => `<span class="chip">${esc(g)}</span>`).join("")}</div>` : ""}
            ${m.overview ? `<p class="overview">${esc(m.overview)}</p>` : ""}
            ${plexLink}
          </div>
        </div>
      </div>
      <div class="detail-body">
        <section class="dp-col dp-primary">${ratingsPanel(m, canRate)}</section>
        <section class="dp-col dp-secondary">${coveragePanel(m)}</section>
      </div>`;
    paintView(null, body, preserve);
    wireVoteButtons();
    if (canRate) wireRatingInput(m);
    wireDetailMenu();
    const pickBtn = $("#detail-pick");
    if (pickBtn) pickBtn.onclick = () => scheduleMovie(m.id);
    const archiveBtn = $("#detail-archive");
    if (archiveBtn) archiveBtn.onclick = () => archiveMovie(m.id);
    const delBtn = $("#detail-del");
    if (delBtn) delBtn.onclick = () => deleteMovie(m.id, m.title, backRoute);
    const unschedBtn = $("#detail-unschedule");
    if (unschedBtn) unschedBtn.onclick = () => backToBacklog(m.id, m.title, "unschedule");
    const returnThisWeekBtn = $("#detail-return-thisweek");
    if (returnThisWeekBtn) returnThisWeekBtn.onclick = () => returnToThisWeek(m.id, m.title);
    const unwatchBtn = $("#detail-unwatch");
    if (unwatchBtn) unwatchBtn.onclick = () => backToBacklog(m.id, m.title, "unwatch");
  }

  const ICON_MORE = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>`;

  // Overflow menu on the detail page (reversal/destructive actions).
  function wireDetailMenu() {
    const btn = $("#detail-more"), menu = $("#detail-menu");
    if (!btn || !menu) return;
    btn.onclick = (e) => {
      e.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    };
    menu.onclick = (e) => e.stopPropagation();
    if (!wireDetailMenu._doc) {
      wireDetailMenu._doc = true;
      document.addEventListener("click", () => {
        const mn = $("#detail-menu"), bt = $("#detail-more");
        if (mn && !mn.hidden) { mn.hidden = true; if (bt) bt.setAttribute("aria-expanded", "false"); }
      });
    }
  }

  async function scheduleMovie(id) {
    try {
      await api(`/api/movies/${id}/schedule`, { method: "POST" });
      toast("Picked as this week’s movie");
      refreshTodo();
      location.hash = "#/thisweek";
    } catch (e) { toast("Couldn't pick it: " + e.message, true); }
  }

  async function archiveMovie(id) {
    try {
      await api(`/api/movies/${id}/watch`, { method: "POST" });
      toast("Moved to Watched");
      refreshTodo();
      location.hash = "#/watched";
    } catch (e) { toast("Couldn't move it: " + e.message, true); }
  }

  async function returnToThisWeek(id, title) {
    if (!confirm(`Move "${title}" back to This Week?\n\nIts ratings and all other movie data will be kept.`)) return;
    try {
      await api(`/api/movies/${id}/return-to-this-week`, { method: "POST" });
      toast("Moved back to This Week");
      refreshTodo();
      location.hash = "#/thisweek";
    } catch (e) { toast("Couldn't move it back: " + e.message, true); }
  }

  // Undo a pick or archive back to the backlog without discarding its history.
  async function backToBacklog(id, title, endpoint) {
    if (!confirm(`Move "${title}" back to the backlog?\n\nIts ratings and all other movie data will be kept.`)) return;
    try {
      await api(`/api/movies/${id}/${endpoint}`, { method: "POST" });
      toast("Moved back to backlog");
      refreshTodo();
      location.hash = "#/backlog";
    } catch (e) { toast("Couldn't move it back: " + e.message, true); }
  }

  function coveragePanel(m) {
    const c = m.coverage;
    const isWatched = m.status === "watched";
    const isScheduled = m.status === "scheduled";
    const rows = (ids, klass, label) => ids.length ? `<div class="coverage-group">
      <div class="rating-group-label">${label}</div>
      <div class="coverage-people">${ids.map(id => {
        const mem = coverageMemberIndex[id] || (m.members || []).find(x => x.id === id);
        const style = klass === "unknown" ? "" : `background:${mem ? mem.color : "#555"};border-color:${mem ? mem.color : "#555"}`;
        const av = `<span class="avatar sm ${klass}" style="${style}">${klass === "unknown" ? "?" : esc((mem ? mem.username : "?").slice(0, 2).toUpperCase())}</span>`;
        return mem
          ? `<a class="coverage-person" href="#/member/${mem.id}">${av}<span>${esc(mem.username)}</span></a>`
          : `<span class="coverage-person">${av}<span>Unknown member</span></span>`;
      }).join("")}</div></div>` : "";
    const tag = {
      eligible: `<span class="elig-tag elig-eligible">Eligible</span>`,
      ineligible: `<span class="elig-tag elig-ineligible">Everyone's seen it</span>`,
      unconfirmed: `<span class="elig-tag elig-unconfirmed">Unconfirmed</span>`,
    }[c.eligibility];
    const heading = isWatched ? "Prior views at watch"
      : isScheduled ? "Who's watched it" : "Pick eligibility";
    // On the This-week pick, "seen" means "has watched it this week".
    const line = isScheduled
      ? `<b>${c.seen_ids.length} of ${c.total_members}</b> watched it${c.unknown_count ? ` · ${c.unknown_count} unmarked` : ""}`
      : `<b>${c.unseen_count} of ${c.total_members}</b> hadn't seen it${c.unknown_count ? ` · ${c.unknown_count} unknown` : ""}`;
    return `<h3>${heading} ${isScheduled ? "" : tag}</h3>
      <div class="coverage-line" style="margin-bottom:.6rem">${line}</div>
      ${rows(c.not_seen_ids, "unseen", isScheduled ? "Not yet watched" : "Hadn't seen")}
      ${rows(c.seen_ids, "seen", isScheduled ? "Watched it" : "Had seen")}
      ${rows(c.unknown_ids, "unknown", "Unknown")}`;
  }

  function ratingsPanel(m, canRate) {
    if (!canRate) {
      return `<h3>Ratings</h3><p style="color:var(--text-faint)">Ratings open once this film is picked as this week's movie.</p>`;
    }
    if (!m.ratings_public) {
      return `<h3>Ratings</h3>${ratingInput(m)}
        <p style="color:var(--text-faint);margin:.75rem 0 0">Your rating stays private until this film is moved to Watched.</p>`;
    }
    const first = m.ratings.filter(r => !r.seen_before);
    const rewatch = m.ratings.filter(r => r.seen_before);

    const meansBlock = `
      <div class="means-row">
        <div class="mean-stat"><div class="n">${m.avg_rating != null ? m.avg_rating.toFixed(2) : "—"}</div><div class="l">All ratings (${m.ratings.length})</div></div>
        ${first.length ? `<div class="mean-stat"><div class="n">${m.first_watch_mean.toFixed(2)}</div><div class="l">First watch (${first.length})</div></div>` : ""}
        ${rewatch.length ? `<div class="mean-stat"><div class="n">${m.rewatch_mean.toFixed(2)}</div><div class="l">Rewatch (${rewatch.length})</div></div>` : ""}
      </div>`;

    const rowHtml = (r) => `<div class="rating-row">${r.member ? `<a href="#/member/${r.member.id}" class="rr-avatar">${avatar(r.member, "")}</a>` : avatar(r.member, "")}
      <div class="rr-body">
        <div class="rr-head"><span class="rr-name">${r.member ? `<a class="member-link plain" href="#/member/${r.member.id}">${esc(r.member.username)}</a>` : "?"}</span>
          <span class="rr-tag">${r.seen_before ? "rewatch" : "first"}</span>
          <span class="rr-score">${starsSvg(r.score)} ${r.score.toFixed(1)}</span></div>
        ${r.note ? `<div class="rr-note">${esc(r.note)}</div>` : ""}
      </div></div>`;

    const groups = [];
    if (first.length) groups.push(`<div class="rating-group-label">First watch</div>${first.map(rowHtml).join("")}`);
    if (rewatch.length) groups.push(`<div class="rating-group-label">Rewatch</div>${rewatch.map(rowHtml).join("")}`);
    if (!m.ratings.length) groups.push(`<p style="color:var(--text-faint)">No ratings yet — be the first.</p>`);

    return `<h3>Ratings</h3>${meansBlock}${ratingInput(m)}${groups.join("")}`;
  }

  function ratingInput(m) {
    const mine = m.ratings.find(r => r.member_id === state.me.id);
    const startScore = mine ? mine.score : 0;
    const seenBefore = mine ? mine.seen_before : m.my_rating_default.seen_before;
    return `<div id="rate-box" style="border-bottom:1px solid var(--line);padding-bottom:1rem;margin-bottom:.4rem">
      <div class="rating-group-label" style="border:none;padding:0;margin:.2rem 0">${mine ? "Your rating" : "Add your rating"}</div>
      <div class="star-input" id="star-input" data-score="${startScore}">
        ${[1,2,3,4,5].map(i => `<span class="star-slot" data-i="${i}">${oneStar(startScore - (i - 1))}</span>`).join("")}
        <span class="val">${startScore ? startScore.toFixed(1) : "—"}</span>
      </div>
      <div class="rate-controls">
        <label class="toggle-pill"><input type="checkbox" id="seen-before-cb" ${seenBefore ? "checked" : ""}> Had you seen this before?</label>
      </div>
      <textarea class="rate-note" id="rate-note" placeholder="Optional note…">${esc(mine ? (mine.note || "") : "")}</textarea>
      <div style="margin-top:.6rem"><button class="btn btn-primary" id="save-rating">${mine ? "Update rating" : "Save rating"}</button></div>
    </div>`;
  }

  function wireRatingInput(m) {
    const box = $("#star-input");
    if (!box) return;
    let current = parseFloat(box.dataset.score) || 0;
    const valEl = $(".val", box);

    const paint = (score) => {
      box.querySelectorAll(".star-slot").forEach((slot) => {
        const i = parseInt(slot.dataset.i, 10);
        slot.innerHTML = oneStar(score - (i - 1));
      });
      valEl.textContent = score ? score.toFixed(1) : "—";
    };

    box.querySelectorAll(".star-slot").forEach((slot) => {
      const i = parseInt(slot.dataset.i, 10);
      slot.onmousemove = (e) => {
        const r = slot.getBoundingClientRect();
        const half = (e.clientX - r.left) < r.width / 2 ? 0.5 : 1;
        paint(i - 1 + half);
      };
      slot.onmouseleave = () => paint(current);
      slot.onclick = (e) => {
        const r = slot.getBoundingClientRect();
        const half = (e.clientX - r.left) < r.width / 2 ? 0.5 : 1;
        current = i - 1 + half;
        box.dataset.score = current;
        paint(current);
      };
    });

    $("#save-rating").onclick = async () => {
      if (!current || current < 0.5) { toast("Pick a score first", true); return; }
      try {
        const result = await api(`/api/movies/${m.id}/rating`, {
          method: "POST",
          body: { score: current, seen_before: $("#seen-before-cb").checked, note: $("#rate-note").value },
        });
        const sync = result.plex && result.plex.status;
        const message = sync === "synced" ? "Rating saved to Film Club and Plex"
          : sync === "not_connected" ? "Rating saved · connect Plex from your profile to enable sync"
          : sync === "not_in_library" ? "Rating saved · movie isn't in your Plex library"
          : sync === "failed" ? "Rating saved · Plex sync failed"
          : "Rating saved";
        toast(message, sync === "failed");
        refreshTodo();
        renderDetail(m.id, true);
      } catch (e) { toast("Couldn't save rating: " + e.message, true); }
    };
  }

  // ================= TMDB SEARCH MODAL =================
  function openSearchModal() {
    const root = $("#modal-root");
    root.innerHTML = `<div class="modal-backdrop" id="mb">
      <div class="modal">
        <div class="modal-head"><h2>Add a suggestion</h2><button class="modal-close" id="mc">×</button></div>
        <div class="modal-body">
          <input class="search-input" id="tmdb-q" placeholder="Search TMDB by title…" autocomplete="off" autofocus>
          <div class="search-results" id="tmdb-results"><div class="empty" style="padding:2rem">Type to search.</div></div>
        </div>
      </div></div>`;
    const close = () => { root.innerHTML = ""; };
    $("#mb").onclick = (e) => { if (e.target.id === "mb") close(); };
    $("#mc").onclick = close;

    const input = $("#tmdb-q");
    input.focus();
    let timer = null, seq = 0;
    input.oninput = () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (!q) { $("#tmdb-results").innerHTML = `<div class="empty" style="padding:2rem">Type to search.</div>`; return; }
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const { results } = await api(`/api/tmdb/search?q=${encodeURIComponent(q)}`);
          if (mine !== seq) return; // stale
          $("#tmdb-results").innerHTML = results.length
            ? results.map(r => `<div class="sr-item" data-tmdb="${r.tmdb_id}">
                ${r.poster_url ? `<img class="sr-poster" src="${esc(r.poster_url)}" alt="">` : `<div class="sr-poster"></div>`}
                <div class="sr-info"><div class="sr-title">${esc(r.title)} <span style="color:var(--text-faint)">${r.year || ""}</span></div>
                <div class="sr-sub">${r.director ? esc(r.director) + (r.language ? ` · ${esc(r.language)}` : "") : esc(r.overview || "")}</div></div></div>`).join("")
            : `<div class="empty" style="padding:2rem">No matches.</div>`;
          $("#tmdb-results").querySelectorAll("[data-tmdb]").forEach(el =>
            el.onclick = () => addSuggestion(el.dataset.tmdb, close));
        } catch (e) {
          $("#tmdb-results").innerHTML = `<div class="empty" style="padding:2rem;color:var(--bad)">${esc(e.message)}</div>`;
        }
      }, 280);
    };
  }

  // Map the Seerr auto-request outcome to a toast. `disabled` (feature off) and
  // any unknown status fall back to the plain "Added to backlog".
  const SEERR_TOAST = {
    in_library: "Added — already in your Plex library",
    available: "Added — already available in Seerr",
    requested: "Added — requested from Seerr",
    failed: "Added — Seerr request failed (added anyway)",
  };

  async function addSuggestion(tmdbId, close) {
    try {
      const res = await api("/api/movies", { method: "POST", body: { tmdb_id: parseInt(tmdbId, 10) } });
      const status = res && res.seerr && res.seerr.status;
      toast(SEERR_TOAST[status] || "Added to backlog");
      close();
      refreshTodo();
      renderBacklog(true);
    } catch (e) { toast(e.message, true); }
  }

  // ================= STATS =================
  async function renderStats(preserve = false) {
    if (!preserve) paintView("stats", `<div class="empty">Crunching numbers…</div>`);
    let s;
    try { s = await api("/api/stats"); }
    catch (e) { if (e.message !== "unauth") paintError("stats", e, preserve); return; }

    const memById = Object.fromEntries(s.members.map(m => [m.id, m]));
    const th = s.thresholds;

    const body = `
      <div class="page-head"><h1>Stats</h1>
        <span class="count">${s.totals.watched} watched · ${s.totals.ratings} ratings</span></div>
      <div class="stat-card wide" style="margin-bottom:1.3rem">
        <div class="kpi-row">
          <div class="kpi"><div class="n">${s.totals.watched}</div><div class="l">Films watched</div></div>
          <div class="kpi"><div class="n">${s.totals.suggested_open}</div><div class="l">On the backlog</div></div>
          <div class="kpi"><div class="n">${s.totals.group_mean != null ? s.totals.group_mean.toFixed(2) : "—"}</div><div class="l">Club mean score</div></div>
          <div class="kpi"><div class="n">${fmtRuntime(s.totals.total_runtime_minutes)}</div><div class="l">Total runtime</div></div>
          <div class="kpi"><div class="n">${s.totals.members}</div><div class="l">Members</div></div>
        </div>
      </div>
      <div class="stats-lead">
        ${divisivenessCard(s, th)}
        ${suggesterCard(s, th)}
      </div>
      <div class="stats-more">
        ${statsDisclosure("Members & suggestions", "Contribution, watch-rate, and rating habits.", suggestionsCard(s) + raterProfileCard(s, th))}
        ${statsDisclosure("Taste agreement", `Pairwise correlation; fewer than ${th.min_overlap} shared films are muted.`, `<div class="stat-card wide"><h3>Taste agreement matrix</h3>${matrixTable(s, memById)}</div>`)}
        ${statsDisclosure("First watches & rewatches", "How familiarity changes scores across films and members.", firstRewatchCard(s) + memberBiasCard(s, th))}
        ${statsDisclosure("Genres & decades", "The shape of the club's watched history.", genreCard(s) + decadeCard(s))}
      </div>`;
    paintView("stats", body, preserve);
  }

  function statsDisclosure(title, subtitle, content) {
    return `<details class="stats-disclosure">
      <summary><span><strong>${title}</strong><small>${subtitle}</small></span><span class="disclosure-plus" aria-hidden="true">+</span></summary>
      <div class="stats-disclosure-body">${content}</div>
    </details>`;
  }

  function hbar(label, value, max, color = "var(--text-dim)", suffix = "") {
    const pct = max ? Math.max(2, (value / max) * 100) : 0;
    return `<div class="hbar-row"><span class="lbl">${label}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${pct}%;background:${color}"></span></span>
      <span class="num">${value}${suffix}</span></div>`;
  }

  function suggestionsCard(s) {
    const rows = s.suggestions_per_member.slice().sort((a, b) => b.suggested - a.suggested);
    const max = Math.max(1, ...rows.map(r => r.suggested));
    return `<div class="stat-card"><h3>Suggestions & watch-rate</h3>
      <div class="sub">How many each person put up, and how many got picked.</div>
      <table class="stat-table"><thead><tr><th>Member</th><th class="num">Suggested</th><th class="num">Watched</th><th class="num">Rate</th></tr></thead><tbody>
      ${rows.map(r => `<tr>
        <td>${memberLink(r.member)}</td>
        <td class="num">${r.suggested}</td><td class="num">${r.watched}</td>
        <td class="num">${r.watch_rate != null ? Math.round(r.watch_rate * 100) + "%" : "—"}</td>
      </tr>`).join("")}
      </tbody></table></div>`;
  }

  function raterProfileCard(s, th) {
    const rows = s.rater_profiles.slice().sort((a, b) => (b.mean ?? -1) - (a.mean ?? -1));
    return `<div class="stat-card"><h3>Rater profiles</h3>
      <div class="sub">Mean, spread, and delta from the club mean. Under ${th.min_films_per_member} ratings = low confidence.</div>
      <table class="stat-table"><thead><tr><th>Member</th><th class="num">n</th><th class="num">Mean</th><th class="num">SD</th><th class="num">Δ group</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${r.low_confidence ? "lowconf" : ""}">
        <td>${memberLink(r.member, r.low_confidence ? `<span class="lowconf-tag">low n</span>` : "")}</td>
        <td class="num">${r.n}</td>
        <td class="num">${r.mean != null ? r.mean.toFixed(2) : "—"}</td>
        <td class="num">${r.stdev != null ? r.stdev.toFixed(2) : "—"}</td>
        <td class="num ${r.delta_from_group > 0 ? "pos" : r.delta_from_group < 0 ? "neg" : ""}">${r.delta_from_group != null ? (r.delta_from_group > 0 ? "+" : "") + r.delta_from_group.toFixed(2) : "—"}</td>
      </tr>`).join("")}
      </tbody></table></div>`;
  }

  function matrixColor(r) {
    // muted diverging: negative -> red, positive -> green
    if (r == null) return "var(--bg)";
    const a = Math.min(0.7, Math.abs(r) * 0.7 + 0.08);
    return r >= 0 ? `rgba(76,195,138,${a.toFixed(2)})` : `rgba(229,72,77,${a.toFixed(2)})`;
  }

  function matrixTable(s, memById) {
    const ids = s.agreement_matrix.member_ids;
    if (ids.length < 2) return `<div class="empty">Need at least two members.</div>`;
    const cells = s.agreement_matrix.cells;
    const head = `<tr><th></th>${ids.map(id => `<th title="${esc(memById[id].username)}">${avatar(memById[id], "sm")}</th>`).join("")}</tr>`;
    const rows = ids.map(a => `<tr><th style="text-align:right;padding-right:.5rem">${avatar(memById[a], "sm")}</th>${
      ids.map(b => {
        if (a === b) return `<td class="diag">—</td>`;
        const c = cells[`${a}:${b}`];
        if (!c || c.suppressed) return `<td class="supp" title="${c ? c.overlap : 0} shared">·<span class="ov">${c ? c.overlap : 0}</span></td>`;
        return `<td style="background:${matrixColor(c.r)}" title="r=${c.r}, ${c.overlap} shared">
          <span class="cellv">${c.r.toFixed(2)}</span><span class="ov">${c.overlap}</span></td>`;
      }).join("")}</tr>`).join("");
    return `<div style="overflow-x:auto"><table class="matrix">${head}${rows}</table></div>
      <div class="legend-note">Small numbers = count of films both members rated. Green = agree, red = disagree.</div>`;
  }

  function divisivenessCard(s, th) {
    const rows = s.divisiveness.filter(d => d.stdev != null).slice(0, 5);
    if (!rows.length) return `<div class="stat-card"><h3>Most divisive</h3><div class="empty">Not enough ratings.</div></div>`;
    return `<div class="stat-card"><h3>Most divisive films</h3>
      <div class="sub">Highest spread of scores. Under ${th.min_raters_per_film} raters flagged.</div>
      ${rows.map(d => `<div class="divis-row ${d.low_confidence ? "lowconf" : ""}">
        ${d.movie.poster_url ? `<img class="divis-poster" src="${esc(d.movie.poster_url)}">` : `<div class="divis-poster"></div>`}
        <div class="divis-info"><div class="divis-title">${esc(d.movie.title)}</div>
          <div class="divis-meta">${d.n} raters${d.low_confidence ? ` <span class="lowconf-tag">low n</span>` : ""}
          ${d.split_explains ? ` <span class="tag-split">split: first vs rewatch</span>` : ""}</div></div>
        <div class="divis-val">${d.stdev.toFixed(2)}</div>
      </div>`).join("")}</div>`;
  }

  function firstRewatchCard(s) {
    const rows = s.first_vs_rewatch_delta.slice(0, 8);
    if (!rows.length) return `<div class="stat-card"><h3>First-watch vs rewatch</h3>
      <div class="empty">No film yet has raters on both sides.</div></div>`;
    return `<div class="stat-card"><h3>First-watch vs rewatch gap</h3>
      <div class="sub">Rewatchers' mean minus first-timers' mean. Positive = the newcomers enjoyed it less.</div>
      ${rows.map(d => `<div class="divis-row">
        ${d.movie.poster_url ? `<img class="divis-poster" src="${esc(d.movie.poster_url)}">` : `<div class="divis-poster"></div>`}
        <div class="divis-info"><div class="divis-title">${esc(d.movie.title)}</div>
          <div class="divis-meta">rewatch ${d.rewatch_mean.toFixed(1)} (${d.rewatch_n}) · first ${d.first_watch_mean.toFixed(1)} (${d.first_watch_n})</div></div>
        <div class="divis-val ${d.delta > 0 ? "pos" : d.delta < 0 ? "neg" : ""}">${d.delta > 0 ? "+" : ""}${d.delta.toFixed(2)}</div>
      </div>`).join("")}</div>`;
  }

  function memberBiasCard(s, th) {
    const rows = s.member_rewatch_bias;
    return `<div class="stat-card"><h3>Per-member rewatch bias</h3>
      <div class="sub">Does someone score higher on rewatch than first watch? Needs 3+ on each side.</div>
      <table class="stat-table"><thead><tr><th>Member</th><th class="num">First</th><th class="num">Rewatch</th><th class="num">Δ</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${(!r.available || r.low_confidence) ? "lowconf" : ""}">
        <td>${memberLink(r.member, (r.available && r.low_confidence) ? `<span class="lowconf-tag">low n</span>` : "")}</td>
        <td class="num">${r.first_watch_mean != null ? r.first_watch_mean.toFixed(2) : "—"}<span style="color:var(--text-faint)"> (${r.first_watch_n})</span></td>
        <td class="num">${r.rewatch_mean != null ? r.rewatch_mean.toFixed(2) : "—"}<span style="color:var(--text-faint)"> (${r.rewatch_n})</span></td>
        <td class="num ${r.delta > 0 ? "pos" : r.delta < 0 ? "neg" : ""}">${r.available ? (r.delta > 0 ? "+" : "") + r.delta.toFixed(2) : "—"}</td>
      </tr>`).join("")}
      </tbody></table></div>`;
  }

  function suggesterCard(s, th) {
    const rows = s.suggester_scorecard.filter(r => r.avg_rating != null);
    if (!rows.length) return `<div class="stat-card"><h3>Suggester scorecard</h3><div class="empty">No rated suggestions yet.</div></div>`;
    const max = Math.max(...rows.map(r => r.avg_rating), 5);
    return `<div class="stat-card"><h3>Suggester scorecard</h3>
      <div class="sub">Average rating of each person's picks. Under ${th.min_films_per_member} films flagged.</div>
      ${rows.map(r => `<div class="hbar-row ${r.low_confidence ? "lowconf" : ""}">
        <span class="lbl">${esc(r.member.username)}${r.low_confidence ? ` <span class="lowconf-tag">low n</span>` : ""}</span>
        <span class="bar-track"><span class="bar-fill" style="width:${(r.avg_rating / 5) * 100}%;background:${r.member.color}"></span></span>
        <span class="num">${r.avg_rating.toFixed(2)}</span></div>`).join("")}</div>`;
  }

  function genreCard(s) {
    if (!s.genres.length) return "";
    const max = Math.max(...s.genres.map(g => g.count));
    return `<div class="stat-card"><h3>Genres</h3><div class="sub">Across watched films</div>
      ${s.genres.slice(0, 10).map(g => hbar(esc(g.genre), g.count, max)).join("")}</div>`;
  }

  function decadeCard(s) {
    if (!s.decades.length) return "";
    const max = Math.max(...s.decades.map(d => d.count));
    return `<div class="stat-card"><h3>Decades</h3><div class="sub">Release decade of watched films</div>
      ${s.decades.map(d => hbar(d.decade, d.count, max, "var(--accent)")).join("")}</div>`;
  }

  // ================= ADMIN =================
  async function renderAdmin(preserve = false) {
    if (!preserve) paintView("admin", `<div class="empty">Loading…</div>`);
    let data, configData;
    try { [data, configData] = await Promise.all([api("/api/admin/members"), api("/api/admin/settings")]); }
    catch (e) {
      if (e.message === "unauth") return;
      if (preserve) toast("Couldn't refresh: " + e.message, true);
      else paintView("admin", `<div class="empty">${e.message === "Admin access required" ? "You don't have admin access." : "Error: " + esc(e.message)}</div>`);
      return;
    }
    const members = data.members;
    const reals = members.filter(m => !m.is_placeholder);
    const placeholders = members.filter(m => m.is_placeholder);

    const typeBadge = (m) => m.is_owner ? `<span class="elig-tag elig-eligible">Owner</span>`
      : m.is_placeholder ? `<span class="elig-tag elig-unconfirmed">Placeholder</span>`
      : m.is_admin ? `<span class="elig-tag" style="color:var(--accent);background:rgba(91,141,239,.12)">Admin</span>`
      : `<span class="elig-tag" style="color:var(--text-dim);background:var(--bg-raise-2)">Member</span>`;

    const realOptions = (selId) => reals.map(r =>
      `<option value="${r.id}" ${selId === r.id ? "selected" : ""}>${esc(r.username)}</option>`).join("");

    const rowActions = (m) => {
      if (m.is_placeholder) {
        if (!reals.length) return `<span style="color:var(--text-faint);font-size:.8rem">no real accounts yet</span>`;
        const sel = m.suggested_merge ? m.suggested_merge.id : reals[0].id;
        return `<div class="admin-actions">
          <span style="color:var(--text-dim);font-size:.82rem">Merge into</span>
          <select class="merge-target" data-from="${m.id}">${realOptions(sel)}</select>
          <button class="btn merge-btn" data-from="${m.id}">Merge</button>
          ${m.suggested_merge ? `<span class="hint">matches ${esc(m.suggested_merge.username)}</span>` : ""}
        </div>`;
      }
      if (m.is_owner) return `<span style="color:var(--text-faint);font-size:.8rem">you · locked</span>`;
      return m.is_admin
        ? `<button class="btn admin-toggle" data-id="${m.id}" data-val="0">Remove admin</button>`
        : `<button class="btn admin-toggle" data-id="${m.id}" data-val="1">Make admin</button>`;
    };

    const table = (list, emptyMsg) => list.length ? `
      <table class="stat-table admin-table"><thead><tr>
        <th>Member</th><th></th><th class="num">Suggested</th><th class="num">Ratings</th><th>Actions</th>
      </tr></thead><tbody>
      ${list.map(m => `<tr>
        <td class="admin-member-cell"><div class="member-cell">${avatar(m, "sm")}${esc(m.username)}</div></td>
        <td class="admin-type-cell">${typeBadge(m)}</td>
        <td class="num" data-label="Suggested">${m.counts.suggested}${m.counts.suggested_watched ? ` <span style="color:var(--text-faint)">(${m.counts.suggested_watched} watched)</span>` : ""}</td>
        <td class="num" data-label="Ratings">${m.counts.ratings}</td>
        <td class="admin-actions-cell">${rowActions(m)}</td>
      </tr>`).join("")}
      </tbody></table>` : `<div class="empty" style="padding:2rem">${emptyMsg}</div>`;

    const body = `
      <div class="page-head"><h1>Admin</h1>
        <span class="count">${reals.length} accounts · ${placeholders.length} placeholders</span>
        <div class="controls"><button class="btn" id="refresh-lib">↻ Refresh Plex library</button></div>
      </div>

      ${placeholders.length ? `<div class="stat-card wide" style="margin-bottom:1.3rem">
        <h3>Placeholders to reconcile</h3>
        <div class="sub">Temporary member records. Once the real person signs in with Plex, merge their placeholder into that account — all their suggestions, ratings, and seen-states move over, and the placeholder is removed.</div>
        ${table(placeholders, "")}
      </div>` : ""}

      <div class="stat-card wide">
        <h3>Accounts</h3>
        <div class="sub">Real Plex logins. Grant admin to give someone access to this panel.</div>
        ${table(reals, "No one has signed in yet.")}
      </div>
      <form class="stat-card wide admin-settings" id="admin-settings">
        <h3>Application settings</h3>
        <div class="sub">Update integrations without editing files. Saved secrets are never displayed; leave a saved secret blank to keep it.</div>
        <div class="setup-grid">${settingsFields(configData.settings)}</div>
        <div class="setup-actions"><span id="settings-message"></span><button class="btn btn-primary" type="submit">Validate and save</button></div>
      </form>`;
    paintView("admin", body, preserve);

    $("#refresh-lib").onclick = async (e) => {
      e.target.disabled = true; e.target.textContent = "Refreshing…";
      try { await api("/api/admin/refresh_library", { method: "POST" }); toast("Plex library refreshed"); }
      catch (err) { toast("Refresh failed: " + err.message, true); }
      e.target.disabled = false; e.target.textContent = "↻ Refresh Plex library";
    };

    app.querySelectorAll(".merge-btn").forEach(btn => btn.onclick = () => {
      const fromId = parseInt(btn.dataset.from, 10);
      const sel = app.querySelector(`.merge-target[data-from="${fromId}"]`);
      const intoId = parseInt(sel.value, 10);
      const fromName = placeholders.find(p => p.id === fromId)?.username;
      const intoName = reals.find(r => r.id === intoId)?.username;
      if (!confirm(`Merge placeholder "${fromName}" into "${intoName}"?\n\nAll of ${fromName}'s suggestions and ratings will be reassigned to ${intoName}, and the "${fromName}" placeholder will be deleted. This can't be undone.`)) return;
      mergeMembers(fromId, intoId);
    });

    app.querySelectorAll(".admin-toggle").forEach(btn => btn.onclick = () =>
      setAdmin(parseInt(btn.dataset.id, 10), btn.dataset.val === "1"));
    const settingsForm = $("#admin-settings");
    settingsForm.onsubmit = async (event) => {
      event.preventDefault(); showSettingsErrors(settingsForm);
      const button = settingsForm.querySelector("button[type=submit]");
      button.disabled = true; button.textContent = "Validating…";
      try {
        await api("/api/admin/settings", { method: "PUT", body: collectSettings(settingsForm) });
        toast("Settings saved"); renderAdmin(true);
      } catch (e) {
        showSettingsErrors(settingsForm, (e.data && e.data.errors) || {});
        $("#settings-message").textContent = e.message;
        button.disabled = false; button.textContent = "Validate and save";
      }
    };
  }

  async function mergeMembers(fromId, intoId) {
    try {
      const r = await api("/api/admin/merge", { method: "POST", body: { from_id: fromId, into_id: intoId } });
      toast(`Merged ${r.merged_from} into ${r.into}`);
      renderAdmin(true);
    } catch (e) { toast("Merge failed: " + e.message, true); }
  }

  async function setAdmin(id, value) {
    try {
      await api(`/api/admin/members/${id}/admin`, { method: "POST", body: { is_admin: value } });
      toast(value ? "Admin granted" : "Admin removed");
      renderAdmin(true);
    } catch (e) { toast(e.message, true); }
  }

  // ---------- misc ----------
  function skeletonGrid() {
    return `<div class="page-head"><h1>&nbsp;</h1></div><div class="grid skeleton-grid">${Array(8).fill('<div class="sk"></div>').join("")}</div>`;
  }
  function errBox(e) { return `<div class="empty">Something went wrong: ${esc(e.message)}</div>`; }

  // ---------- router ----------
  async function render({ preserve = false } = {}) {
    if (!state.me) { renderLogin(); return; }
    await ensureMembers().catch(() => {});
    const hash = location.hash || "#/thisweek";
    const [, view, arg] = hash.split("/");
    if (view === "member" && state.currentHash && !state.currentHash.startsWith("#/member/")) {
      state.memberBackHash = state.currentHash;
    }
    state.currentHash = hash;
    state.route = view;
    if (view === "backlog") return renderBacklog(preserve);
    if (view === "watched") return renderWatched(preserve);
    if (view === "stats") return renderStats(preserve);
    if (view === "admin") return renderAdmin(preserve);
    if (view === "profile") return renderProfile(preserve);
    if (view === "member" && arg) return renderMemberProfile(arg, preserve);
    if (view === "movie" && arg) return renderDetail(arg, preserve);
    return renderThisWeek(preserve);
  }

  document.addEventListener("click", (e) => {
    const closest = (sel) => e.target.closest && e.target.closest(sel);
    const menu = $("#me-menu");
    const meBtn = closest("#me-btn");
    if (meBtn) {
      if (menu) {
        const open = !menu.hidden;
        menu.hidden = open;
        meBtn.setAttribute("aria-expanded", String(!open));
      }
      return;
    }
    // Any click outside the open menu closes it (menu items included: navigation
    // or logout re-renders the shell, which resets the menu to hidden anyway).
    if (menu && !menu.hidden && !closest("#me-menu")) {
      menu.hidden = true;
      const b = $("#me-btn");
      if (b) b.setAttribute("aria-expanded", "false");
    }
    if (e.target.id === "logout-btn") {
      disconnectEvents();
      api("/auth/logout", { method: "POST" }).finally(() => { state.me = null; location.hash = ""; render(); });
    }
  });

  window.addEventListener("hashchange", () => render({ preserve: true }));

  // ---------- boot ----------
  (async function boot() {
    try {
      const setup = await api("/api/setup/status");
      if (setup.required) { state.setupRequired = true; renderSetup(); return; }
    } catch { /* normal auth flow will show the actionable error */ }
    bootAuthenticated();
  })();

  async function bootAuthenticated() {
    try {
      state.me = await api("/api/me");
    } catch { state.me = null; }
    render();
    if (state.me) { refreshTodo(); connectEvents(); }
  }
})();
