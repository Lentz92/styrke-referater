"""The page's hash handling (website/template.html), run in Node against stubs of the browser."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
TEMPLATE = Path(__file__).resolve().parent.parent / "website" / "template.html"

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


# Runs the popup's code (from the open-rule comment up to init(), plus init's hashchange listener) against a model
# of the browser: a page that cannot scroll while its body is position:fixed (as on iOS), and session history whose
# entries save their scroll and restore it on Back/Forward unless their scroll restoration is "manual". Prints, after
# each step, whether the page is pinned and which offset of the page the reader sees.
POPUP_PROBE = r"""
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8");
const start = html.indexOf("/* The open rule lives in the URL"), end = html.indexOf("\nfunction init()");
if (start < 0 || end < 0) throw new Error("popup code not found");
const onHashchange = html.match(/addEventListener\("hashchange", (.+)\);$/m)[1];

const D = { aliases: {} };
const R = [{ slug: "startgebyr" }, { slug: "licensgebyr" }];
const href = (ri) => `#regel/${R[ri].slug}`;
const ruleDetail = () => "";
const el = () => ({ innerHTML: "", scrollTop: 0, focus() {} });
const els = { modal: el(), close: el() };
const $ = (id) => els[id];

let y = 0; // the window's scroll offset on a 5000px page in an 800px window
const body = { classList: { add() {}, remove() {} }, style: { top: "", width: "", _position: "",
  get position() { return this._position; },
  set position(v) { this._position = v; if (v === "fixed") y = 0; } } };
const document = { body, activeElement: el() };
const pinned = () => body.style.position === "fixed";
const window = { get scrollY() { return y; }, scrollTo(_x, to) { y = pinned() ? 0 : Math.max(0, Math.min(to, 4200)); } };
const seen = () => (pinned() ? -parseFloat(body.style.top) : y);

const location = { hash: "", pathname: "/", search: "" };
const entries = [{ hash: "", scroll: 0, restore: "auto" }];
let at = 0, fire = null;
const history = {
  get scrollRestoration() { return entries[at].restore; },
  set scrollRestoration(v) { entries[at].restore = v; },
  replaceState(_s, _t, url) { entries[at].hash = location.hash = url.startsWith("#") ? url : ""; },
  back() { go(at - 1); },
  forward() { go(at + 1); },
};
function go(to) {
  entries[at].scroll = y;
  at = to; location.hash = entries[at].hash;
  if (entries[at].restore === "auto") window.scrollTo(0, entries[at].scroll);
  fire();
}
function follow(hash) { // a link to a rule: a new entry that inherits the current one's restoration mode
  entries[at].scroll = y;
  entries.splice(at + 1, Infinity, { hash, scroll: 0, restore: entries[at].restore });
  at += 1; location.hash = hash;
  fire();
}

const page = eval(`(() => { ${html.slice(start, end)}\n fire = ${onHashchange}; return { syncWithHash, closeRule }; })()`);
const out = [];
const step = (name, action) => { action(); out.push([name, pinned(), seen(), location.hash]); };
entries[0].hash = location.hash = "#regel/startgebyr";
step("opened by a shared link", () => page.syncWithHash());
step("closed with the close button", () => page.closeRule());
step("opened from a link further down", () => { window.scrollTo(0, 1500); follow("#regel/startgebyr"); });
step("another rule while open", () => follow("#regel/licensgebyr"));
step("Back to the first rule", () => history.back());
step("Back to the page", () => history.back());
step("Forward to the rule", () => history.forward());
step("closed with Esc", () => page.closeRule());
step("Esc again with nothing open", () => page.closeRule());
console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_an_open_rule_holds_the_page_still_and_closing_returns_to_the_same_spot(tmp_path):
    script = tmp_path / "popup.js"
    script.write_text(POPUP_PROBE)
    result = subprocess.run([NODE, str(script), str(TEMPLATE)], capture_output=True, text=True, check=True, timeout=30)

    # [step, page pinned, offset the reader sees, address]: open pins the page where the reader was, switching rules
    # and Back/Forward between open rules keep that spot, and every close unpins and scrolls back to it.
    assert json.loads(result.stdout) == [
        ["opened by a shared link", True, 0, "#regel/startgebyr"],
        ["closed with the close button", False, 0, ""],
        ["opened from a link further down", True, 1500, "#regel/startgebyr"],
        ["another rule while open", True, 1500, "#regel/licensgebyr"],
        ["Back to the first rule", True, 1500, "#regel/startgebyr"],
        ["Back to the page", False, 1500, ""],
        ["Forward to the rule", True, 1500, "#regel/startgebyr"],
        ["closed with Esc", False, 1500, ""],
        ["Esc again with nothing open", False, 1500, ""],
    ]
