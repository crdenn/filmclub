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
        <div class="cl-blurb">${markdown(entry.blurb)}</div>
        <div class="cl-row-actions">${watchButton(entry)}${remove}</div>
      </div>
    </article>`;
  }

  // ---------- pages ----------
  function indexPage(items) {
    if (!items.length) {
      return `<div class="cl-index">
        <h1 class="cl-index-title">Collections</h1>
        <div class="empty">No collections yet.</div>
      </div>`;
    }
    const cards = items.map((c) => {
      const draft = c.published ? "" : `<span class="cl-draft">Draft</span>`;
      const kind = c.kind === "director" && c.director_name
        ? `<div class="cl-card-kind">Director · ${esc(c.director_name)}</div>` : "";
      return `<a class="cl-card" href="#/collections/${encodeURIComponent(c.slug)}">
        ${kind}
        <h2 class="cl-card-title">${esc(c.title)}</h2>
        <div class="cl-card-intro">${markdown(excerpt(c.intro))}</div>
        ${draft}
      </a>`;
    }).join("");
    return `<div class="cl-index">
      <h1 class="cl-index-title">Collections</h1>
      <div class="cl-cards">${cards}</div>
    </div>`;
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
        <div class="cl-intro">${markdown(c.intro)}</div>
      </header>
      ${entries.length
        ? `<div class="cl-rows">${entries.map(row).join("")}</div>`
        : `<div class="empty">Nothing to show here yet.</div>`}
      <div class="cl-foot">
        <a class="cl-back" href="#/collections">← All collections</a>
        ${isAdmin()
          ? `<button class="cl-delete" data-slug="${esc(c.slug)}"
               data-title="${esc(c.title)}">Delete collection</button>` : ""}
      </div>
    </article>`;
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
      } else {
        const data = await api("/api/collections");
        paintView("collections", indexPage(data.items || []), preserve);
      }
    } catch (e) {
      if (e.message !== "unauth") paintError("collections", e, preserve);
    }
  }

  FC.registerRoute("collections", renderCollections);
})();
