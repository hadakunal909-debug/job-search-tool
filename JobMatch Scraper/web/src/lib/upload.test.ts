/* The upload helper must hand fetch a FormData and set NO Content-Type.
 *
 * This is the fetch-path replacement for the enctype guard on the Jinja forms. That guard
 * exists because a missing enctype posts the field NAME and no file, and it fails SILENTLY.
 * The modern version of the same silent failure is setting Content-Type: application/json on a
 * multipart body: the browser then never writes the multipart boundary, the server parses no
 * file, and the user is told nothing.
 *
 * scripts/test_resume_upload.py asserts the enctype on the templates. This asserts the same
 * invariant for the React path, which no Python test can reach.
 */
import { describe, expect, it, vi, afterEach } from "vitest";
import { upload, post, setCsrf } from "./api";

function stubFetch(status = 200, body: unknown = { ok: true }) {
  const spy = vi.fn(async () => ({
    ok: status >= 200 && status < 300,
    status,
    redirected: false,
    url: "http://localhost/api/x",
    headers: new Headers({ "content-type": "application/json" }),
    json: async () => body,
    text: async () => JSON.stringify(body),
  }));
  vi.stubGlobal("fetch", spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("upload()", () => {
  it("passes the FormData through untouched", async () => {
    const spy = stubFetch();
    const form = new FormData();
    form.append("resume_file", new Blob(["cv"]), "cv.pdf");
    await upload("/api/onboard/resume", form);

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.body).toBe(form);
    expect(init.body).toBeInstanceOf(FormData);
  });

  it("does NOT set Content-Type, or the multipart boundary is never written", async () => {
    const spy = stubFetch();
    await upload("/api/onboard/resume", new FormData());

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("still sends the CSRF header, since the route checks it", async () => {
    setCsrf("tok-123");
    const spy = stubFetch();
    await upload("/api/onboard/resume", new FormData());

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("tok-123");
  });

  it("sends cookies, because auth is a session cookie", async () => {
    const spy = stubFetch();
    await upload("/api/onboard/resume", new FormData());

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.credentials).toBe("same-origin");
  });
});

describe("post()", () => {
  it("DOES set Content-Type for a JSON body", async () => {
    // The mirror of the test above: the exemption is specific to FormData, not general.
    const spy = stubFetch();
    await post("/api/prefs", { min: 45 });

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(init.body).toBe(JSON.stringify({ min: 45 }));
  });

  it("sets no Content-Type when there is no body at all", async () => {
    const spy = stubFetch();
    await post("/api/action");

    const [, init] = spy.mock.calls[0] as unknown as [string, RequestInit];
    expect(new Headers(init.headers).has("Content-Type")).toBe(false);
  });
});
