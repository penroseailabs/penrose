---
name: play-store
description: Play Store by deep link: an app's page, searching apps, installing, what an install screen shows.
packages: com.android.vending
apps: Play Store, Google Play
---

- An app's page: `open_link(uri="market://details?id=<package>")`. Search: `open_link(uri="market://search?q=<words>")`.
- On the page, Install starts the download; the button reads Open when it is done. `wait_for(text="Open")`
  with a few retries rather than tapping Install again.
- A "Complete account setup" or payment sheet may appear on a first install: Skip it, never add a payment
  method.
- `install_app` by package ID is faster than Play Store when it works; Play Store is the fallback.
