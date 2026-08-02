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
  function still(entry) {
    if (!entry.still_url) return `<div class="cl-still cl-still-empty"></div>`;
    return `<div class="cl-still">
      <img src="${esc(entry.still_url)}" alt="" loading="lazy" decoding="async">
    </div>`;
  }

  function watchButton(entry) {
    if (!entry.plex_link) return "";
    return `<a class="cl-watch" href="${esc(entry.plex_link)}"
       target="_blank" rel="noopener">▶ Watch on Plex</a>`;
  }

  const isAdmin = () => !!(FC.me && FC.me.is_admin);

  /* Marks a rendered-markdown block as editable in place. The raw source is
     parked in a data attribute so clicking can swap the rendered HTML for the
     text that produced it — the author edits what they actually wrote. */
  function editable(source, key, placeholder) {
    if (!isAdmin()) return "";
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
    const remove = isAdmin()
      ? `<button class="cl-remove" data-entry="${entry.id}"
           data-title="${esc(entry.title)}" title="Remove from this collection">Remove</button>`
      : "";
    return `<article class="cl-row">
      ${still(entry)}
      <div class="cl-panel">
        ${director}
        <h2 class="cl-title">${esc(entry.title)}</h2>
        ${line ? `<div class="cl-meta">${esc(line)}</div>` : ""}
        ${missing}
        <div class="cl-blurb" ${editable(entry.blurb, `entry:${entry.id}`,
          "Why is this worth watching?")}>${markdown(entry.blurb)}</div>
        <div class="cl-row-actions">${watchButton(entry)}${remove}</div>
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
    const admin = isAdmin() ? `
      <div class="cl-admin cl-admin-index">
        <button class="cl-new-toggle" type="button">+ New collection</button>
        <form class="cl-new" hidden>
          <input class="cl-new-title" type="text" required maxlength="200"
                 placeholder="Collection title" aria-label="Collection title">
          <select class="cl-new-kind" aria-label="Collection kind">
            <option value="picked">Hand-picked</option>
            <option value="director">Director</option>
          </select>
          <input class="cl-new-director" type="text" maxlength="200" hidden
                 placeholder="Director name" aria-label="Director name">
          <button class="btn btn-primary" type="submit">Create</button>
        </form>
      </div>` : "";

    return `${indexHero(items)}
      ${admin}
      ${items.length
        ? `<div class="cl-cards">${cards}</div>`
        : `<div class="empty">No collections yet.</div>`}`;
  }

  function wireNewCollection() {
    const toggle = document.querySelector(".cl-new-toggle");
    const form = document.querySelector(".cl-new");
    if (!toggle || !form) return;
    const title = form.querySelector(".cl-new-title");
    const kind = form.querySelector(".cl-new-kind");
    const director = form.querySelector(".cl-new-director");

    toggle.onclick = () => {
      form.hidden = !form.hidden;
      if (!form.hidden) title.focus();
    };
    // A director collection needs its subject; a hand-picked one does not.
    kind.onchange = () => { director.hidden = kind.value !== "director"; };

    form.onsubmit = async (e) => {
      e.preventDefault();
      const name = title.value.trim();
      if (!name) return;
      const submit = form.querySelector('button[type="submit"]');
      submit.disabled = true;
      try {
        const c = await api("/api/collections", {
          method: "POST",
          body: {
            title: name,
            kind: kind.value,
            director_name: kind.value === "director" ? director.value.trim() : null,
          },
        });
        FC.toast(`Created "${c.title}" — it's a draft until you publish it`);
        location.hash = `#/collections/${encodeURIComponent(c.slug)}`;
      } catch (e2) {
        submit.disabled = false;
        if (e2.message !== "unauth") FC.toast(e2.message, true);
      }
    };
  }

  function collectionPage(c) {
    const entries = c.entries || [];
    // A draft is only ever served to an admin, so this badge is not a leak.
    const draft = c.published ? "" : `<span class="cl-draft">Draft</span>`;
    const director = c.kind === "director" && c.director_name
      ? `<div class="cl-eyebrow">${esc(c.director_name)}</div>` : "";

    return `<article class="cl-page">
      <header class="cl-head">
        ${director}
        <h1 class="cl-page-title">${esc(c.title)}</h1>
        ${draft}
        <div class="cl-intro" ${editable(c.intro, "intro",
          "Introduce this collection…")}>${markdown(c.intro)}</div>
      </header>
      ${entries.length
        ? `<div class="cl-rows">${entries.map(row).join("")}</div>`
        : `<div class="empty">Nothing to show here yet.</div>`}
      ${isAdmin() ? `
        <div class="cl-admin">
          <button class="cl-add-toggle" type="button">+ Add a film</button>
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
            const path = key === "intro"
              ? `/api/collections/${encodeURIComponent(slug)}`
              : `/api/collections/${encodeURIComponent(slug)}/entries/${key.split(":")[1]}`;
            const body = key === "intro" ? { intro: text } : { blurb: text };
            await api(path, { method: "PATCH", body });
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
  async function renderCollections({ arg, preserve = false }) {
    if (!preserve) paintView("collections", `<div class="cl-loading"></div>`);
    try {
      if (arg) {
        const c = await api(`/api/collections/${encodeURIComponent(arg)}`);
        paintView("collections", collectionPage(c), preserve);
        wireAdminActions(c.slug);
        wireEditing(c.slug);
        wireAddFilm(c.slug);
        wirePublish(c.slug);
      } else {
        const data = await api("/api/collections");
        paintView("collections", indexPage(data.items || []), preserve);
        wireNewCollection();
      }
    } catch (e) {
      if (e.message !== "unauth") paintError("collections", e, preserve);
    }
  }

  FC.registerRoute("collections", renderCollections);
})();
