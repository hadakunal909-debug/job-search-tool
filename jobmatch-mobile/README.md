# JobMatch — phone app (Expo / React Native)

A standalone Tinder-style job-swipe app for your iPhone/Android. It talks to your existing
JobMatch backend, so it shows **your** jobs, scores, and account.

- **Swipe right** = apply → marks the job applied, opens the company's apply page, and builds a
  **tailored résumé** you can share/upload.
- **Swipe left** = pass.
- **Tap a card** = full job description + skill match.
- **Résumés** button (top right) = your tailored résumés, each with a **Share / Save** button
  (AirDrop it, save to Files, email it — then upload on the company site).

It's all in one file: **`App.js`**.

---

## Before you start (one-time)

1. **Install “Expo Go”** on your phone (free — App Store / Play Store). This is what runs the app.
2. **Deploy the backend** so the app has an API to talk to. The app needs these routes live on your
   server (`https://stemjobs1.astrochakra.co`): `/api/app/login`, `/api/app/feed`, `/api/app/job`,
   `/api/app/action`, `/api/ext/tailor`. They're already written in `JobMatch Scraper/web.py` on the
   `claude/tinder-job-swipe-app-d0f5c8` branch — deploy = merge/pull that branch on cPanel + restart
   the Python app. (Ask Claude to commit + push, then pull on cPanel.)

---

## Easiest way to see it — Expo Snack (no computer setup)

1. On your computer, open **https://snack.expo.dev**.
2. Delete the sample `App.js`, then **paste the contents of this `App.js`**.
3. Snack detects the imports; if prompted, accept adding **expo-secure-store**, **expo-file-system**,
   **expo-sharing**.
4. In the top of `App.js`, leave `API_BASE` as your live site (or change it).
5. Click **“My Device”** → a QR code appears → **scan it with your iPhone camera** → it opens in
   Expo Go.
6. To see it immediately with **no backend/login**, tap **“Try the demo (no account)”** — it loads
   sample jobs so you can swipe, open details, and test the résumé sheet right away.
7. For real jobs, log in with your JobMatch **username + password** (needs the backend deployed).

## Run it locally instead (needs Node.js)

```bash
npx create-expo-app@latest jobmatch-app
cd jobmatch-app
npx expo install expo-secure-store expo-file-system expo-sharing
```
Then replace the generated `App.js` with the `App.js` from this folder, and:
```bash
npx expo start
```
Scan the QR with your phone (Expo Go). Phone and computer must be on the same Wi-Fi (or use
`npx expo start --tunnel`).

> Using `create-expo-app` guarantees the Expo SDK matches your Expo Go version — that's why it's
> preferred over a hand-written `package.json`.

---

## Settings

- **`API_BASE`** (top of `App.js`) — your backend URL. You can also tap **“Advanced: change
  server”** on the login screen to point it somewhere else (e.g. a computer on your Wi-Fi running
  `python web.py`, like `http://192.168.1.50:5057`) without editing code.

## Notes / limits

- The app can't *submit* an application end-to-end (company sites are login/CAPTCHA-walled). Right-
  swipe opens the apply page and hands you a tailored résumé to upload — same honest model as the
  web app.
- Tailoring takes ~10–40s per job and needs an AI key configured on the server; without one it
  falls back to your best-matching résumé (no AI rewrite). Résumés build one at a time in the
  background and show up under the **Résumés** button.
- Auth uses a long-lived token stored securely on the device; **Log out** (top right ⎋) clears it.
