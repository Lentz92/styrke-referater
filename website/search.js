/* Rule search for the DSF website.
   MiniSearch does the ranking (BM25) with word-start and typo matching. On top of it: Danish stemming
   (Snowball), compound words indexed with their parts too ("licensgebyr" also counts as "licens" and
   "gebyr") and a hand-written thesaurus (website/synonyms.json). Runs in the browser; also loads in Node
   for testing. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(require("./vendor/minisearch.js"));
  else root.RuleSearch = factory(root.MiniSearch);
})(typeof self !== "undefined" ? self : this, function (MiniSearch) {
  "use strict";

  // Snowball's Danish stop word list, minus "uden" (meaningful in rules: "uden krav").
  const STOPWORDS = new Set(("og i jeg det at en den til er som på de med han af for ikke der var mig sig men et har om vi " +
    "min havde ham hun nu over da fra du ud sin dem os op man hans hvor eller hvad skal selv her alle vil blev kunne ind " +
    "når være dog noget ville jo deres efter ned skulle denne end dette mit også under have dig anden hende mine alt " +
    "meget sit sine vor mod disse hvis din nogle hos blive mange ad bliver hendes været thi jer sådan").split(" "));

  /* ---- Danish Snowball stemmer: https://snowballstem.org/algorithms/danish/stemmer.html ---- */
  const VOWELS = "aeiouyæåø";
  const S_ENDING = "abcdfghjklmnoprtvyzå'"; // letters after which a final -s is removed
  const UNDOUBLE = "bdfgklmnprst"; // final double consonants that are reduced to one
  const STEP1 = ["erendes", "erende", "hedens", "ethed", "erede", "heden", "heder", "endes", "ernes", "erens", "erets",
    "ered", "ende", "erne", "eres", "eren", "erer", "heds", "enes", "eret", "hed", "ene", "ere", "ens", "ers", "ets",
    "en", "er", "es", "et", "e", "s"];
  const STEP3 = ["elig", "løst", "lig", "els", "ig"];

  // R1 starts after the first non-vowel that follows a vowel, but never before the 4th letter.
  function region1(w) {
    if (w.length < 3) return w.length;
    let i = 0;
    while (i < w.length && !VOWELS.includes(w[i])) i++;
    while (i < w.length && VOWELS.includes(w[i])) i++;
    return i >= w.length ? w.length : Math.max(i + 1, 3);
  }
  const inR1 = (w, suffix, p1) => w.endsWith(suffix) && w.length - suffix.length >= p1;
  const consonantPair = (w, p1) => (["gd", "dt", "gt", "kt"].some((s) => inR1(w, s, p1)) ? w.slice(0, -1) : w);

  function stem(word) {
    const p1 = region1(word);
    let w = word;
    const s1 = STEP1.find((s) => inR1(w, s, p1));
    if (s1 === "s") { if (S_ENDING.includes(w[w.length - 2])) w = w.slice(0, -1); }
    else if (s1) w = w.slice(0, -s1.length);
    w = consonantPair(w, p1);
    if (w.endsWith("igst")) w = w.slice(0, -2);
    const s3 = STEP3.find((s) => inR1(w, s, p1));
    if (s3 === "løst") w = w.slice(0, -1);
    else if (s3) w = consonantPair(w.slice(0, -s3.length), p1);
    const n = w.length;
    if (n - 1 >= p1 && UNDOUBLE.includes(w[n - 1]) && w[n - 1] === w[n - 2]) w = w.slice(0, -1);
    return w;
  }

  /* ---- index ---- */
  const tokenize = MiniSearch.getDefault("tokenize");
  const words = (text) => tokenize(text || "").map((t) => t.toLowerCase()).filter((w) => w.length >= 2 && !STOPWORDS.has(w));
  const processTerm = (term) => {
    const w = term.toLowerCase();
    return w.length < 2 || STOPWORDS.has(w) ? null : stem(w);
  };
  const FIELDS = ["title", "summary", "history", "note", "area"];
  const BOOST = { title: 3, summary: 1.5, history: 1, note: 0.5, area: 1 };
  const scaled = (factor) => Object.fromEntries(Object.entries(BOOST).map(([f, b]) => [f, b * factor]));

  /** docs: [{ id, title, summary, history, note, area }]; synonymGroups: [["vm", "verdensmesterskab"], …] */
  function create(docs, synonymGroups = []) {
    // Compound parts must be words that occur on their own somewhere in the rules.
    const vocabulary = new Set();
    for (const d of docs) for (const f of ["title", "summary", "history", "note"]) for (const w of words(d[f])) if (w.length >= 3) vocabulary.add(w);
    const splits = new Map();
    function split(word) {
      if (!splits.has(word)) {
        const found = [];
        for (let i = 3; i <= word.length - 4; i++) {
          const left = word.slice(0, i), right = word.slice(i);
          if (!vocabulary.has(right)) continue;
          // Danish compounds often join with -s- or -e- ("landsholdsdragt").
          const base = vocabulary.has(left) ? left : /[se]$/.test(left) && vocabulary.has(left.slice(0, -1)) ? left.slice(0, -1) : null;
          if (base) found.push([base, right]);
        }
        splits.set(word, found);
      }
      return splits.get(word);
    }

    // Thesaurus: each group becomes one concept term, so a rule counts once per group however many of
    // its words it uses (summing a score per synonym would let synonyms outrank the words searched for).
    const concepts = new Map(); // stem, or stems of a phrase joined by " " -> concept term
    synonymGroups.forEach((group, g) => group.forEach((term) => concepts.set(words(term).map(stem).join(" "), `zsyn${g}`)));

    const index = new MiniSearch({
      fields: FIELDS,
      // Indexing: a compound word also counts as each of its parts, and every stem as its concept.
      processTerm: (term) => {
        const s = processTerm(term);
        if (!s) return null;
        const stems = [s, ...split(term.toLowerCase()).flat().map(stem)];
        return [...new Set([...stems, ...stems.map((x) => concepts.get(x)).filter(Boolean)])];
      },
      searchOptions: {
        processTerm,
        boost: BOOST,
        combineWith: "OR",
        prefix: (term) => term.length >= 3,
        fuzzy: (term) => (term.length >= 5 ? 0.2 : false),
      },
    });
    index.addAll(docs);

    function search(query, { boostDocument } = {}) {
      const terms = words(query);
      if (!terms.length) return [];
      const queries = [terms.join(" ")];
      const stems = terms.map(stem), found = new Set();
      for (const [i, w] of terms.entries()) {
        for (const [a, b] of split(w)) queries.push({ queries: [`${a} ${b}`], combineWith: "AND", boost: scaled(0.7) });
        found.add(concepts.get(stems[i])).add(concepts.get(stems.slice(i, i + 2).join(" ")));
      }
      for (const c of found) {
        if (c) queries.push({ queries: [c], processTerm: (t) => t, prefix: false, fuzzy: false, boost: scaled(0.5) });
      }
      const hits = index.search({ combineWith: "OR", queries }, { boostDocument });
      // Weak matches (one typo-tolerant hit in a long history) only add noise below the good ones.
      const floor = hits.length ? hits[0].score * 0.12 : 0;
      return hits.filter((h) => h.score >= floor);
    }

    /* The first passage among `texts` that contains a matched word, as HTML with the matches in <mark>.
       `terms` are the matched index terms (stems) from a search hit; null when no text contains one. */
    function excerpt(texts, terms, maxWords = 24) {
      const matched = new Set(terms);
      const hit = (w) => matched.has(stem(w)) || matched.has(concepts.get(stem(w)));
      const isHit = (token) => token.toLowerCase().split(/[^\p{L}\p{N}]+/u).some((w) =>
        w.length >= 2 && (hit(w) || split(w).some(([a, b]) => hit(a) || hit(b))));
      for (const text of texts) {
        if (!text) continue;
        const tokens = text.split(/\s+/).filter(Boolean);
        const first = tokens.findIndex(isHit);
        if (first < 0) continue;
        const start = Math.max(0, Math.min(first - 6, tokens.length - maxWords));
        const end = Math.min(tokens.length, start + maxWords);
        const body = tokens.slice(start, end).map((t) => {
          if (!isHit(t)) return esc(t);
          const [, lead, core, tail] = t.match(/^([^\p{L}\p{N}]*)(.*?)([^\p{L}\p{N}]*)$/u); // keep punctuation outside <mark>
          return `${esc(lead)}<mark>${esc(core)}</mark>${esc(tail)}`;
        }).join(" ");
        return (start > 0 ? "… " : "") + body + (end < tokens.length ? " …" : "");
      }
      return null;
    }

    return { search, excerpt };
  }

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  return { create, stem };
});
