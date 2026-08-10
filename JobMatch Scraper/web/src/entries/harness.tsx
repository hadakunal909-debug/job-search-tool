/* Phase 2 harness probe. Proves the whole chain end to end: Vite build -> hashed asset in
 * static/dist -> manifest lookup by vite_entry() -> module script under the app's CSP ->
 * React mounts -> props arrive from the server -> the design tokens apply to React markup.
 *
 * Deleted in Phase 3, along with its route. It exists so the pipeline is verified BEFORE any
 * real screen depends on it.
 */
import { createRoot } from "react-dom/client";
import { pageProps, setCsrf } from "../lib/api";

type Props = { user: string; csrf: string; builtFor: string };

function Harness({ user, builtFor }: Props) {
  return (
    <div className="panel">
      <h2>React is mounted.</h2>
      <p className="sub">
        Server rendered the shell, Vite built this island, and the manifest lookup found it.
      </p>
      <div className="callout">
        Signed in as <b>{user}</b>. Route: <b>{builtFor}</b>.
      </div>
      <p>
        <span className="h1b">H-1B</span>
        <span className="pay">$140k</span>
        <span className="agency">Staffing agency</span>
      </p>
      <button className="btn primary" type="button">
        Styled by the same tokens as the Jinja pages
      </button>
    </div>
  );
}

const props = pageProps<Props>();
setCsrf(props.csrf);
const el = document.getElementById("root");
if (el) createRoot(el).render(<Harness {...props} />);
