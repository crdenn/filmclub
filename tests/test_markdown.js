/* Tests for the hand-rolled markdown subset in static/collections.js.
 *
 * That renderer deliberately bypasses the app's usual esc()-everything rule: it
 * builds HTML from author-written markdown. It is therefore the one place in
 * the frontend where a mistake turns stored text into markup, which is why it
 * has tests at all.
 *
 * No test framework and no dependencies — plain node, which AGENTS.md already
 * requires for `node --check`:
 *
 *     node tests/test_markdown.js
 */
const fs = require("fs");
const path = require("path").join(__dirname, "..", "static", "collections.js");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

global.window = {
  FilmClub: {
    esc, api: async () => ({}), paintView() {}, paintError() {},
    registerRoute() {},
    get me() { return { is_admin: true }; },
  },
};

// Expose the internals: append a global assignment before evaluating.
const src = fs.readFileSync(path, "utf8").replace(
  "FC.registerRoute(\"collections\", renderCollections);",
  "global.__md = markdown; global.__excerpt = excerpt;"
);
eval(src);
const md = global.__md, excerpt = global.__excerpt;

let fails = 0;
function check(name, got, want) {
  const ok = got === want;
  if (!ok) fails++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}`);
  if (!ok) console.log(`      got:  ${got}\n      want: ${want}`);
}

check("plain paragraph", md("Hello there."), "<p>Hello there.</p>");

check("two paragraphs", md("One.\n\nTwo."), "<p>One.</p><p>Two.</p>");

check("soft wrap joins", md("One\nline."), "<p>One line.</p>");

check("bold", md("a **b** c"), "<p>a <strong>b</strong> c</p>");

check("italic asterisk", md("a *b* c"), "<p>a <em>b</em> c</p>");

check("italic underscore", md("a _b_ c"), "<p>a <em>b</em> c</p>");

check("html is escaped",
  md('<script>alert("x")</script>'),
  '<p>&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</p>');

check("link",
  md("see [here](https://example.com/a_b)"),
  '<p>see <a href="https://example.com/a_b" target="_blank" rel="noopener noreferrer">here</a></p>');

check("javascript: link is defanged",
  md("[click](javascript:alert)"),
  "<p>click</p>");

// A URL containing parentheses stops the match early, so the label survives and
// a stray ")" is left behind. Cosmetic; what matters is that no anchor is built.
check("javascript: link with parens emits no anchor",
  md("[click](javascript:alert(1))").includes("<a "), false);

check("data: link is defanged",
  md("[x](data:text/html;base64,PHN2Zz4=)"), "<p>x</p>");

// The bug this placeholder scheme exists to avoid: a bare numeric marker would
// be clobbered by any year in the prose.
check("year near a link survives",
  md("In 1976 see [here](https://x.com) and 2011 too"),
  '<p>In 1976 see <a href="https://x.com" target="_blank" rel="noopener noreferrer">here</a> and 2011 too</p>');

check("two links in one paragraph",
  md("[a](https://a.com) and [b](https://b.com)"),
  '<p><a href="https://a.com" target="_blank" rel="noopener noreferrer">a</a> and '
  + '<a href="https://b.com" target="_blank" rel="noopener noreferrer">b</a></p>');

check("underscore inside a word is not italic",
  md("file_name_here stays"),
  "<p>file_name_here stays</p>");

check("ampersand in url is escaped",
  md("[q](https://x.com/?a=1&b=2)"),
  '<p><a href="https://x.com/?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">q</a></p>');

check("empty is empty", md(""), "");
check("null is empty", md(null), "");

check("excerpt cuts on a word boundary",
  excerpt("aaa bbb ccc ddd", 8), "aaa bbb…");
check("excerpt leaves short text alone",
  excerpt("short", 40), "short");

console.log(fails ? `\n${fails} FAILED` : "\nall passed");
process.exit(fails ? 1 : 0);
