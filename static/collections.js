/* Curated collections — an authored, essay-style page of films.
 *
 * Registers its own routes on the shell in app.js rather than living there:
 *   #/collections          the index
 *   #/collections/<slug>   one collection
 *
 * The layout is deliberately unlike the rest of the app. Everywhere else is
 * poster-forward and grid-shaped, because everywhere else is a library you
 * browse. This is prose you read top to bottom, so it is a single column of
 * wide rows — still on one side, writing on the other — and the film title is
 * set large in a serif, as the dominant element on the row.
 */
(() => {
  "use strict";

  const FC = window.FilmClub;
  const { esc, api, paintView, paintError } = FC;

  // ---------- markdown ----------
  // A deliberately small subset: paragraphs, bold, italic, links. That is all
  // the editor will be able to produce, and matching the two exactly keeps the
  // authored text honest — nothing can be stored that will not render.
  //
  // Input is escaped *before* any markup is inserted, so the only HTML that can
  // reach the page is what this function itself builds.
  const MD_LINK = /\[([^\]]+)\]\(([^)\s]+)\)/g;
  const MD_BOLD = /\*\*([^*]+)\*\*/g;
  const MD_ITALIC = /(^|[\s(])[*_]([^*_]+)[*_](?=[\s).,;:!?]|$)/g;
  // Anything else — javascript:, data:, vbscript: — is dropped to plain text.
  const SAFE_HREF = /^(?:https?:\/\/|mailto:|\/|#)/i;

  function inlineMarkdown(block) {
    // Soft-wrap newlines inside a paragraph are just whitespace.
    let s = esc(block.replace(/\s*\n\s*/g, " ").trim());

    // Links are extracted first and parked as placeholders, so emphasis rules
    // below cannot chew on underscores inside a URL. The placeholder is wrapped
    // in NULs specifically because escaped text can never contain one — a bare
    // numeric marker would collide with any year the author happened to write.
    const links = [];
    s = s.replace(MD_LINK, (_m, label, href) => {
      if (!SAFE_HREF.test(href)) return label;
      const i = links.push(
        `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
      ) - 1;
      return `\u0000${i}\u0000`;
    });

    s = s.replace(MD_BOLD, "<strong>$1</strong>");
    s = s.replace(MD_ITALIC, "$1<em>$2</em>");

    return s.replace(/\u0000(\d+)\u0000/g, (_m, i) => links[Number(i)]);
  }

  /* Trim the *source* to a whole word before rendering. Slicing the rendered
     HTML instead would happily cut through a tag or an entity. */
  function excerpt(src, limit = 320) {
    const text = String(src || "").trim();
    if (text.length <= limit) return text;
    const cut = text.slice(0, limit);
    const at = cut.lastIndexOf(" ");
    return (at > 0 ? cut.slice(0, at) : cut) + "…";
  }

  function markdown(src) {
    const text = String(src || "").trim();
    if (!text) return "";
    return text
      .split(/\n\s*\n/)
      .map((block) => block.trim())
      .filter(Boolean)
      .map((block) => `<p>${inlineMarkdown(block)}</p>`)
      .join("");
  }

  // ---------- small formatters ----------
  function runtime(mins) {
    const m = Number(mins);
    if (!m || m <= 0) return "";
    const h = Math.floor(m / 60);
    return h ? `${h}h ${m % 60}m` : `${m}m`;
  }

  function meta(entry) {
    return [entry.year, runtime(entry.runtime)].filter(Boolean).join(" · ");
  }

  // ---------- rows ----------
  /* A film the club is also tracking has a normal detail page, so the artwork
     and title link to it exactly as they do from the backlog. A film that has
     never been suggested has no such page — the Add button below is what
     brings one into existence. */
  function still(entry) {
    const inner = entry.still_url
      ? `<img src="${esc(entry.still_url)}" alt="" loading="lazy" decoding="async">`
      : "";
    const cls = `cl-still${entry.still_url ? "" : " cl-still-empty"}`;
    return entry.movie_id
      ? `<a class="${cls} cl-linked" href="#/movie/${entry.movie_id}"
           aria-label="${esc(entry.title)}">${inner}</a>`
      : `<div class="${cls}">${inner}</div>`;
  }

  function titleMarkup(entry) {
    const t = esc(entry.title);
    return entry.movie_id
      ? `<h2 class="cl-title"><a href="#/movie/${entry.movie_id}">${t}</a></h2>`
      : `<h2 class="cl-title">${t}</h2>`;
  }

  /* The backlog control. Any member can put a film on the list, so this is not
     an admin affordance and stays visible in preview. */
  const BACKLOG_LABEL = {
    suggested: "On the backlog",
    scheduled: "This week's pick",
    watched: "Watched",
  };

  function backlogControl(entry) {
    const status = entry.movie_status;
    if (status) {
      const label = BACKLOG_LABEL[status] || "On the list";
      return `<a class="cl-inlist cl-inlist-${esc(status)}"
         href="#/movie/${entry.movie_id}">${esc(label)}</a>`;
    }
    return `<button class="cl-addbacklog" data-tmdb="${entry.tmdb_id}"
       data-title="${esc(entry.title)}">+ Add to backlog</button>`;
  }

  function watchButton(entry) {
    if (!entry.plex_link) return "";
    return `<a class="cl-watch" href="${esc(entry.plex_link)}"
       target="_blank" rel="noopener">▶ Watch on Plex</a>`;
  }

  /* Preview mode: an admin looking at their own pages exactly as a reader
     would. The flag is checked by isAdmin(), so every admin affordance —
     editing, adding, deleting, publishing, coverage — disappears from a single
     decision rather than each one remembering to hide itself.

     The content gating is done by the server (?preview=1), not here: hiding
     things client-side would only approximate what a reader receives, and the
     point of a preview is to be certain. */
  const PREVIEW_KEY = "fc_cl_preview";
  const previewing = () => {
    try { return localStorage.getItem(PREVIEW_KEY) === "1"; } catch (e) { return false; }
  };
  function setPreview(on) {
    try { localStorage.setItem(PREVIEW_KEY, on ? "1" : "0"); } catch (e) { /* private mode */ }
  }

  const reallyAdmin = () => !!(FC.me && FC.me.is_admin);
  const isAdmin = () => reallyAdmin() && !previewing();

  /* Authoring controls belong only to collections the owner wrote. A generated
     one is read-only, so it carries no editor, no Add, and no Remove — there is
     no point dressing a page in furniture nobody intends to use. Set per render
     from the collection being shown; management actions (publish, delete) are
     governed by isAdmin() alone and stay available on both kinds. */
  let canAuthor = false;

  /* Shown only to a real admin, and deliberately fixed to the viewport: in
     preview there is nothing else on the page that could turn it off. */
  function previewBar() {
    if (!reallyAdmin() || !previewing()) return "";
    return `<div class="cl-preview-bar">
      <span>Preview — this is what everyone else sees</span>
      <button class="cl-preview-exit" type="button">Exit preview</button>
    </div>`;
  }

  const ICON_MORE = `<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>`;

  /* Owner actions for the index live in the hero corner rather than as buttons
     above the list. They are occasional — a collection is created rarely and
     previewed rarely — and standing controls in the reading path made the page
     look like a form. Same overflow pattern the movie detail page uses. */
  function indexMenu() {
    if (!isAdmin()) return "";
    return `<div class="cl-hero-menu overflow-anchor">
      <button type="button" class="btn icon-btn" id="cl-index-more"
        aria-haspopup="true" aria-expanded="false" aria-label="Collection actions">${ICON_MORE}</button>
      <div class="overflow-menu" id="cl-index-menu" hidden>
        <button type="button" class="overflow-menu-item" id="cl-index-new">New collection…</button>
        <button type="button" class="overflow-menu-item" id="cl-index-preview">Preview as reader</button>
      </div>
    </div>`;
  }

  function previewToggle() {
    if (!isAdmin()) return "";
    return `<button class="cl-preview-enter" type="button">Preview as reader</button>`;
  }

  function wirePreview() {
    const exit = document.querySelector(".cl-preview-exit");
    if (exit) exit.onclick = () => { setPreview(false); renderCurrent(); };
    const enter = document.querySelector(".cl-preview-enter");
    if (enter) enter.onclick = () => { setPreview(true); renderCurrent(); };
  }

  /* Marks a rendered-markdown block as editable in place. The raw source is
     parked in a data attribute so clicking can swap the rendered HTML for the
     text that produced it — the author edits what they actually wrote. */
  function editable(source, key, placeholder) {
    if (!isAdmin() || !canAuthor) return "";
    const raw = String(source || "");
    return `data-edit="${esc(key)}" data-md="${esc(raw)}"`
      + ` data-placeholder="${esc(placeholder)}"`
      + (raw.trim() ? "" : ` data-blank="1"`);
  }

  function row(entry) {
    const director = entry.director
      ? `<div class="cl-director">${esc(entry.director)}</div>` : "";
    const line = meta(entry);
    // An entry that no longer resolves on Plex is only ever rendered for an
    // admin, so labelling it here cannot leak anything to a reader.
    const missing = entry.plex_state === "missing"
      ? `<div class="cl-missing">Not on the server — hidden from readers</div>` : "";
    const remove = isAdmin() && canAuthor
      ? `<button class="cl-remove" data-entry="${entry.id}"
           data-title="${esc(entry.title)}" title="Remove from this collection">Remove</button>`
      : "";
    return `<article class="cl-row">
      ${still(entry)}
      <div class="cl-panel">
        ${director}
        ${titleMarkup(entry)}
        ${line ? `<div class="cl-meta">${esc(line)}</div>` : ""}
        ${missing}
        <div class="cl-blurb" ${editable(entry.blurb, `entry:${entry.id}`,
          "Why is this worth watching?")}>${markdown(entry.blurb)}</div>
        <div class="cl-row-actions">
          ${watchButton(entry)}${backlogControl(entry)}${remove}
        </div>
      </div>
    </article>`;
  }

  // ---------- pages ----------
  /* The index is a normal app page: same sans typography, same card idiom as
     the backlog and watched grids. The serif reading treatment belongs to a
     collection itself, so that opening one feels like stepping into something
     different rather than more of the same. */
  function indexHero(items) {
    const cover = (items.find((c) => c.cover_url) || {}).cover_url;
    const films = items.reduce((n, c) => n + (c.entry_count || 0), 0);
    const sub = items.length
      ? `${items.length} ${items.length === 1 ? "collection" : "collections"}`
        + `${films ? ` · ${films} ${films === 1 ? "film" : "films"}` : ""}`
      : "Nothing here yet";
    return `<div class="cl-hero">
      ${cover ? `<img class="cl-hero-img" src="${esc(cover)}" alt="">` : ""}
      <div class="cl-hero-scrim"></div>
      ${indexMenu()}
      <div class="cl-hero-inner">
        <div class="cl-hero-eyebrow">Film Club</div>
        <h1 class="cl-hero-title">Collections</h1>
        <div class="cl-hero-sub">${esc(sub)}</div>
      </div>
    </div>`;
  }

  function indexPage(items) {
    const cards = items.map((c) => {
      const draft = c.published ? "" : `<span class="cl-draft">Draft</span>`;
      const kind = c.kind === "director" && c.director_name
        ? `<div class="cl-card-kind">Director · ${esc(c.director_name)}</div>` : "";
      const count = c.entry_count
        ? `${c.entry_count} ${c.entry_count === 1 ? "film" : "films"}` : "";
      return `<a class="cl-card" href="#/collections/${encodeURIComponent(c.slug)}">
        <div class="cl-card-thumb">
          ${c.cover_url ? `<img src="${esc(c.cover_url)}" alt="" loading="lazy">` : ""}
        </div>
        <div class="cl-card-body">
          ${kind}
          <h2 class="cl-card-title">${esc(c.title)}${draft}</h2>
          <div class="cl-card-intro">${markdown(excerpt(c.intro, 190))}</div>
          ${count ? `<div class="cl-card-count">${esc(count)}</div>` : ""}
        </div>
      </a>`;
    }).join("");

    // No standing admin furniture on the index at all now — New collection
    // lives only in the hero menu, as a modal, so a rare action never occupies
    // permanent space above the list.
    return `${previewBar()}
      ${indexHero(items)}
      ${items.length
        ? `<div class="cl-cards">${cards}</div>`
        : `<div class="empty">No collections yet.</div>`}`;
  }

  /* The index overflow menu. Follows the same open/close behaviour as the
     movie detail page's menu: click toggles, a click anywhere else closes, and
     a click inside does not bubble out and immediately close it again. */
  function wireIndexMenu() {
    const btn = document.querySelector("#cl-index-more");
    const menu = document.querySelector("#cl-index-menu");
    if (!btn || !menu) return;
    const close = () => {
      menu.hidden = true;
      btn.setAttribute("aria-expanded", "false");
    };
    btn.onclick = (e) => {
      e.stopPropagation();
      const open = menu.hidden;
      menu.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
    };
    menu.onclick = (e) => e.stopPropagation();
    if (!wireIndexMenu._doc) {
      wireIndexMenu._doc = true;
      document.addEventListener("click", () => {
        const m = document.querySelector("#cl-index-menu");
        const b = document.querySelector("#cl-index-more");
        if (m && !m.hidden) { m.hidden = true; if (b) b.setAttribute("aria-expanded", "false"); }
      });
    }

    const preview = document.querySelector("#cl-index-preview");
    if (preview) preview.onclick = () => { close(); setPreview(true); renderCurrent(); };

    const newBtn = document.querySelector("#cl-index-new");
    if (newBtn) newBtn.onclick = () => { close(); showNewCollectionModal(); };
  }

  /* New-collection is a real modal in #modal-root — the same host and pattern
     app.js's own modals use (see showDiscordIdModal) — rather than an inline
     form living permanently under the hero. Creating a collection is rare
     enough that it should cost nothing when nobody is doing it, and a modal
     also plants focus in the title field without any extra wiring. */
  function showNewCollectionModal() {
    const root = document.getElementById("modal-root");
    if (!root) return;
    root.innerHTML = `<div class="modal-backdrop" id="cl-new-backdrop"><div class="modal">
      <div class="modal-head"><h2>New collection</h2>
        <button class="modal-close" id="cl-new-close" type="button">×</button></div>
      <form class="modal-body" id="cl-new-form">
        <label><span>Title</span>
          <input class="search-input" id="cl-new-title" type="text" required
            maxlength="200" placeholder="Collection title" autocomplete="off">
        </label>
        <label><span>Kind</span>
          <select class="search-input" id="cl-new-kind">
            <option value="picked">Hand-picked</option>
            <option value="director">Director</option>
          </select>
        </label>
        <label id="cl-new-director-row" hidden><span>Director</span>
          <input class="search-input" id="cl-new-director" type="text" maxlength="200"
            placeholder="Defaults to the title" autocomplete="off">
        </label>
        <div class="setup-actions">
          <span id="cl-new-message"></span>
          <button class="btn btn-primary" type="submit">Create</button>
        </div>
      </form>
    </div></div>`;

    const close = () => { root.innerHTML = ""; };
    document.getElementById("cl-new-close").onclick = close;
    document.getElementById("cl-new-backdrop").onclick = (e) => {
      if (e.target.id === "cl-new-backdrop") close();
    };

    const kind = document.getElementById("cl-new-kind");
    const directorRow = document.getElementById("cl-new-director-row");
    // A director collection needs its subject; a hand-picked one does not.
    kind.onchange = () => { directorRow.hidden = kind.value !== "director"; };

    document.getElementById("cl-new-title").focus();

    document.getElementById("cl-new-form").onsubmit = async (e) => {
      e.preventDefault();
      const name = document.getElementById("cl-new-title").value.trim();
      if (!name) return;
      const submit = e.target.querySelector('button[type="submit"]');
      const message = document.getElementById("cl-new-message");
      submit.disabled = true; submit.textContent = "Creating…";
      try {
        const director = document.getElementById("cl-new-director").value.trim();
        const c = await api("/api/collections", {
          method: "POST",
          body: {
            title: name,
            kind: kind.value,
            director_name: kind.value === "director" ? director : null,
          },
        });
        close();
        const added = c.sync && c.sync.added;
        FC.toast(added
          ? `Created "${c.title}" — ${added} film${added === 1 ? "" : "s"} from Plex, `
            + `all awaiting a blurb`
          : `Created "${c.title}" — it's a draft until you publish it`);
        location.hash = `#/collections/${encodeURIComponent(c.slug)}`;
      } catch (e2) {
        submit.disabled = false; submit.textContent = "Create";
        message.textContent = e2.message;
      }
    };
  }

  const YEAR = (iso) => (String(iso || "").match(/^(\d{4})/) || [])[1] || "";

  /* A director's factual scaffolding, from TMDB: portrait and dates. Having
     these on the page is what frees the author's prose from having to be a
     summary of facts. */
  function directorHeader(c) {
    if (c.kind !== "director") return "";
    const born = YEAR(c.director_born);
    const died = YEAR(c.director_died);
    const dates = born ? (died ? `${born}–${died}` : `b. ${born}`) : "";
    // The name is already the page title; repeating it beside the portrait was
    // just the same words twice. The portrait carries the facts instead.
    const n = c.entry_count || 0;
    const films = n ? `${n} film${n === 1 ? "" : "s"}` : "";
    const facts = [dates, films].filter(Boolean).join(" · ");
    return `<div class="cl-dir">
      ${c.director_portrait_url
        ? `<img class="cl-dir-portrait" src="${esc(c.director_portrait_url)}" alt="">`
        : `<div class="cl-dir-portrait cl-dir-portrait-empty"></div>`}
      ${facts ? `<div class="cl-dir-facts">${esc(facts)}</div>` : ""}
    </div>`;
  }

  /* A small sans label above an authored block, for the author only. Two empty
     serif placeholders stacked together are indistinguishable; a label says
     which is which without intruding on the reading view once written. */
  function proseLabel(text) {
    return isAdmin() && canAuthor
      ? `<div class="cl-prose-label">${esc(text)}</div>` : "";
  }

  /* The author's coverage view: the full filmography as a to-do list. Admin
     only — it is the reason a director page can grow gradually without ever
     looking half-finished to a reader. */
  function coveragePanel(c) {
    if (!isAdmin() || c.kind !== "director") return "";
    const cov = c.coverage;
    if (!cov) {
      return `<div class="cl-cov"><div class="cl-cov-head">Filmography</div>
        <div class="cl-add-hint">${c.director_tmdb_id
          ? "Couldn't reach TMDB for the filmography."
          : "No TMDB director linked, so there's no filmography to track."}</div></div>`;
    }
    const rows = cov.films.map((f) => {
      const label = f.state === "written" ? "Written"
        : f.state === "blank" ? "Added, not written" : "";
      // on_plex is null when the library cache is cold — say nothing rather
      // than imply the film is missing.
      const plexTag = f.on_plex === true ? `<span class="cl-cov-plex">On Plex</span>`
        : f.on_plex === false ? `<span class="cl-cov-noplex">Not on Plex</span>` : "";
      const action = f.state === "untouched"
        ? `<button class="cl-cov-add" data-tmdb="${f.tmdb_id}">Add</button>` : "";
      return `<li class="cl-cov-row cl-cov-${f.state}">
        <span class="cl-cov-year">${esc(f.year || "—")}</span>
        <span class="cl-cov-title">${esc(f.title)}</span>
        ${label ? `<span class="cl-cov-state">${label}</span>` : ""}
        ${plexTag}${action}
      </li>`;
    }).join("");
    const extra = cov.extra.length
      ? `<div class="cl-cov-sub">Also in this collection, not credited to them on TMDB:
           ${cov.extra.map((e) => esc(e.title)).join(", ")}</div>`
      : "";
    return `<div class="cl-cov">
      <div class="cl-cov-head">Filmography
        <span class="cl-cov-count">${cov.written} of ${cov.total} written${
          cov.blank ? ` · ${cov.blank} awaiting a blurb` : ""}</span>
      </div>
      <ul class="cl-cov-list">${rows}</ul>
      ${extra}
    </div>`;
  }

  /* A small factual line under the title. It earns its place twice: it tells a
     reader what they are committing to before they scroll, and it gives the
     title column something to sit on — without it the left half is a large
     heading above a void. */
  function headerMeta(c, entries) {
    const n = entries.length;
    const mins = entries.reduce((t, e) => t + (Number(e.runtime) || 0), 0);
    const years = entries.map((e) => Number(e.year)).filter(Boolean);
    const bits = [];
    if (n) bits.push(`${n} film${n === 1 ? "" : "s"}`);
    if (mins) {
      const h = Math.floor(mins / 60);
      bits.push(h ? `${h}h ${mins % 60}m` : `${mins}m`);
    }
    if (years.length > 1) {
      const lo = Math.min(...years), hi = Math.max(...years);
      if (lo !== hi) bits.push(`${lo}–${hi}`);
    }
    return bits.length ? `<div class="cl-head-meta">${esc(bits.join(" · "))}</div>` : "";
  }

  function collectionPage(c) {
    const entries = c.entries || [];
    // A draft is only ever served to an admin, so this badge is not a leak.
    const draft = c.published ? "" : `<span class="cl-draft">Draft</span>`;
    const eyebrow = c.kind === "director" && c.director_name
      ? `<div class="cl-eyebrow">Director</div>` : "";

    return `${previewBar()}
    <article class="cl-page">
      <header class="cl-head">
        <div class="cl-head-top">
          <div class="cl-head-title">
            ${eyebrow}
            <h1 class="cl-page-title">${esc(c.title)}</h1>
            ${draft}
            ${headerMeta(c, entries)}
          </div>
          <div class="cl-head-summary">
            ${c.kind === "director" ? proseLabel("On this collection") : ""}
            <div class="cl-intro" ${editable(c.intro, "intro",
              "Introduce this collection…")}>${markdown(c.intro)}</div>
          </div>
        </div>
        ${directorHeader(c)}
        ${c.kind === "director" ? `
          ${proseLabel("On the director")}
          <div class="cl-intro" ${editable(c.director_intro, "director_intro",
            "Write about the director…")}>${markdown(c.director_intro)}</div>` : ""}
      </header>
      ${entries.length
        ? `<div class="cl-rows">${entries.map(row).join("")}</div>`
        : `<div class="empty">Nothing to show here yet.</div>`}
      ${coveragePanel(c)}
      ${isAdmin() && canAuthor ? `
        <div class="cl-admin">
          <button class="cl-add-toggle" type="button">+ Add a film</button>
          ${c.kind === "director"
            ? `<button class="cl-sync" type="button" data-slug="${esc(c.slug)}"
                 title="Re-check this director's filmography against the Plex library"
                 >Sync from Plex</button>` : ""}
          <div class="cl-add" hidden>
            <input class="cl-add-input" type="search" autocomplete="off"
                   placeholder="Search TMDB by title…" aria-label="Search films to add">
            <div class="cl-add-results"></div>
          </div>
        </div>` : ""}
      <div class="cl-foot">
        <a class="cl-back" href="#/collections">← All collections</a>
        ${isAdmin()
          ? `<span class="cl-foot-admin">
               ${previewToggle()}
               <button class="cl-publish" data-slug="${esc(c.slug)}"
                 data-published="${c.published ? "1" : "0"}">${
                   c.published ? "Unpublish" : "Publish"}</button>
               <button class="cl-delete" data-slug="${esc(c.slug)}"
                 data-title="${esc(c.title)}">Delete collection</button>
             </span>` : ""}
      </div>
    </article>`;
  }

  /* Add-a-film. Reuses the app's existing TMDB search endpoint rather than
     introducing a second search path. Debounced, because it fires per keystroke
     and TMDB is a third party. */
  function wireAddFilm(slug) {
    const toggle = document.querySelector(".cl-add-toggle");
    const panel = document.querySelector(".cl-add");
    if (!toggle || !panel) return;
    const input = panel.querySelector(".cl-add-input");
    const results = panel.querySelector(".cl-add-results");
    let timer = null;
    let seq = 0;

    toggle.onclick = () => {
      panel.hidden = !panel.hidden;
      if (!panel.hidden) input.focus();
    };

    input.oninput = () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = ""; return; }
      timer = setTimeout(async () => {
        // Responses can arrive out of order; only the newest query may paint.
        const mine = ++seq;
        results.innerHTML = `<div class="cl-add-hint">Searching…</div>`;
        try {
          const data = await api(`/api/tmdb/search?q=${encodeURIComponent(q)}`);
          if (mine !== seq) return;
          const items = data.results || [];
          results.innerHTML = items.length
            ? items.map((r) => `
                <button class="cl-add-result" data-tmdb="${r.tmdb_id}">
                  ${r.poster_url
                    ? `<img src="${esc(r.poster_url)}" alt="" loading="lazy">`
                    : `<span class="cl-add-noposter"></span>`}
                  <span class="cl-add-meta">
                    <span class="cl-add-title">${esc(r.title)}</span>
                    <span class="cl-add-sub">${esc([r.year, r.director].filter(Boolean).join(" · "))}</span>
                  </span>
                </button>`).join("")
            : `<div class="cl-add-hint">No matches.</div>`;
          wireResults();
        } catch (e) {
          if (mine !== seq) return;
          results.innerHTML = `<div class="cl-add-hint">${esc(e.message)}</div>`;
        }
      }, 300);
    };

    function wireResults() {
      results.querySelectorAll(".cl-add-result").forEach((btn) => {
        btn.onclick = async () => {
          btn.disabled = true;
          try {
            const r = await api(`/api/collections/${encodeURIComponent(slug)}/entries`,
              { method: "POST", body: { tmdb_id: Number(btn.dataset.tmdb) } });
            FC.toast(`Added ${r.title || "film"}`);
            renderCollections({ arg: slug, preserve: true });
          } catch (e) {
            btn.disabled = false;
            if (e.message !== "unauth") FC.toast(e.message, true);
          }
        };
      });
    }
  }

  /* One-click add straight from the coverage list — the common case is
     "I've decided to write about this one", not a fresh search. */
  function wireCoverage(slug) {
    document.querySelectorAll(".cl-cov-add").forEach((btn) => {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "Adding…";
        try {
          const r = await api(`/api/collections/${encodeURIComponent(slug)}/entries`,
            { method: "POST", body: { tmdb_id: Number(btn.dataset.tmdb) } });
          FC.toast(`Added ${r.title || "film"}`);
          renderCollections({ arg: slug, preserve: true });
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Add";
          if (e.message !== "unauth") FC.toast(e.message, true);
        }
      };
    });
  }

  /* Adding to the backlog reuses the app's own /api/movies endpoint, so a film
     added from here goes through exactly the same path as one added from the
     search box — including the Seerr request when it isn't on the server.

     The result is swapped in place rather than re-rendering the page: on a long
     collection a full repaint would throw away the reader's scroll position for
     what is a one-row change. */
  function wireAddToBacklog() {
    document.querySelectorAll(".cl-addbacklog").forEach((btn) => {
      btn.onclick = async () => {
        const title = btn.dataset.title;
        btn.disabled = true;
        btn.textContent = "Adding…";
        try {
          const r = await api("/api/movies", {
            method: "POST", body: { tmdb_id: Number(btn.dataset.tmdb) },
          });
          const link = document.createElement("a");
          link.className = "cl-inlist cl-inlist-suggested";
          link.href = `#/movie/${r.id}`;
          link.textContent = "On the backlog";
          btn.replaceWith(link);
          FC.toast(`Added ${title} to the backlog`);
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "+ Add to backlog";
          if (e.message !== "unauth") FC.toast(e.message, true);
        }
      };
    });
  }

  function wireSync(slug) {
    const btn = document.querySelector(".cl-sync");
    if (!btn) return;
    btn.onclick = async () => {
      btn.disabled = true;
      const label = btn.textContent;
      btn.textContent = "Syncing…";
      try {
        const r = await api(`/api/collections/${encodeURIComponent(slug)}/sync`,
          { method: "POST" });
        FC.toast(r.added
          ? `Added ${r.added} film${r.added === 1 ? "" : "s"} from Plex`
          : `Nothing new — ${r.on_plex || 0} of ${r.filmography || 0} on Plex already here`);
        renderCollections({ arg: slug, preserve: true });
      } catch (e) {
        btn.disabled = false;
        btn.textContent = label;
        if (e.message !== "unauth") FC.toast(e.message, true);
      }
    };
  }

  function wirePublish(slug) {
    const btn = document.querySelector(".cl-publish");
    if (!btn) return;
    btn.onclick = async () => {
      const publish = btn.dataset.published !== "1";
      btn.disabled = true;
      try {
        await api(`/api/collections/${encodeURIComponent(slug)}`,
          { method: "PATCH", body: { published: publish } });
        FC.toast(publish ? "Published — readers can see this now" : "Unpublished");
        renderCollections({ arg: slug, preserve: true });
      } catch (e) {
        btn.disabled = false;
        if (e.message !== "unauth") FC.toast(e.message, true);
      }
    };
  }

  /* Click-to-edit. The page shows rendered markdown; clicking a block swaps it
     for the source that produced it and hands that to the editor component.
     On blur the text is saved and re-rendered, so the author spends almost all
     their time looking at the real typography rather than at markup. */
  function wireEditing(slug) {
    if (!isAdmin()) return;

    document.querySelectorAll("[data-edit]").forEach((el) => {
      const key = el.dataset.edit;
      const placeholder = el.dataset.placeholder || "";
      if (el.dataset.blank) el.classList.add("cl-blank");
      el.classList.add("cl-editable");
      el.title = "Click to edit";

      el.addEventListener("click", function start() {
        if (el.dataset.editing) return;
        el.dataset.editing = "1";
        el.classList.remove("cl-blank");

        const handle = window.InlineEditor.attach(el, {
          value: el.dataset.md || "",
          placeholder,
          onStatus: (state) => {
            if (state === "error") FC.toast("Couldn't save — your text is still here", true);
          },
          onSave: async (text) => {
            // "entry:<id>" patches one film's blurb; anything else names a
            // field on the collection itself (intro, director_intro).
            const isEntry = key.startsWith("entry:");
            const path = isEntry
              ? `/api/collections/${encodeURIComponent(slug)}/entries/${key.slice(6)}`
              : `/api/collections/${encodeURIComponent(slug)}`;
            await api(path, {
              method: "PATCH",
              body: isEntry ? { blurb: text } : { [key]: text },
            });
            el.dataset.md = text;
          },
        });

        // Leaving the block ends the edit: save, tear the editor down, and put
        // the rendered version back.
        el.addEventListener("blur", async function done() {
          el.removeEventListener("blur", done);
          await handle.destroy();
          delete el.dataset.editing;
          el.innerHTML = markdown(el.dataset.md);
          el.classList.toggle("cl-blank", !String(el.dataset.md || "").trim());
        }, { once: true });

        el.focus();
      });
    });
  }

  /* Admin-only destructive actions. Wired after each paint rather than
     delegated globally, so nothing of this module is live on other routes. */
  function wireAdminActions(slug) {
    document.querySelectorAll(".cl-remove").forEach((btn) => {
      btn.onclick = async () => {
        const title = btn.dataset.title;
        if (!confirm(`Remove "${title}" from this collection?\n\n`
          + `The blurb written for it is deleted too. The film itself is untouched.`)) return;
        btn.disabled = true;
        try {
          await api(`/api/collections/${encodeURIComponent(slug)}/entries/${btn.dataset.entry}`,
            { method: "DELETE" });
          FC.toast(`Removed ${title}`);
          renderCollections({ arg: slug, preserve: true });
        } catch (e) {
          btn.disabled = false;
          if (e.message !== "unauth") FC.toast(e.message, true);
        }
      };
    });

    const del = document.querySelector(".cl-delete");
    if (del) {
      del.onclick = async () => {
        const title = del.dataset.title;
        if (!confirm(`Delete the whole "${title}" collection?\n\n`
          + `Every film in it and everything written about them is deleted. `
          + `This cannot be undone.`)) return;
        del.disabled = true;
        try {
          await api(`/api/collections/${encodeURIComponent(del.dataset.slug)}`,
            { method: "DELETE" });
          FC.toast(`Deleted ${title}`);
          location.hash = "#/collections";
        } catch (e) {
          del.disabled = false;
          if (e.message !== "unauth") FC.toast(e.message, true);
        }
      };
    }
  }

  // ---------- routing ----------
  // Re-render whatever route is currently showing (used by the preview toggle,
  // which changes what the server should send rather than just the styling).
  let currentArg = null;
  function renderCurrent() {
    renderCollections({ arg: currentArg, preserve: true });
  }

  async function renderCollections({ arg, preserve = false }) {
    currentArg = arg || null;
    const q = previewing() ? "?preview=1" : "";
    if (!preserve) paintView("collections", `<div class="cl-loading"></div>`);
    try {
      if (arg) {
        const c = await api(`/api/collections/${encodeURIComponent(arg)}${q}`);
        canAuthor = c.editable !== false;
        paintView("collections", collectionPage(c), preserve);
        wireAdminActions(c.slug);
        wireEditing(c.slug);
        wireAddFilm(c.slug);
        wirePublish(c.slug);
        wireCoverage(c.slug);
        wireSync(c.slug);
        wireAddToBacklog();
        wirePreview();
      } else {
        const data = await api(`/api/collections${q}`);
        paintView("collections", indexPage(data.items || []), preserve);
        wireIndexMenu();
        wirePreview();
      }
    } catch (e) {
      if (e.message !== "unauth") paintError("collections", e, preserve);
    }
  }

  FC.registerRoute("collections", renderCollections);
})();
