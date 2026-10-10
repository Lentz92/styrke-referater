"""The page (website/template.html), run in Node against stubs of the browser."""

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest
from conftest import decision, rule

from styrke import website

NODE = shutil.which("node")
TEMPLATE = Path(__file__).resolve().parent.parent / "website" / "template.html"
SEARCH_JS = TEMPLATE.parent / "search.js"

# Takes the template's own functions out of the page and runs them with a stubbed location, history and
# GoatCounter; prints what happened for each hash.
PROBE = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const grab = (name) => html.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`))[0];
const D = { aliases: { "gammel-adresse": "licensgebyr", "gammel-dødløft": "dødløft-for-masters" } };
const R = [{ slug: "startgebyr" }, { slug: "licensgebyr" }, { slug: "dødløft-for-masters" }, { slug: "tostring" },
           { slug: "toString" }];
const location = { hash: "", pathname: "/", search: "" };
let events = [];
const history = { replaceState: (_s, _t, url) => { events.push(["replace", url]); location.hash = url; } };
const window = { goatcounter: { count: (e) => events.push(["count", e.path]) } };
const href = (ri) => `#regel/${encodeURIComponent(R[ri].slug)}`;
const $ = () => null;
let opened = null;
const syncWithHash = eval(`(() => {
  ${["hashSlug", "countRuleOpen", "syncWithHash"].map(grab).join("\n")}
  function showRule(ri) { countRuleOpen(ri); opened = ri; }
  function hideRule() { opened = -1; }
  return syncWithHash;
})()`);
const out = [];
for (const hash of JSON.parse(process.argv[3])) {
  events = []; opened = null; location.hash = hash;
  syncWithHash();
  out.push({ opened, hash: location.hash, events });
}
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_old_links_lead_to_the_current_rule_and_odd_hashes_open_nothing(tmp_path):
    script = tmp_path / "probe.js"
    script.write_text(PROBE)
    hashes = ["#regel/gammel-adresse", "#regel/startgebyr", "#regel/gammel-d%C3%B8dl%C3%B8ft", "#regel/toString",
              "#regel/constructor", "#regel/%E0%A4%A", ""]
    result = subprocess.run([NODE, str(script), str(TEMPLATE), json.dumps(hashes)], capture_output=True, text=True,
                            check=True, timeout=30)
    alias, direct, encoded, inherited_name, prototype, malformed, empty = json.loads(result.stdout)

    # A former slug opens its rule, the address becomes the current one, and the visit counts under it.
    assert alias == {"opened": 1, "hash": "#regel/licensgebyr",
                     "events": [["replace", "#regel/licensgebyr"], ["count", "regel/licensgebyr"]]}
    assert direct == {"opened": 0, "hash": "#regel/startgebyr", "events": [["count", "regel/startgebyr"]]}
    assert encoded["opened"] == 2 and encoded["events"][-1] == ["count", "regel/dødløft-for-masters"]
    # A slug named like an inherited object property is a rule like any other, not an alias.
    assert inherited_name == {"opened": 4, "hash": "#regel/toString", "events": [["count", "regel/toString"]]}
    # Neither an object property, a cut-off %-escape nor an empty hash opens anything or throws.
    for case in (prototype, malformed, empty):
        assert case["opened"] == -1 and case["events"] == []


# Loads the popup's code and init() (from the open-rule comment to the end of init) into a model of the browser: a
# page that cannot scroll while its body is position:fixed (as on iOS), and session history whose entries keep a
# state, save their scroll and restore it on Back, Forward and reload unless their scroll restoration is "manual".
# Prints, after each step, whether the page is pinned, which offset of the page the reader sees, the address and the
# current entry's scroll restoration.
POPUP_PROBE = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const start = html.indexOf("/* The open rule lives in the URL"), end = html.indexOf("\ninit();");
if (start < 0 || end < 0) throw new Error("popup code not found");
const code = html.slice(start, end);

const D = { aliases: {} };
const R = [{ slug: "startgebyr" }, { slug: "licensgebyr" }];
const TODAY = "2026-10-09";
const href = (ri) => `#regel/${R[ri].slug}`;
const ruleDetail = () => "", draw = () => {}, longD = () => "";
let els = {}, listeners = {}; // the current document's elements and window listeners
const $ = (id) => (els[id] ??= { innerHTML: "", scrollTop: 0, focus() {}, addEventListener() {} });

let y = 0; // the window's scroll offset on a 5000px page in an 800px window
const body = { classList: { add() {}, remove() {} }, style: { top: "", width: "", _position: "",
  get position() { return this._position; },
  set position(v) { this._position = v; if (v === "fixed") y = 0; } } };
