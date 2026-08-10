/* The one place React talks to Flask.
 *
 * Auth needs no server change: islands are server rendered inside an already authenticated
 * response, so React never sees a logged-out state. CSRF needs no server change either, because
 * web._check_csrf already accepts an X-CSRF-Token header as well as the _csrf form field.
 */

/** Props the server rendered into the page. Read once; the block is not executable. */
export function pageProps<T>(): T {
  const el = document.getElementById("page-props");
  if (!el || !el.textContent) return {} as T;
  try {
    return JSON.parse(el.textContent) as T;
  } catch {
    return {} as T;
  }
}

let csrf = "";
/** Set from the props block at mount. Never read from a global inline script. */
export function setCsrf(token: string) {
  csrf = token || "";
}

export class SessionExpired extends Error {}

/* A dead session answers an XHR with a 302 to /login, and fetch FOLLOWS it, so the response
   that comes back is the login page with status 200. Parsing that as JSON throws something
   unreadable. Detect it and hard-navigate instead, which is what the user needs anyway. */
function looksLikeLogin(res: Response): boolean {
  return res.redirected && new URL(res.url).pathname === "/login";
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET" && method !== "HEAD") {
    if (csrf) headers.set("X-CSRF-Token", csrf);
    if (init.body !== undefined && !(init.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
  }
  const res = await fetch(url, { ...init, headers, credentials: "same-origin" });
  if (res.status === 401 || looksLikeLogin(res)) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    throw new SessionExpired("session expired");
  }
  if (!res.ok) throw new Error(`${method} ${url} failed: ${res.status}`);
  const type = res.headers.get("content-type") || "";
  return (type.includes("application/json") ? await res.json() : ((await res.text()) as unknown)) as T;
}

export const get = <T>(url: string) => request<T>(url);
export const post = <T>(url: string, body?: unknown) =>
  request<T>(url, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

/* Uploads: pass the FormData and set NO Content-Type. The browser has to write the multipart
   boundary itself. Hand-setting application/json here is the modern way to make a file vanish
   silently, which is the same failure the enctype guard protects against on the Jinja forms. */
export const upload = <T>(url: string, form: FormData) =>
  request<T>(url, { method: "POST", body: form });
