---
name: classifieds
description: Second-hand marketplaces by region: Kleinanzeigen (Germany), Craigslist and Facebook Marketplace (US), Gumtree (UK, Australia), leboncoin (France), Wallapop (Spain), Marktplaats (Netherlands), Subito (Italy), OLX, Mercado Libre (Latin America), Jiji (Africa), Carousell (South-East Asia), Vinted; search by link with a price cap and a radius; messaging a seller is a logged-in, consequential step.
packages: com.ebay.kleinanzeigen, com.craigslist.CraigslistMobile, com.facebook.katana, com.gumtree.android, com.ebay.gumtree.au, fr.leboncoin, com.wallapop, com.horizon.marktplaats, it.subito, com.olx.southasia, com.olxbr.android, pl.tablica, com.mercadolibre, ng.jiji.app, com.thecarousell.Carousell, fr.vinted
apps: Kleinanzeigen, eBay Kleinanzeigen, Craigslist, Facebook Marketplace, Marketplace, Gumtree, leboncoin, Wallapop, Marktplaats, Subito, OLX, Mercado Libre, MercadoLibre, Jiji, Carousell, Vinted, classifieds, second-hand, used
---

- Search by link where the site has one, with the price cap and the area in the URL, so the first page
  already is the answer (these are drafts until a run confirms them; if a link lands elsewhere, use the
  site's search box and filters):
  - Kleinanzeigen: `https://www.kleinanzeigen.de/s-<city>/preis::<max>/<words>/k0l<location id>r<km>`
    (Berlin is l3331; the id of another city appears in the URL after one search from its page).
  - Craigslist: `https://<city>.craigslist.org/search/sss?query=<words>&max_price=<max>` (sss is all
    for sale; bia bikes, fuo furniture, ela electronics).
  - Gumtree UK: `https://www.gumtree.com/search?q=<words>&search_location=<postcode>&max_price=<max>`;
    Australia `https://www.gumtree.com.au/s-<words>/k0?price=0__<max>`.
  - leboncoin: `https://www.leboncoin.fr/recherche?text=<words>&price=min-<max>`; Marktplaats
    `https://www.marktplaats.nl/q/<words>/`; Subito `https://www.subito.it/annunci-italia/vendita/usato/?q=<words>`.
  - Mercado Libre: `https://listado.mercadolibre.com.mx/<words>` (`.com.ar`, `.com.br`, `.cl`, `.com.co`
    by country), with the price filter on the page.
  - Facebook Marketplace, Wallapop, Vinted, OLX, Jiji and Carousell: the app (or the site) needs the
    user's login to search properly; "Continue with Google" where offered, else report needs_input for
    an account the brief does not cover.
- The search area: the site's location field with the user's city or postcode from `phone_location`
  and a radius of about 5 to 15 miles (10 to 25 km); listings from far away mean the location was not applied, so fix that
  before reading more.
- A listing is one page: price, condition, the seller's area, posted date, and the photos (a
  screenshot shows the item). Note price, title, area and link for each candidate; three good ones
  answer "find me a used X".
- Messaging a seller ("is it still available?") needs an account and is a message sent in the user's
  name: a step of its own, only when the user asked for it, with the text in the instructions. Never
  send to more sellers than asked, never twice, and never share the user's phone number unasked.
- If Kleinanzeigen's app shows an Application Error twice, use the site by the link above instead of alternating.
  Messaging a seller in the app needs an e-mail and password login (no code sign-in): without the
  user's credentials in the brief, report needs_input once, and never ask twice for the same thing.
