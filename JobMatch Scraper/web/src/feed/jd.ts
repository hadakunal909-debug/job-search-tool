/* Job description rendering, ported from static/app.js (lines ~925-1116).
 *
 * WHY THIS IS THE FIRST THING PORTED IN PHASE 4, before any component work: it is ~180 lines
 * of heuristics tuned against measurements on the live corpus, and every threshold in it was
 * chosen for a reason recorded in the comments. If the port drifts, every job description in
 * the product renders as mush, and it would be discovered by reading, not by a test failing.
 * scripts/test_jd_render.py proves this file and the original produce byte-identical output
 * over 20 real descriptions covering all five shapes the corpus actually contains.
 *
 * Measured on 16,344 stored descriptions: 13,339 of them (82%) arrive as ONE unbroken run with
 * zero newlines, because Workday, iCIMS and Amazon hand over the whole posting as a single
 * ~4,000 character string. That is the reason this file is not three lines of `split("\n")`.
 *
 * SAFETY: every fragment goes through esc() BEFORE it is placed inside a tag, and no substring
 * of a description is ever concatenated into markup unescaped. esc() is the only door in. If
 * URL linkification is ever added it must run on the ALREADY-ESCAPED string; getting that
 * order backwards is the one way this becomes an XSS hole.
 */

/** Matches the DOM escape the original used (textContent -> innerHTML) exactly.
 *
 * The original called document.createElement, which cannot run under node, so this had to
 * become a pure function. Verified against a real browser rather than assumed: `&`, `<` and
 * `>` are escaped, U+00A0 becomes `&nbsp;`, and quotes are deliberately NOT escaped, because
 * innerHTML does not escape them and every consumer here places the result in element content
 * rather than in an attribute.
 */
export function esc(s: string | null | undefined): string {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\u00a0/g, "&nbsp;");
}

const JD_BULLET = /^\s*(?:[•·▪●◦‣⁃*–—-]|\(?\d{1,2}[.)])\s+/;
const JD_HEAD =
  /^(?:about|responsibilit|qualificat|requirement|what you|who you|the role|your role|benefit|perks|compensation|skills|experience|education|duties|essential|preferred|minimum|basic|nice to have|equal (?:employment )?opportunity|eeo|how to apply|why join|our team|job (?:summary|description|details))/i;

export function isJdHeading(t: string): boolean {
  if (!t || t.length > 70 || JD_BULLET.test(t)) return false;
  const letters = t.replace(/[^A-Za-z]/g, "");
  if (letters.length >= 3 && t === t.toUpperCase()) return true; // short ALL-CAPS line
  if (/:$/.test(t) && t.split(/\s+/).length <= 8) return true; // "Requirements:"
  // A known section name with no colon. Digits and $ disqualify, or "Salary: $120,000" would be
  // promoted from a fact to a heading.
  return (
    JD_HEAD.test(t) && t.split(/\s+/).length <= 8 && !/[.;,]$/.test(t) && !/[\d$]/.test(t)
  );
}

/* Two tiers, because precision matters more than recall: a false heading mid-sentence is far
   uglier than a missed one. STRONG names are unambiguous enough to match on a colon OR a
   following capital ("Overview This is a hybrid role" is how iCIMS writes it). WEAK ones are
   ordinary words that appear constantly in prose ("5 years of experience"), so they need an
   explicit colon before they count. */
const JD_SECTION_STRONG =
  "job description|position purpose|position summary|role summary|" +
  "essential (?:functions?|duties)|basic qualifications|minimum qualifications|" +
  "preferred qualifications|additional qualifications|key responsibilities|" +
  "primary responsibilities|what you(?:'|\u2019)ll (?:do|bring)|what you will do|" +
  "what you bring|what we(?:'|\u2019)re looking for|what we offer|who you are|" +
  "about (?:us|the role|the team|the company|the job|this role)|required skills|" +
  "day in the life|nice to have|how to apply|why join(?: us)?|our team|" +
  "equal (?:employment )?opportunity(?: employer)?|eeo statement|" +
  "pay range|salary range|compensation range|overview";
const JD_SECTION_WEAK =
  "summary|responsibilities|requirements|qualifications|benefits|perks|" +
  "compensation|education|experience|skills|duties";
