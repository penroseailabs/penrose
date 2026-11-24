// Google's results for each query, read in a hidden google.com tab of the phone's Chrome (gateway/web.py).
// Each search is a same-origin fetch of the results page, parsed without being shown: nothing comes on screen.
// Called as (queries, limit) => [{overview, answer, results, drawn} | {check} | {error}] per query, or as
// (null, limit, true) => [the same for the results page the tab itself shows, scripts run].
(async (queries, limit, live) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').replace(/ ([.,;:!?)])/g, '$1').trim();
  const leaves = el => {
    const out = [];
    const walk = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n; n = walk.nextNode()) {
      const t = n.textContent.trim();
      if (t) out.push(t);
    }
    return out;
  };
  const HEADING = 'a [role=heading], a h3';
  const GOOGLE = /(^|\.)google\.[a-z.]+$/;
  const outside = el => [...el.querySelectorAll('a[href^="http"]')]
    .filter(a => { try { return !GOOGLE.test(new URL(a.href).hostname); } catch (e) { return false; } }).length;
  const UI = /\s*(\bDive deeper in AI Mode|\bShow more|\bShow thread|\bShow all|\bFeedback$)[\s\S]*$/;

  const parse = d => {
    d.querySelectorAll('script,style,noscript,template').forEach(e => e.remove());
    const rso = d.querySelector('#rso');
    if (!rso) return {error: 'the page had no results'};

    // The AI Overview, when Google made one: its text after the "AI Mode replied:" header, without the buttons.
    // The block also carries Google's "not available" placeholder, which is all there is when it made none.
    let overview = null, answer = null, drawn = false;
    for (const block of rso.children) {
      const t = clean(leaves(block).join(' '));
      if (/AI Overview/.test(t)) {
        const at = t.lastIndexOf('replied:');
        const body = at >= 0 ? t.slice(at + 8) : /not available for this search/.test(t) ? '' : t.slice(t.indexOf('AI Overview') + 11);
        if (!overview && body.replace(UI, '').trim()) overview = body.replace(UI, '').trim().slice(0, 1200);
        continue;
      }
      // An answer card before the first result (a time, a sports score, a definition). A block linking to
      // several other sites is a strip of results (short videos, images), not an answer. A card drawn by
      // script (the weather) is only its title in a fetched page: `drawn` asks web.py to read the page shown.
      const card = !block.querySelector(HEADING) && outside(block) <= 1 && t && !/^(People also ask|Videos|Web results)/.test(t);
      if (!answer && card && t.length >= 25) answer = t.replace(UI, '').slice(0, 700);
      else if (!answer && card) drawn = true;
      if (block.querySelector(HEADING)) break;
    }

    const results = [], seen = new Set();
    for (const a of rso.querySelectorAll('a[href]')) {
      const head = a.querySelector('[role=heading], h3');
      if (!head) continue;
      let url;
      try { url = new URL(a.getAttribute('href'), location.origin); } catch (e) { continue; }
      if (!/^https?:$/.test(url.protocol) || GOOGLE.test(url.hostname)) continue;
      url.searchParams.delete('srsltid');  // Google's tracking tag on shopping links
      const key = url.origin + url.pathname;
      if (seen.has(key)) continue;
      seen.add(key);
      const title = clean(head.textContent);
      const own = leaves(a);
      const source = own.map(clean).find(t => t && t !== title && t !== '·' && !/^https?:\/\//.test(t))
        || url.hostname.replace(/^www\./, '');
      // The snippet: the text around this link, up to the box that holds no other result.
      let box = a;
      while (box.parentElement && box.parentElement !== rso && box.parentElement.querySelectorAll(HEADING).length <= 1)
        box = box.parentElement;
      const mine = new Set(own);
      const snippet = clean(leaves(box).filter(t => !mine.has(t) && !/^(Web results|Videos|·)$/.test(t)).join(' '));
      results.push({title, source, url: url.href, snippet: snippet.slice(0, 300)});
      if (results.length >= limit) break;
    }
    return {overview, answer, results, drawn: drawn && !answer};
  };

  if (live) return [parse(document.cloneNode(true))];
  const one = async q => {
    const r = await fetch('/search?q=' + encodeURIComponent(q), {credentials: 'include'});
    if (/\/sorry\//.test(r.url)) return {error: 'Google asked to prove this is not a robot'};
    if (!r.ok) return {error: 'Google answered HTTP ' + r.status};
    const html = await r.text();
    const d = new DOMParser().parseFromString(html, 'text/html');
    // Google's JavaScript check: a page that only runs a script, which a fetched page never does. Loading one
    // results page in the tab passes it (web.py), and Chrome keeps the pass in its cookies for later searches.
    if (!d.querySelector('#rso') && /having trouble accessing Google Search|\/httpservice\/retry\/enablejs/.test(html))
      return {check: true};
    return parse(d);
  };
  return Promise.all(queries.map(q => one(q).catch(e => ({error: String(e && e.message || e).slice(0, 200)}))));
})
