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
import { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { pageProps, setCsrf, upload } from "../lib/api";

type Props = { csrf: string; hasResume: boolean; resumeText?: string };
type Role = { key: string; label: string };
type Parsed = {
  ok: boolean;
  error?: string;
  chars?: number;
  years?: number;
  skills?: string[];
  roles?: Role[];
  text?: string;
};

function Echo({ p, text }: { p: Parsed | null; text: string }) {
  // Only render what was genuinely detected. An invented number on the one screen that is
  // asking the user to trust the parser would be worse than saying nothing.
  const chips: string[] = [];
  if (p?.years) chips.push(`${p.years} years`);
  (p?.roles || []).slice(0, 3).forEach((r) => chips.push(r.label));
  (p?.skills || []).slice(0, 5).forEach((s) => chips.push(s));
  return (
    <div className="echo">
      <p className="echo-h">{p ? "Here is what we read." : "Your résumé is saved."}</p>
      {chips.length > 0 ? (
        <div className="echo-chips">
          {chips.map((c) => (
            <span className="vt" key={c}>
              {c}
            </span>
          ))}
        </div>
      ) : p ? (
        <p className="dim">
          We saved the text but could not pick out any skills. That is fine, the match scores
          still use the whole thing.
        </p>
      ) : null}
      <p className="dim">
        {(p?.chars ?? text.length).toLocaleString()} characters saved. You can edit the text or replace the file below.
      </p>
    </div>
  );
}

function ResumeStep({ csrf, hasResume, resumeText = "" }: Props) {
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [error, setError] = useState("");
  const [savedText, setSavedText] = useState(resumeText);
  const [draftText, setDraftText] = useState(resumeText);
  const [mode, setMode] = useState<"choose" | "saved" | "paste" | "replace">(
    resumeText ? "saved" : "choose",
  );
  const fileRef = useRef<HTMLInputElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const confirmationRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (mode === "paste") textRef.current?.focus();
    if (mode === "replace") fileRef.current?.focus();
    if (mode === "saved") confirmationRef.current?.focus();
  }, [mode]);

  useEffect(() => {
    const form = document.getElementById("resume-island")?.closest("form");
    function waitForSave(event: Event) {
      if (!busyRef.current) return;
      event.preventDefault();
      setError("Please wait until your résumé has finished saving.");
    }
    form?.addEventListener("submit", waitForSave);
    return () => form?.removeEventListener("submit", waitForSave);
  }, []);

  function editText() {
    setDraftText(savedText || draftText);
    setError("");
    setMode("paste");
  }

  function cancel() {
    setDraftText(savedText);
    setError("");
    setMode(savedText ? "saved" : "choose");
  }

  async function send(form: FormData) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    try {
      const res = await upload<Parsed>("/api/onboard/resume", form);
      if (res.ok && typeof res.text === "string" && res.text.trim()) {
        setSavedText(res.text);
        setDraftText(res.text);
        setParsed(res);
        setMode("saved");
      }
      else setError(res.error || "That didn't work. Try a different file, or paste the text.");
    } catch {
      setError("We couldn't reach the server. Try again.");
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    const form = new FormData();
    form.append("resume_file", f);
    form.append("_csrf", csrf);
    // A failed attempt must allow selecting this same file again after correction.
    e.target.value = "";
    void send(form);
  }

  function onPaste() {
    const text = draftText.trim();
    if (!text) {
      setError("Paste your résumé text before saving.");
      textRef.current?.focus();
      return;
    }
    const form = new FormData();
    form.append("resume", text);
    form.append("_csrf", csrf);
    void send(form);
  }

  if (mode === "saved") return (
    <>
      <div ref={confirmationRef} tabIndex={-1} aria-label="Saved résumé" role="status">
        <Echo p={parsed} text={savedText} />
      </div>
      <div className="wact">
        <button type="button" className="btn" onClick={editText}>Edit pasted text</button>
        <button type="button" className="btn" onClick={() => { setError(""); setMode("replace"); }}>
          Replace file
        </button>
      </div>
    </>
  );

  return (
    <>
      {(hasResume || savedText) && (
        <div className="callout">
          Your saved résumé stays in place until you save a replacement.
        </div>
      )}
      {mode !== "paste" && <label className="dropzone" htmlFor="resume_file">
        <span className="dz-main">{busy ? "Reading it…" : "Choose a file, or drop one here"}</span>
        <span className="dz-sub">PDF, Word, or plain text</span>
        <input
          type="file"
          id="resume_file"
          name="resume_file"
          aria-label={mode === "replace" ? "Replace résumé file" : "Upload résumé file"}
          accept=".pdf,.docx,.txt,.md"
          ref={fileRef}
          disabled={busy}
          onChange={onFile}
        />
      </label>}

      {error && (
        <p className="field-error" role="alert">
          {error}
        </p>
      )}

      {mode !== "paste" ? (
        <p className="pastelink">
          <button type="button" className="btn tertiary" disabled={busy} onClick={editText}>
            {savedText ? "Or edit the saved text instead" : "Or paste the text instead"}
          </button>
        </p>
      ) : (
        <div className="pastebox is-open">
          <textarea
            ref={textRef}
            name="resume"
            value={draftText}
            onChange={(e) => setDraftText(e.target.value)}
            disabled={busy}
            rows={12}
            aria-label="Paste your résumé"
            placeholder="Paste the whole thing. Plain text is fine."
          />
          <button
            type="button"
            className={"btn" + (busy ? " is-loading" : "")}
            aria-busy={busy || undefined}
            disabled={busy}
            onClick={onPaste}
          >
            {savedText ? "Save changes" : "Save this"}
          </button>
        </div>
      )}

      {(mode === "paste" || mode === "replace") && (
        <p>
          <button type="button" className="btn ghost" disabled={busy} onClick={cancel}>
            Cancel {mode === "paste" ? "editing" : "replacement"}
          </button>
        </p>
      )}

      {mode === "paste" && <p className="dim">Save to check what we read, or Continue to save and go on.</p>}

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
