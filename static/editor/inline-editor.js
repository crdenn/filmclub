/* inline-editor — edit markdown in place, autosaving on pause.
 *
 * Self-contained and framework-agnostic on purpose: it takes a string in,
 * hands a string back, and calls onSave. It knows nothing about databases,
 * HTTP, authentication, or what the text it is editing means. Dropping this
 * folder into another project should require no edits to this file.
 *
 * It also carries no styling. The host application owns the look, via the
 * class names below, so the editor inherits whatever typography it is sitting
 * in — which is the point: you write inside the real design, not in a box that
 * approximates it.
 *
 *   const handle = InlineEditor.attach(el, {
 *     value: "**markdown** source",
 *     onSave: async (markdown) => { ... },   // may throw; errors are surfaced
 *     onStatus: (state) => {},               // 'dirty' | 'saving' | 'saved' | 'error'
 *     placeholder: "Write something…",
 *     delay: 900,                            // ms of quiet before autosaving
 *   });
 *   handle.flush();     // save now if dirty (returns a promise)
 *   handle.destroy();   // detach, flushing anything unsaved
 *
 * Editing is deliberately of the *markdown source*, not a rich-text rendering
 * of it. A WYSIWYG surface has to convert HTML back into markdown on every
 * keystroke, which is where these components normally accumulate their bugs.
 * Editing the source keeps the stored text exactly what the author typed.
 */
(() => {
  "use strict";

  const CLASS = {
    root: "ie-editable",
    active: "ie-active",
    empty: "ie-empty",
    saving: "ie-saving",
    saved: "ie-saved",
    error: "ie-error",
  };

  // Some browsers support plaintext-only, which keeps contenteditable from
  // inventing markup on Enter or paste. Where they do not, we strip formatting
  // on paste and rely on innerText for the value.
  const PLAINTEXT_OK = (() => {
    try {
      const probe = document.createElement("div");
      probe.setAttribute("contenteditable", "plaintext-only");
      return probe.contentEditable === "plaintext-only";
    } catch (e) {
      return false;
    }
  })();

  function attach(el, opts = {}) {
    const {
      value = "",
      onSave = async () => {},
      onStatus = () => {},
      placeholder = "",
      delay = 900,
    } = opts;

    let saved = value;
    let timer = null;
    let destroyed = false;
    let inFlight = null;

    el.classList.add(CLASS.root);
    el.setAttribute("contenteditable", PLAINTEXT_OK ? "plaintext-only" : "true");
    el.setAttribute("role", "textbox");
    el.setAttribute("aria-multiline", "true");
    if (placeholder) el.setAttribute("data-placeholder", placeholder);
    el.innerText = value;
    reflectEmpty();

    function current() {
      // innerText, not textContent: it preserves the line breaks the author
      // typed, which are what separate markdown paragraphs.
      return el.innerText.replace(/ /g, " ").replace(/\s+$/, "");
    }

    function reflectEmpty() {
      el.classList.toggle(CLASS.empty, current() === "");
    }

    function status(state) {
      el.classList.remove(CLASS.saving, CLASS.saved, CLASS.error);
      if (state === "saving") el.classList.add(CLASS.saving);
      if (state === "saved") el.classList.add(CLASS.saved);
      if (state === "error") el.classList.add(CLASS.error);
      onStatus(state);
    }

    async function save() {
      const text = current();
      if (text === saved) return;
      // Serialise saves: a slow request must not be overtaken by a later one
      // and leave the older text as the stored value.
      if (inFlight) {
        await inFlight.catch(() => {});
        if (destroyed) return;
      }
      status("saving");
      inFlight = (async () => {
        try {
          await onSave(text);
          saved = text;
          status("saved");
        } catch (e) {
          status("error");
          throw e;
        } finally {
          inFlight = null;
        }
      })();
      return inFlight.catch(() => {});
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(() => { if (!destroyed) save(); }, delay);
    }

    function onInput() {
      reflectEmpty();
      status(current() === saved ? "saved" : "dirty");
      schedule();
    }

    function onBlur() {
      clearTimeout(timer);
      el.classList.remove(CLASS.active);
      save();
    }

    function onFocus() {
      el.classList.add(CLASS.active);
    }

    function onPaste(e) {
      if (PLAINTEXT_OK) return;   // the browser already keeps it plain
      e.preventDefault();
      const text = (e.clipboardData || window.clipboardData).getData("text/plain");
      document.execCommand("insertText", false, text);
    }

    function onKeydown(e) {
      // Escape abandons focus (and therefore saves, via blur).
      if (e.key === "Escape") { el.blur(); return; }

      // Markdown shortcuts for the two marks that have keyboard conventions.
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      const key = e.key.toLowerCase();
      const wrap = key === "b" ? "**" : key === "i" ? "*" : null;
      if (!wrap) return;
      e.preventDefault();
      surround(wrap);
    }

    function surround(marks) {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);
      if (!el.contains(range.commonAncestorContainer)) return;
      const selected = range.toString();
      document.execCommand("insertText", false, marks + selected + marks);
      if (!selected) {
        // Nothing was selected: drop the caret between the new marks so the
        // author can just keep typing.
        const back = document.getSelection();
        if (back && back.rangeCount) {
          const r = back.getRangeAt(0);
          r.setStart(r.startContainer, Math.max(0, r.startOffset - marks.length));
          r.collapse(true);
          back.removeAllRanges();
          back.addRange(r);
        }
      }
      onInput();
    }

    el.addEventListener("input", onInput);
    el.addEventListener("blur", onBlur);
    el.addEventListener("focus", onFocus);
    el.addEventListener("paste", onPaste);
    el.addEventListener("keydown", onKeydown);

    return {
      flush() { clearTimeout(timer); return save(); },
      value() { return current(); },
      isDirty() { return current() !== saved; },
      destroy() {
        clearTimeout(timer);
        const pending = save();
        destroyed = true;
        el.removeEventListener("input", onInput);
        el.removeEventListener("blur", onBlur);
        el.removeEventListener("focus", onFocus);
        el.removeEventListener("paste", onPaste);
        el.removeEventListener("keydown", onKeydown);
        el.removeAttribute("contenteditable");
        el.removeAttribute("role");
        el.removeAttribute("aria-multiline");
        el.classList.remove(CLASS.root, CLASS.active, CLASS.empty,
                            CLASS.saving, CLASS.saved, CLASS.error);
        return pending;
      },
    };
  }

  window.InlineEditor = { attach };
})();