const JD_SECTION = new RegExp(
  "\\b(" + JD_SECTION_STRONG + ")\\b(?::\\s+|\\s+(?=[A-Z]))" + "|\\b(" + JD_SECTION_WEAK + ")\\b:\\s+",
  "gi"
);
const JD_PARA_MAX = 360; // chars; above this a run is split on sentence boundaries

/* Lists that lost their bullets AND their punctuation: the iCIMS/Workday "Essential Functions"
   idiom. The boundary is a capital following a lowercase word, but splitting on ANY capital
   would wreck real prose, because the most frequent capitals in that position across the corpus
   are proper nouns (Boeing, Capital, Company, Engineering, One, States). So the split only fires
   before words that actually START a requirement, and only inside a run that has already failed
   the punctuation test. Lead, Support, Design, Manage, Build and Drive are deliberately excluded
   despite being common bullet openers: each is an ordinary noun that shows up capitalised
   mid-title ("team Lead Engineer"), and a false cut reads far worse than a missed one. */
const JD_ITEM_START =
  "Demonstrated|Demonstrates|Ability|Abilities|Proven|Proficien(?:cy|t|cies)|Familiarity|" +
  "Knowledge|Understanding|Excellent|Strong|Solid|Exceptional|Experience|Expertise|" +
  "Bachelor'?s?|Master'?s?|Minimum|Preferred|Required|Must|Should|Responsible|Working|" +
  "Assists?|Develops?|Ensures?|Maintains?|Performs?|Provides?|Coordinates?|Participates?|" +
  "Collaborates?|Implements?|Analyzes?|Prepares?|Monitors?|Reviews?|Conducts?|Evaluates?|" +
  "Recommends?|Identifies|Identify|Communicates?|Translates?|Oversees?|Establishes?|" +
  "Contributes?|Executes?|Delivers?|Partners?|Serves?|Troubleshoots?|Utilizes?";
// Keeps the matched preceding character and marks the cut with a control character rather than
// using a lookbehind. U+0001 cannot occur in a description, so splitting on it is unambiguous.
const JD_ITEM_RE = new RegExp("([a-z)\\]])\\s+(?=(?:" + JD_ITEM_START + ")\\b)", "g");
const JD_CUT = "\u0001";
const JD_ITEM_MIN = 25; // a real requirement line is long; a short piece means a bad cut
// A cut is wrong if the text before it ends on a word that cannot end a sentence. Measured over
// 600 real descriptions this is the whole of the remaining false-positive class.
const JD_DANGLING =
  /\b(?:a|an|the|and|or|of|with|to|for|in|on|at|by|from|as|plus|per|our|your|their|its|this|that|these|those|is|are|be|been|has|have|had|will|shall|may|any|all|each|other|including|includes|include)$/i;

export function jdFlatList(t: string): string[] | null {
  // Punctuation gate first: prose that already has sentences goes to the sentence splitter.
  const stops = (t.match(/[.!?]/g) || []).length;
  if (stops / (t.length / 1000) >= 6) return null;
  const raw = t.replace(JD_ITEM_RE, "$1" + JD_CUT).split(JD_CUT);
  // Heal bad cuts by gluing a dangling piece onto the one after it, rather than throwing the
  // whole split away: one wrong boundary should not cost the other eleven.
  const parts: string[] = [];
  for (let i = 0; i < raw.length; i++) {
    let piece = raw[i].trim();
    if (!piece) continue;
    while (JD_DANGLING.test(piece) && i + 1 < raw.length) piece += " " + raw[++i].trim();
    parts.push(piece);
  }
  if (parts.length < 4) return null; // a lead-in plus at least 3 items
  // From 1, not 0: parts[0] is whatever preceded the first item and is routinely a stub.
  for (let j = 1; j < parts.length; j++) if (parts[j].length < JD_ITEM_MIN) return null;
  return parts;
}

/** One run of prose to one or more blocks. Never emits a paragraph much longer than
 *  JD_PARA_MAX unless a single sentence is. */
