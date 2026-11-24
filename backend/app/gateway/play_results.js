// Google Play's apps for each query, read in a hidden play.google.com tab of the phone's Chrome (gateway/play.py).
// Each search is a same-origin fetch of Play's own page, parsed without being shown: nothing comes on screen, and
// the Play Store app is never opened. A query that is a package ID reads that app's page instead.
// Called as (queries, limit) => [{apps: [{package, title, developer, rating, summary}]} | {app: {..., about}}
//   | {bounce} | {error}] per query.
(async (queries, limit) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const PACKAGE = /^[A-Za-z]\w*(\.[A-Za-z]\w*)+$/;
  const APP = /\/store\/apps\/details\?id=([\w.]+)/;
  const RATING = /^\d[.,]\d$/;
  const ICON = /^[a-z0-9]+(_[a-z0-9]+)*$/;  // an icon drawn by its name ("star", "arrow_forward")
  const leaves = el => {
    const out = [];
    const walk = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const t = clean(n.textContent);
      if (t && !ICON.test(t)) out.push(t);
    }
    return out;
  };
  const tidy = d => d.querySelectorAll('script,style,noscript,template,svg,button,[role=button]').forEach(e => e.remove());
  const id = a => (APP.exec(a.getAttribute('href') || '') || [])[1];
  // Play's pages only let HTML be parsed through a Trusted Types policy. The parsed page is never shown or
  // run (DOMParser documents are inert): it is only read.
  let trusted = s => s;
  if (window.trustedTypes) {
    const policy = window.penroseReader || (window.penroseReader = trustedTypes.createPolicy('penrose-reader', {createHTML: s => s}));
    trusted = s => policy.createHTML(s);
  }

  // A results page: one card per app, in Play's order. The card is the box around an app's link that holds no
  // other app's link; its first text is the name, the next the developer. The first result may be a wide card
  // that also carries the app's one-line summary.
  const results = d => {
    tidy(d);
    const links = [...d.querySelectorAll('a[href*="/store/apps/details?id="]')].filter(id);
    const apps = [], seen = new Set();
    for (const a of links) {
      const pkg = id(a);
      if (seen.has(pkg)) continue;
      seen.add(pkg);
      let box = a;
      while (box.parentElement && box.parentElement !== d.body
             && [...box.parentElement.querySelectorAll('a[href*="/store/apps/details?id="]')].every(o => id(o) === pkg))
        box = box.parentElement;
      const texts = leaves(box);
      const title = clean(a.getAttribute('aria-label')) || texts[0] || '';
      const rest = texts.filter(t => t !== title);
      const developer = rest.find(t => !RATING.test(t)) || '';
      const summary = rest.filter(t => t !== developer && t.length >= 40).sort((x, y) => y.length - x.length)[0] || '';
      if (title) apps.push({package: pkg, title, developer, rating: rest.find(t => RATING.test(t)) || '', summary: summary.slice(0, 200)});
      if (apps.length >= limit) break;
    }
    return {apps};
  };

  // An app's own page: its name, developer, rating and what it says it does.
  const page = (d, pkg) => {
    const meta = n => clean((d.querySelector(`meta[itemprop="${n}"], meta[property="og:${n}"], meta[name="${n}"]`) || {}).content);
    const about = d.querySelector('[data-g-id="description"]');
    const text = about ? clean(about.innerHTML.replace(/<br\s*\/?>/gi, '\n').replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&')) : '';
    const title = clean((d.querySelector('h1') || {}).textContent) || meta('title').replace(/ [-–] Apps on Google Play$/, '');
    if (!title) return {error: 'Play showed no app for ' + pkg};
    const dev = d.querySelector('a[href*="/store/apps/dev"]');
    const rating = d.querySelector('[itemprop="starRating"]');
    return {app: {package: pkg, title, developer: clean(dev && dev.textContent),
                  rating: clean(rating && rating.textContent).replace(/[^\d.,]/g, '').slice(0, 3),
                  summary: meta('description').slice(0, 200), about: text.slice(0, 1500)}};
  };

  const one = async q => {
    const pkg = PACKAGE.test(q) ? q : null;
    let r;
    try {
      r = await fetch(pkg ? '/store/apps/details?id=' + encodeURIComponent(pkg) : '/store/search?c=apps&q=' + encodeURIComponent(q),
                      {credentials: 'include'});
    } catch (e) {
      // Play sends a browser it has not seen lately through accounts.google.com and back, which a fetch may not
      // follow. Loading one Play page in the tab passes it (play.py), and Chrome keeps the pass in its cookies.
      return {bounce: true};
    }
    if (pkg && r.status === 404) return {error: 'Play has no app ' + pkg};
    if (!r.ok) return {error: 'Play answered HTTP ' + r.status};
    const d = new DOMParser().parseFromString(trusted(await r.text()), 'text/html');
    return pkg ? page(d, pkg) : results(d);
  };
  return Promise.all(queries.map(q => one(q).catch(e => ({error: String(e && e.message || e).slice(0, 200)}))));
})
