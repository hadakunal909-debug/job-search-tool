"use strict";
// Content script on tesla.com/careers* — the most reliable trigger: this page context
// has fresh Akamai cookies by definition (you're browsing the site), so the import
// can't be challenged. Throttled inside jmRunTeslaImport to ~once a day.
(async () => {
  try {
    await jmSleep(3000);                  // let the page settle first
    await jmRunTeslaImport({ trigger: "page" });
  } catch (e) {}
})();