export function jdChunk(t: string): string {
  t = (t || "").trim();
  if (!t) return "";
  // Some boards flatten a real <ul> into "• a • b • c" on one line. Two or more bullets is a
  // list; a single one is just a stray glyph inside a sentence.
  if ((t.match(/\u2022/g) || []).length >= 2) {
    const parts = t.split(/\s*\u2022\s*/);
    let lead = "";
    let items = "";
    if (parts.length && parts[0].trim() && t.charAt(0) !== "\u2022")
      lead = "<p>" + esc(parts.shift()!.trim()) + "</p>";
    for (let b = 0; b < parts.length; b++)
      if (parts[b].trim()) items += "<li>" + esc(parts[b].trim()) + "</li>";
    return lead + (items ? "<ul>" + items + "</ul>" : "");
  }
  if (t.length <= JD_PARA_MAX) return "<p>" + esc(t) + "</p>";
  // A requirements list that lost its bullets AND its full stops, rendered as a real <ul>,
  // because that is what it is. The first piece is the lead-in sentence before the list.
  const flat = jdFlatList(t);
  if (flat) {
    const head = "<p>" + esc(flat.shift()!.trim()) + "</p>";
    let li = "";
    for (let f = 0; f < flat.length; f++) li += "<li>" + esc(flat[f].trim()) + "</li>";
    return head + "<ul>" + li + "</ul>";
  }
  const sent = t.match(/[^.!?]+(?:[.!?]+["'\u2019)\]]*|$)/g) || [t];
  let out = "";
  let buf = "";
  for (let i = 0; i < sent.length; i++) {
    const s = sent[i].trim();
    if (!s) continue;
    if (buf && buf.length + s.length > JD_PARA_MAX) {
      out += "<p>" + esc(buf) + "</p>";
      buf = "";
    }
    buf = buf ? buf + " " + s : s;
  }
  return out + (buf ? "<p>" + esc(buf) + "</p>" : "");
}

export function jdParagraphs(text: string): string {
  const t = String(text || "").trim();
  if (!t) return "";
  let out = "";
  let last = 0;
  let m: RegExpExecArray | null;
  JD_SECTION.lastIndex = 0; // module-level regex carrying /g: its state is shared
  while ((m = JD_SECTION.exec(t)) !== null) {
    if (m.index === JD_SECTION.lastIndex) {
      JD_SECTION.lastIndex++;
      continue;
    } // no progress
    // Not a heading if it is finishing the sentence in front of it: "…is proud to be an" /
    // "Equal Opportunity Employer" is one sentence, and lifting the tail out of it leaves a
    // paragraph dangling on "an".
    if (JD_DANGLING.test(t.slice(last, m.index).trim())) continue;
    out += jdChunk(t.slice(last, m.index));
    out += '<h4 class="jdh">' + esc(m[1] || m[2]) + "</h4>";
    last = JD_SECTION.lastIndex;
  }
  return out + jdChunk(t.slice(last));
}

export function jdHTML(text: string | null | undefined): string {
  const src = String(text == null ? "" : text)
    .replace(/\r\n?/g, "\n")
    .replace(/\u00a0/g, " ");
  const lines = src.split("\n");
  const out: string[] = [];
  let para: string[] = [];
  let list: string[] | null = null;
  // jdParagraphs, not a bare <p>: on most boards a "line" here is the ENTIRE posting.
  const flushPara = () => {
    if (para.length) out.push(jdParagraphs(para.join(" ")));
    para = [];
  };
  const flushList = () => {
    if (list && list.length) out.push("<ul>" + list.join("") + "</ul>");
    list = null;
  };
  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].trim();
    if (!t) {
      flushPara();
      flushList();
      continue;
    } // a blank ends the block; runs of them vanish
    const bm = t.match(JD_BULLET);
    if (bm) {
      flushPara();
      if (!list) list = [];
      list.push("<li>" + esc(t.slice(bm[0].length).trim()) + "</li>");
      continue;
    }
    if (isJdHeading(t)) {
      flushPara();
      flushList();
      out.push('<h4 class="jdh">' + esc(t.replace(/:$/, "")) + "</h4>");
      continue;
    }
    flushList();
    // Hard-wrapped prose: a long previous line that does not end a sentence is mid-paragraph,
    // so join onto it. Otherwise start a new <p>, so separate one-line statements, which is how
    // most bullet-less descriptions are written, do not get glued into a wall.
    const prev = para.length ? para[para.length - 1] : "";
    if (prev && !(prev.length > 62 && !/[.:;!?]$/.test(prev))) flushPara();
    para.push(t);
  }
  flushPara();
  flushList();
  return out.join("");
}