const document = { body, activeElement: $("link") };
const pinned = () => body.style.position === "fixed";
const window = { get scrollY() { return y; }, scrollTo(_x, to) { y = pinned() ? 0 : Math.max(0, Math.min(to, 4200)); },
  addEventListener(type, fn) { listeners[type] = fn; } };
const seen = () => (pinned() ? -parseFloat(body.style.top) : y);

const location = { hash: "", pathname: "/", search: "" };
const entries = [{ hash: "", state: null, scroll: 0, restore: "auto" }];
let at = 0;
const history = {
  get state() { return entries[at].state; },
  get scrollRestoration() { return entries[at].restore; },
  set scrollRestoration(v) { entries[at].restore = v; },
  replaceState(s, _t, url) {
    entries[at].state = structuredClone(s);
    if (url !== undefined) entries[at].hash = location.hash = url.startsWith("#") ? url : "";
  },
  back() { go(at - 1); },
  forward() { go(at + 1); },
};
function go(to) {
  entries[at].scroll = y;
  at = to; location.hash = entries[at].hash;
  if (entries[at].restore === "auto") window.scrollTo(0, entries[at].scroll);
  listeners.hashchange();
}
function follow(hash) { // a link to a rule: a new entry that inherits the current one's restoration mode
  entries[at].scroll = y;
  entries.splice(at + 1, Infinity, { hash, state: null, scroll: 0, restore: entries[at].restore });
  at += 1; location.hash = hash;
  listeners.hashchange();
}
function load() { // a fresh document for the current entry; the browser restores the scroll after the load
  els = {}; listeners = {}; body.style.position = body.style.top = body.style.width = ""; y = 0;
  location.hash = entries[at].hash;
  eval(`(() => { ${code}\n init(); })()`);
  if (entries[at].restore === "auto") window.scrollTo(0, entries[at].scroll);
}
const reload = () => { entries[at].scroll = y; load(); };
const closeButton = () => els.modal.onclick({ target: { closest: (sel) => (sel === "#close" ? {} : null) } });

const out = [];
const step = (name, action) => { action(); out.push([name, pinned(), seen(), location.hash, history.scrollRestoration]); };
entries[0].hash = "#regel/startgebyr";
step("opened by a shared link", load);
step("closed with the close button", closeButton);
step("opened from a link further down", () => { window.scrollTo(0, 1500); follow("#regel/startgebyr"); });
step("another rule while open", () => follow("#regel/licensgebyr"));
step("Back to the first rule", () => history.back());
step("Back to the page", () => history.back());
step("Forward to the rule", () => history.forward());
step("closed with the scrim", () => els.scrim.onclick());
step("Forward to the rule again", () => history.forward());
step("reloaded", reload);
step("closed with Esc", () => listeners.keydown({ key: "Escape" }));
step("Esc again with nothing open", () => listeners.keydown({ key: "Escape" }));
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_an_open_rule_holds_the_page_still_and_closing_returns_to_the_same_spot(tmp_path):
    script = tmp_path / "popup.js"
    script.write_text(POPUP_PROBE)
    result = subprocess.run([NODE, str(script), str(TEMPLATE)], capture_output=True, text=True, check=True, timeout=30)

    # [step, page pinned, offset the reader sees, address, scroll restoration]: opening pins the page where the reader
    # was; switching rules, Back/Forward between open rules and a reload keep that spot; every close unpins, scrolls
    # back to it and leaves the browser's scroll restoration as it was.
    assert json.loads(result.stdout) == [
        ["opened by a shared link", True, 0, "#regel/startgebyr", "manual"],
        ["closed with the close button", False, 0, "", "auto"],
        ["opened from a link further down", True, 1500, "#regel/startgebyr", "manual"],
        ["another rule while open", True, 1500, "#regel/licensgebyr", "manual"],
        ["Back to the first rule", True, 1500, "#regel/startgebyr", "manual"],
        ["Back to the page", False, 1500, "", "auto"],
        ["Forward to the rule", True, 1500, "#regel/startgebyr", "manual"],
        ["closed with the scrim", False, 1500, "", "auto"],
        ["Forward to the rule again", True, 1500, "#regel/startgebyr", "manual"],
        ["reloaded", True, 1500, "#regel/startgebyr", "manual"],
        ["closed with Esc", False, 1500, "", "auto"],
        ["Esc again with nothing open", False, 1500, "", "auto"],
    ]


