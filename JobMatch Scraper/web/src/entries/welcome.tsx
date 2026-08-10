/* Question 1 of onboarding, enhanced.
 *
 * ENHANCEMENT, not replacement. The server renders a working upload form inside this island's
 * container; React replaces it only once it has mounted. With JavaScript off, or if this bundle
 * fails to load, the plain form is still there and still completes onboarding. That matters
 * more here than anywhere else in the app: /welcome is the only path that sets
 * extra.onboarded and feed() hard-redirects new accounts into it, so a screen that depends on
 * JS to work would strand a brand new account in a loop with no way out.
 *
 * What it adds over the plain form: the parse echo. A scanned or photographed PDF has no text
 * to extract, and the server-rendered flow can only report that AFTER a redirect, as a flash
 * message on the next screen, by which point the user has moved on. Uploading inline and
 * showing what was actually read turns a silent failure into a correctable one.
 */
import { useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { pageProps, setCsrf, upload } from "../lib/api";

type Props = { csrf: string; hasResume: boolean };
type Role = { key: string; label: string };
type Parsed = {
  ok: boolean;
  error?: string;
  chars?: number;
  years?: number;
  skills?: string[];
  roles?: Role[];
};

function Echo({ p }: { p: Parsed }) {
  // Only render what was genuinely detected. An invented number on the one screen that is
  // asking the user to trust the parser would be worse than saying nothing.
  const chips: string[] = [];
  if (p.years) chips.push(`${p.years} years`);
  (p.roles || []).slice(0, 3).forEach((r) => chips.push(r.label));
  (p.skills || []).slice(0, 5).forEach((s) => chips.push(s));
  return (
    <div className="echo">
      <p className="echo-h">Here is what we read.</p>
      {chips.length > 0 ? (
        <div className="echo-chips">
          {chips.map((c) => (
            <span className="vt" key={c}>
              {c}
            </span>
          ))}
        </div>
      ) : (
        <p className="dim">
          We saved the text but could not pick out any skills. That is fine, the match scores
          still use the whole thing.
        </p>
      )}
      <p className="dim">
        {p.chars?.toLocaleString()} characters saved. Wrong? Upload a different file.
      </p>
    </div>
  );
}

function ResumeStep({ csrf, hasResume }: Props) {
  const [busy, setBusy] = useState(false);
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [error, setError] = useState("");
  const [pasting, setPasting] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);

  async function send(form: FormData) {
    setBusy(true);
    setError("");
    try {
      const res = await upload<Parsed>("/api/onboard/resume", form);
      if (res.ok) setParsed(res);
      else setError(res.error || "That didn't work. Try a different file, or paste the text.");
    } catch {
      setError("We couldn't reach the server. Try again.");
    } finally {
      setBusy(false);
    }
  }

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    const form = new FormData();
    form.append("resume_file", f);
    form.append("_csrf", csrf);
    void send(form);
  }

  function onPaste() {
    const text = (textRef.current?.value || "").trim();
    if (!text) return;
    const form = new FormData();
    form.append("resume", text);
    form.append("_csrf", csrf);
    void send(form);
  }

  if (parsed) return <Echo p={parsed} />;

  return (
    <>
      {hasResume && (
        <div className="callout">
          You already have a résumé saved. Adding a new one replaces it.
        </div>
      )}
      <label className="dropzone" htmlFor="resume_file">
        <span className="dz-main">{busy ? "Reading it…" : "Choose a file, or drop one here"}</span>
        <span className="dz-sub">PDF, Word, or plain text</span>
        <input
          type="file"
          id="resume_file"
          name="resume_file"
          accept=".pdf,.docx,.txt,.md"
          ref={fileRef}
          disabled={busy}
          onChange={onFile}
        />
      </label>

      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}

      {!pasting ? (
        <p className="pastelink">
          <button type="button" className="btn tertiary" onClick={() => setPasting(true)}>
            Or paste the text instead
          </button>
        </p>
      ) : (
        <div className="pastebox is-open">
          <textarea
            ref={textRef}
            rows={12}
            aria-label="Paste your résumé"
            placeholder="Paste the whole thing. Plain text is fine."
          />
          <button
            type="button"
            className={"btn" + (busy ? " is-loading" : "")}
            aria-busy={busy || undefined}
            onClick={onPaste}
          >
            Save this
          </button>
        </div>
      )}

      <p className="dim">
        We read the words out and don't keep the file. A scanned or photographed PDF has no text
        in it, so paste the text instead. You can change any of this later.
      </p>
    </>
  );
}

const props = pageProps<Props>();
setCsrf(props.csrf);
const el = document.getElementById("resume-island");
// createRoot().render() replaces the server-rendered fallback that is sitting in here.
if (el) createRoot(el).render(<ResumeStep {...props} />);
