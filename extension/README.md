# JobMatch Helper — Chrome extension

Save any job to your JobMatch tracker with one click, and autofill application forms
from your saved profile.

## Install (one time, ~1 min)
1. In JobMatch, open **Profile** and copy your **token** (looks like `username:xxxxxxxx`).
2. In Chrome go to **`chrome://extensions`**.
3. Turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select this **`extension/`** folder.
5. Click the puzzle-piece icon → pin **JobMatch Helper**.
6. Click the extension, paste your **token**, leave the App URL as
   `https://stemjobs.astrochakra.co` (or `http://127.0.0.1:5000` to test against a local
   preview), and hit **Save**.

## Use
- **📌 Save to JobMatch** — on any job posting (LinkedIn, Workday, a company site), open the
  extension, tweak the title/company if needed, and click. It's logged in your **Applications**
  tab (status *applied*, today's date, your main résumé attached). Duplicates are ignored.
- **✍️ Autofill this form** — on an application page, click to fill common fields from your
  Profile: name, email, phone, location, LinkedIn, and the work-authorization / sponsorship
  questions.

## Honest limits
- **It cannot upload your résumé file.** Browsers block scripts from putting a file into an
  upload box, for security. Autofill handles the text fields; you drop in the résumé yourself.
- **Autofill is best-effort.** Every site's form is different, so some fields won't match
  (especially custom/Workday multi-step forms). Capture ("Save to JobMatch") is the reliable part.
- Your token links the extension to your account — keep it private. Re-open Profile any time to
  copy it again.
