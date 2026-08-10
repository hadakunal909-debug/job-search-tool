import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

/* Islands, not a single page app.
 *
 * Flask keeps owning every URL. Each migrated route renders a thin Jinja shell holding
 * <div id="root"> and a JSON props block, and gets ONE entry from the map below. Un-migrated
 * pages ship zero React. That removes the refresh-404 problem by construction, keeps one topbar
 * across both worlds during the migration, keeps _ev_page_view working (it only fires on
 * text/html 200s, so a real SPA would silently kill the dead-route analytics the admin panel
 * is built on), and makes every phase revertible with a one line template change.
 *
 * Not Next.js: it needs a Node runtime cPanel cannot host beside the Python app.
 */
export default defineConfig({
  plugins: [react()],
  // Must match where Flask serves the build from, or dynamic-import chunks 404 only on the
  // routes that lazy-load, which is the worst kind of bug because it passes local testing.
  base: "/static/dist/",
  build: {
    outDir: "../static/dist",
    emptyOutDir: true,
    // Flask reads this to turn an entry name into its hashed filename.
    manifest: true,
    // Vite's modulePreload polyfill emits an INLINE <script>, which the app's CSP blocks.
    // Every browser this app supports has native modulepreload.
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: {
        // One entry per migrated page. Phase 3 adds welcome, Phase 4 adds feed.
        harness: resolve(__dirname, "src/entries/harness.tsx"),
      },
      output: {
        // No code splitting until the base path is proven in production.
        manualChunks: undefined,
        // Everything in dist/ must be content hashed. _security_headers stamps
        // "immutable, max-age=604800" on all of /static/, so a non-hashed file there would be
        // cached for a week with no way to bust it short of renaming.
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    // Dev is served from Vite's own origin so Flask's CSP never applies to the HMR client.
    // Do NOT add a dev-only CSP relaxation to web.py; that is how 'unsafe-inline' reaches prod.
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:5000",
      "/static": "http://127.0.0.1:5000",
      "/login": "http://127.0.0.1:5000",
      "/prefs": "http://127.0.0.1:5000",
    },
  },
});