# Runs the page's whole script, with the data website.py embeds, against a document whose elements only keep their
# HTML and handlers. Prints, as loaded, the dates of the meetings shown and the text of the areas and of "På vej"; the
# text of the content and of "På vej" with the area argv[3] chosen; the hits of a search for argv[4]; and the status
# line of each rule popup argv[5..].
PAGE_PROBE = r"""
const fs = require("fs"), vm = require("vm");
const [page, searchJs, area, query, ...slugs] = process.argv.slice(2);
const code = fs.readFileSync(page, "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {}, listeners = {};
const el = (id) => (els[id] ??= { innerHTML: "", value: "", focus() {}, getBoundingClientRect: () => ({ top: 0 }),
  addEventListener(type, fn) { (this.on ??= {})[type] = fn; } });
const location = { hash: "", pathname: "/", search: "" };
vm.runInNewContext(code, {
  RuleSearch: require(searchJs), location,
  document: { getElementById: el, querySelectorAll: () => [], activeElement: null,
    body: { classList: { add() {}, remove() {} }, style: {} } },
  window: { scrollY: 0, scrollTo() {}, addEventListener(type, fn) { listeners[type] = fn; } },
  history: { state: null, replaceState() {} },
});
const text = (html) => html.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
const titles = (cls) => [...els.content.innerHTML.matchAll(new RegExp(`<div class="${cls}">([^<]*)`, "g"))]
  .map((m) => m[1]);
const out = { meetings: titles("when"), side: text(els.side.innerHTML), rail: text(els.rail.innerHTML) };
els.side.onclick({ target: { closest: () => ({ dataset: { area } }) } });
Object.assign(out, { area: text(els.content.innerHTML), areaRail: text(els.rail.innerHTML) });
els.q.on.input({ target: { value: query } });
out.hits = titles("title");
out.status = slugs.map((slug) => {
  location.hash = `#regel/${slug}`;
  listeners.hashchange();
  return text(els.modal.innerHTML.match(/<p class="status">([\s\S]*?)<\/p>/)[1]);
});
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_page_lists_todays_rules_and_what_is_on_its_way(tmp_path):
    # Three rules of one area on 9 October 2026: one in force, one repealed, one adopted but in force only from 2027.
    adopted = decision(ref="rep2024#1", doc_id="rep2024", dato="2024-03-24",
                       tekst="Licensgebyret er 300 kr. pr. løfter.")
    started = decision(ref="rep2020#1", doc_id="rep2020", dato="2020-03-01",
                       tekst="Startgebyret er 200 kr. pr. løfter.")
    repealed = decision(ref="rep2025#1", doc_id="rep2025", dato="2025-03-30", handling="ophaev",
                        tekst="Startgebyret pr. løfter er afskaffet.")
    coming = decision(ref="hb2026#1", doc_id="hb2026", dato="2026-09-15", gaelder_fra="2027-01-01",
                      tekst="Kontingentet er 100 kr.")
    raw = [rule("Licensgebyr", "licensgebyr", adopted, kategori="okonomi"),
           rule("Startgebyr", "startgebyr", started, (repealed, "ophaevet"), kategori="okonomi"),
           rule("Kontingent", "kontingent", coming, kategori="okonomi")]
    page = tmp_path / "index.html"
    page.write_text(website.page_html([], [adopted, started, repealed, coming], raw, {}, date(2026, 10, 9)))
    script = tmp_path / "page.js"
    script.write_text(PAGE_PROBE)
    result = subprocess.run([NODE, str(script), str(page), str(SEARCH_JS), "0", "løfter", "licensgebyr", "startgebyr",
                             "kontingent"], capture_output=True, text=True, check=True, timeout=30)
    out = json.loads(result.stdout)

    # The area lists only the rule in force today and counts it; what is adopted but not in force yet is on its way.
    assert "Licensgebyr" in out["area"] and "Startgebyr" not in out["area"] and "Kontingent" not in out["area"]
    assert out["side"].startswith("Områder Medlemskab 1 Stævner 0")
    assert out["rail"] == out["areaRail"] == "På vej Fra 1. januar 2027 Medlemskab Kontingent Kontingentet er 100 kr."
    # The decisions come newest meeting first, across years.
    assert out["meetings"] == ["15. september 2026", "30. marts 2025", "24. marts 2024", "1. marts 2020"]
    # A search ranks the rule in force today above the repealed one, which matches more often.
    assert out["hits"] == ["Licensgebyr", "Startgebyr"]
    # Each popup says where the rule stands today.
    assert out["status"][0].startswith("Gældende i dag. Vedtaget af ? den 24. marts 2024")
    assert out["status"][1:] == ["Ikke gældende i dag.", "Vedtaget 15. september 2026 – gælder fra 1. januar 2027."]
