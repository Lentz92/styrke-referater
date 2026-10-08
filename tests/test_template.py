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
