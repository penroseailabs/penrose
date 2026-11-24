// relay-bridge page script. Web.java sends it into a WebView with evaluateJavascript; build.py
// embeds it in WebScript.java. It reads the DOM the way Dump.java reads Views (kept nodes,
// handles that survive redraws, roles, state, list rows) and acts on it. Everything it returns
// is plain JSON. It runs in the page's own world so framework-controlled inputs see real events.
//
// The expression evaluates to the API object; installing twice on one page is a no-op.
(function () {
  'use strict';
  var VERSION = '7';
  var top = window;
  try { if (top.__relay && top.__relay.v === VERSION) return top.__relay; } catch (e) { /* fall through */ }

  var EPOCH = Math.random().toString(36).slice(2, 10);  // a new page (reload, navigation) gets a new one
  var MAX_TEXT = 300;
  var BIG = 1e9;
  var SKIP = { script: 1, style: 1, template: 1, noscript: 1, head: 1, meta: 1, link: 1, title: 1, base: 1,
               datalist: 1, option: 1, optgroup: 1, source: 1, track: 1, param: 1, area: 1, map: 1 };
  var HEADING = { h1: 1, h2: 2, h3: 3, h4: 4, h5: 5, h6: 6 };
  var ROLE_ACTIVE = { button: 1, link: 1, checkbox: 1, radio: 1, 'switch': 1, tab: 1, menuitem: 1,
                      menuitemcheckbox: 1, menuitemradio: 1, option: 1, treeitem: 1, combobox: 1,
                      searchbox: 1, textbox: 1, slider: 1, spinbutton: 1, listbox: 0 };
  var ROLE_CHECK = { checkbox: 1, radio: 1, 'switch': 1, menuitemcheckbox: 1, menuitemradio: 1 };
  var ROLE_EDIT = { textbox: 1, searchbox: 1, spinbutton: 1 };
  var LIST_ROLES = { list: 1, listbox: 1, grid: 1, table: 1, tree: 1, menu: 1, menubar: 1, tablist: 1, rowgroup: 0 };
  var ROW_ROLES = { listitem: 1, row: 1, option: 1, treeitem: 1, menuitem: 1, tab: 1, gridcell: 0 };
  var TEXT_INPUTS = { text: 1, search: 1, email: 1, url: 1, tel: 1, password: 1, number: 1, date: 1,
                      time: 1, 'datetime-local': 1, month: 1, week: 1 };
  var BUTTON_INPUTS = { button: 1, submit: 1, reset: 1, image: 1, color: 1 };
  // A password field stays one when "Show password" turns it into a text box: known by its type when it was
  // seen as one, or by its name, id or autofill hint. Its value is never read out.
  var SECRET_HINT = /current-password|new-password|pass(word|wd)|pwd/i;
  var secrets = new WeakSet();
  var NATIVE_CLICK = { a: 1, button: 1, input: 1, select: 1, textarea: 1, summary: 1, label: 1, option: 1 };
  var RECORDED = ['pointerdown', 'pointerup', 'touchstart', 'touchend', 'mousedown', 'mouseup', 'click'];

  // ---- handles ---------------------------------------------------------------------------

  var ids = new WeakMap();
  var els = new Map();
  var nextId = 1;

  function idOf(el) {
    var i = ids.get(el);
    if (!i) { i = nextId++; ids.set(el, i); els.set(i, new WeakRef(el)); }
    return i;
  }

  function byId(i) {
    var r = els.get(i);
    var el = r && r.deref();
    return el && el.isConnected ? el : null;
  }

  function prune() {
    els.forEach(function (r, i) { if (!r.deref()) els.delete(i); });
  }

  // ---- small helpers ---------------------------------------------------------------------

  function collapse(s) { return String(s).replace(/\s+/g, ' ').trim(); }
  function clipText(s, n) { n = n || MAX_TEXT; return s.length > n ? s.slice(0, n) + '…' : s; }
  function attr(el, n) { var v = el.getAttribute(n); return v == null ? '' : collapse(v); }
  function round(r) { return [Math.round(r[0] * 10) / 10, Math.round(r[1] * 10) / 10, Math.round(r[2] * 10) / 10, Math.round(r[3] * 10) / 10]; }

  function inter(a, b) {
    if (!a || !b) return null;
    var l = Math.max(a[0], b[0]), t = Math.max(a[1], b[1]), r = Math.min(a[2], b[2]), bt = Math.min(a[3], b[3]);
    return r > l && bt > t ? [l, t, r, bt] : null;
  }

  function union(a, b) { return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])]; }

  function viewBox() {
    var v = top.visualViewport;
    if (v) return { x: v.offsetLeft, y: v.offsetTop, w: v.width, h: v.height, scale: v.scale };
    return { x: 0, y: 0, w: top.innerWidth, h: top.innerHeight, scale: 1 };
  }

  function composedContains(a, b) {
    for (var n = b; n; n = n.parentNode || n.host) if (n === a) return true;
    return false;
  }

  function childNodesOf(node) {
    if (node.nodeType === 1) {
      if (node.localName === 'slot') {
        var assigned = node.assignedNodes({ flatten: true });
        return Array.prototype.slice.call(assigned.length ? assigned : node.childNodes);
      }
      if (node.shadowRoot) return Array.prototype.slice.call(node.shadowRoot.childNodes);
    }
    return Array.prototype.slice.call(node.childNodes);
  }

  /** Offset of a same-origin frame's viewport inside the top window, in CSS px. */
  function frameOffset(win) {
    var x = 0, y = 0;
    try {
      while (win && win !== top && win.frameElement) {
        var fe = win.frameElement, r = fe.getBoundingClientRect();
        x += r.left + fe.clientLeft;
        y += r.top + fe.clientTop;
        win = win.parent;
      }
    } catch (e) { /* cross-origin parent: stop */ }
    return [x, y];
  }

  function docCtx(el) {
    var doc = el.ownerDocument, off = frameOffset(doc.defaultView);
    return { win: doc.defaultView, doc: doc, ox: off[0], oy: off[1] };
  }

  function absRect(el, d) {
    var r = el.getBoundingClientRect();
    return [r.left + d.ox, r.top + d.oy, r.right + d.ox, r.bottom + d.oy];
  }

  function hitAt(el, x, y, d) {
    var root = el.getRootNode && el.getRootNode();
    if (!root || !root.elementFromPoint) root = d.doc;
    return root.elementFromPoint(x - d.ox, y - d.oy);
  }

  function related(h, el) { return !!h && (h === el || composedContains(el, h) || composedContains(h, el)); }

  /** First point of the visible rect where `el` is what a tap would hit; null when covered. */
  function probePoint(el, vis, d) {
    var w = vis[2] - vis[0], h = vis[3] - vis[1];
    var pts = [[.5, .5], [.25, .25], [.75, .25], [.25, .75], [.75, .75]];
    for (var i = 0; i < pts.length; i++) {
      var x = vis[0] + w * pts[i][0], y = vis[1] + h * pts[i][1];
      if (related(hitAt(el, x, y, d), el)) return [x, y];
    }
    return null;
  }

  /** The page's focused element. `:focus` is false while the WebView itself lacks window focus. */
  function isFocused(el) {
    var root = el.getRootNode && el.getRootNode();
    return !!root && root.activeElement === el;
  }

  function alphaOf(color) {
    if (!color || color === 'transparent') return 0;
    var m = /rgba?\(([^)]+)\)/.exec(color);
    if (!m) return 1;
    var parts = m[1].split(/[,/ ]+/).filter(Boolean);
    return parts.length > 3 ? parseFloat(parts[3]) : 1;
  }

  /** Does `h`, found on top of some text, actually hide it? A modal or an opaque positioned layer
   * (backdrop, cookie bar) does; a transparent click overlay (a "stretched link") does not. */
  function hides(h) {
    var win = h.ownerDocument.defaultView;
    for (var n = h; n && n.nodeType === 1 && n !== h.ownerDocument.body; n = n.parentElement) {
      if (n.localName === 'dialog' || n.getAttribute('aria-modal') === 'true') return true;
      var cs = win.getComputedStyle(n);
      if (cs.position !== 'static' && alphaOf(cs.backgroundColor) >= 0.3) return true;
    }
    return false;
  }

  function isDisabled(el) {
    try { if (el.matches(':disabled')) return true; } catch (e) { /* not a form control */ }
    return attr(el, 'aria-disabled') === 'true';
  }

  // Shown as not available (a greyed-out date, a sold-out size): disabled, a no-entry cursor, or a class
  // saying so (flatpickr-disabled, is-disabled).
  function unavailable(el, cs) {
    if (isDisabled(el) || cs.cursor === 'not-allowed') return true;
    var cl = el.classList;
    for (var i = 0; cl && i < cl.length; i++) if (/(^|[-_])disabled$/i.test(cl[i])) return true;
    return false;
  }

  function stableId(s) {
    if (!s || s.length > 40) return '';
    if (/^[\d:«»r_-]+$/i.test(s) || /[0-9a-f]{8}-[0-9a-f]{4}/i.test(s) || (s.match(/\d/g) || []).length >= 6) return '';
    return s;
  }

  function describeEl(el) {
    if (!el) return 'something';
    var t = collapse(el.getAttribute && (el.getAttribute('aria-label') || '') || '') || collapse((el.textContent || '')).slice(0, 40);
    return el.localName + (el.id ? '#' + el.id : '') + (t ? ' "' + t + '"' : '');
  }

  // ---- naming ----------------------------------------------------------------------------

  function labelsText(el) {
    var out = [];
    var labels = el.labels;
    if (labels) for (var i = 0; i < labels.length; i++) { var t = collapse(labels[i].textContent || ''); if (t) out.push(t); }
    return out.join(' ');
  }

  // The words of the elements an ARIA attribute points at by id (aria-labelledby="a b").
  function idsText(el, name) {
    var ref = attr(el, name);
    if (!ref) return '';
    var root = el.getRootNode(), out = [];
    ref.split(' ').forEach(function (id) {
      var n = root.getElementById ? root.getElementById(id) : null;
      if (n) out.push(collapse(n.textContent || ''));
    });
    return collapse(out.join(' '));
  }

  function labelledBy(el) { return idsText(el, 'aria-labelledby'); }

  // What the page says about a control beyond its name: its help text and, once it was rejected, why
  // (aria-errormessage, or the browser's own validation message).
  function noteOf(el, invalid) {
    var parts = [];
    var err = invalid ? idsText(el, 'aria-errormessage') || collapse(typeof el.validationMessage === 'string' ? el.validationMessage : '') : '';
    if (err) parts.push(err);
    var help = idsText(el, 'aria-describedby');
    if (help && help !== err) parts.push(help);
    return parts.join(' · ');
  }

  // The help and error texts that listed controls say as their note (noteOf), so the page walk leaves them
  // out. Only a shown control's: a dialog's description or a hidden field's help stays where it is.
  function describedEls(doc) {
    var set = new WeakSet();
    var owners = doc.querySelectorAll('[aria-describedby],[aria-errormessage]');
    for (var i = 0; i < owners.length; i++) {
      var el = owners[i];
      if (!el.getClientRects().length || !interactiveInfo(el, doc.defaultView.getComputedStyle(el), el.localName)) continue;
      var invalid = attr(el, 'aria-invalid') === 'true';
      try { invalid = invalid || el.matches(':user-invalid'); } catch (e) { /* old engine */ }
      var refs = attr(el, 'aria-describedby') + (invalid ? ' ' + attr(el, 'aria-errormessage') : '');
      refs.split(' ').forEach(function (id) { var n = id && doc.getElementById(id); if (n) set.add(n); });
    }
    return set;
  }

  // A value read out as words: aria-valuetext, else the number against its maximum.
  function valueText(el, tag) {
    var vt = attr(el, 'aria-valuetext');
    if (vt) return vt;
    var now = attr(el, 'aria-valuenow'), max = attr(el, 'aria-valuemax');
    if (!now && (tag === 'meter' || (tag === 'progress' && el.hasAttribute('value')))) { now = String(el.value); max = String(el.max); }
    if (!now) return '';
    if (max === '1' || max === '100') return Math.round(Number(now) * (max === '1' ? 100 : 1)) + '%';
    return max ? now + ' of ' + max : now;
  }

  function nameOf(el, tag, type) {
    var s = attr(el, 'aria-label') || labelledBy(el);
    if (s) return s;
    if (el.labels && el.labels.length) { s = labelsText(el); if (s) return s; }
    if (tag === 'input' && BUTTON_INPUTS[type]) { s = collapse(el.value || '') || attr(el, 'alt'); if (s) return s; }
    if (tag === 'img') { s = attr(el, 'alt'); if (s) return s; }
    return attr(el, 'title');
  }

  function iconOf(el) {
    var name = '';
    try {
      var u = el.querySelector('use');
      var ref = u && (u.getAttribute('href') || u.getAttribute('xlink:href'));
      if (ref) name = ref.split('#').pop().split('/').pop();
      if (!name) {
        var im = el.querySelector('img[src]');
        if (im && !attr(im, 'alt')) name = (im.getAttribute('src') || '').split(/[?#]/)[0].split('/').pop().replace(/\.[a-z0-9]+$/i, '');
      }
      if (!name) {
        var ic = el.querySelector('i[class], span[class], svg[class]');
        if (ic) {
          var toks = (ic.getAttribute('class') || '').split(/\s+/);
          for (var k = 0; k < toks.length; k++) {
            if (/^(fa|fas|far|fab|fal|mdi|bi|ti|ic|icon|glyphicon|lucide|feather|material-icons|material-symbols)[-_]/.test(toks[k]) &&
                !/^(fa|icon|ic)$/.test(toks[k]) && !/^(fa-(solid|regular|brands|fw|lg|2x|3x))$/.test(toks[k])) { name = toks[k]; break; }
          }
        }
      }
    } catch (e) { /* querySelector on a detached node */ }
    return clipText(name, 40);
  }

  /** For a control that is itself a picture or an icon and has no words (an <img> or <svg> the page listens
   * to): its file name, or its first class name, the one hint the page gives of what it does. */
  function ownHint(el, tag) {
    var name = '';
    if (tag === 'img') name = (el.currentSrc || el.getAttribute('src') || '').split(/[?#]/)[0].split('/').pop().replace(/\.[a-z0-9]+$/i, '');
    if (!name || name.indexOf('data:') === 0) {
      var cls = el.getAttribute('class') || '';
      name = (cls.split(/\s+/).filter(function (t) { return /^[a-z][a-z0-9_-]{2,}$/i.test(t); })[0]) || '';
    }
    return clipText(name, 40);
  }

  // ---- interactivity ---------------------------------------------------------------------

  function isSecret(el, type) {
    if (type === 'password') { secrets.add(el); return true; }
    if (secrets.has(el)) return true;
    return SECRET_HINT.test([attr(el, 'autocomplete'), attr(el, 'name'), el.id || ''].join(' '));
  }

  // What a tap does lives in the page's script, and markup does not always show it: an <img> or <div> the
  // page listens to for clicks (a framework's @click or data-action, addEventListener) with no role, onclick
  // attribute or pointer cursor is a button to the person and plain content to everything above. A host that
  // can ask the browser which elements have listeners (Chrome over DevTools: getEventListeners) hands that
  // question in as dump's `listeners`; without it the reader goes by markup alone, as before.
  var HEARD = ['click', 'mousedown', 'mouseup', 'pointerdown', 'pointerup'];  // not touch alone: a carousel swipes
  var HEARD_AREA = 0.25;   // of the viewport: larger is a backdrop or a whole section, not a control
  var HEARD_INNER = 40;    // elements inside: more is a container that listens for its children
  var HEARD_MAX = 120;     // per page: a script that listens on everything tells nothing

  function listenerProbe(ask) {
    var memo = new WeakMap();
    return function (el) {
      var known = memo.get(el);
      if (known === undefined) {
        known = false;
        try {
          var l = ask(el) || {};
          for (var i = 0; i < HEARD.length; i++) if (l[HEARD[i]] && l[HEARD[i]].length) { known = true; break; }
        } catch (e) { /* not something the browser lists */ }
        memo.set(el, known);
      }
      return known;
    };
  }

  /** The page listens for clicks on `el` itself, and `el` is the thing to tap: no control inside it (a card
   * that listens around its own link or button is that link's container), and not most of the screen. */
  function heardControl(el, C) {
    if (C.heardCount >= HEARD_MAX || !C.heard(el)) return false;
    var r = el.getBoundingClientRect();
    if (r.width * r.height > HEARD_AREA * C.vb.w * C.vb.h) return false;
    var inner = el.getElementsByTagName('*');
    if (inner.length > HEARD_INNER) return false;
    var win = el.ownerDocument.defaultView;
    for (var i = 0; i < inner.length; i++) {
      var k = inner[i];
      if (C.heard(k) || interactiveInfo(k, win.getComputedStyle(k), k.localName, null)) return false;
    }
    C.heardCount++;
    return true;
  }

  function interactiveInfo(el, cs, tag, C) {
    var role = attr(el, 'role').split(' ')[0].toLowerCase();
    var type = tag === 'input' ? String(el.type || 'text').toLowerCase() : '';
    if (tag === 'a') {
      if (el.hasAttribute('href') || ROLE_ACTIVE[role]) return { role: role && ROLE_ACTIVE[role] ? role : 'link' };
    } else if (tag === 'button') {
      return { role: role && ROLE_ACTIVE[role] ? role : 'button', check: !!ROLE_CHECK[role] };
    } else if (tag === 'summary') {
      return { role: 'button' };
    } else if (tag === 'input') {
      if (type === 'hidden') return null;
      if (type === 'checkbox' || type === 'radio') return { role: type, check: true };
      if (BUTTON_INPUTS[type]) return { role: 'button' };
      if (type === 'range') return { role: 'slider', edit: true };
      if (type === 'file') return { role: 'file' };
      return { role: 'textbox', edit: true, password: isSecret(el, type), type: type };
    } else if (tag === 'textarea') {
      return { role: 'textbox', edit: true };
    } else if (tag === 'select') {
      return { role: 'combobox', select: true };
    } else if (tag === 'label') {
      var c = el.control;
      if (c && c.localName === 'input' && (c.type === 'checkbox' || c.type === 'radio') && controlHidden(c)) {
        return { role: c.type, check: true, control: c };
      }
    }
    var ce = el.getAttribute && el.getAttribute('contenteditable');
    if (ce !== null && ce !== 'false' && el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) {
      return { role: 'textbox', edit: true, rich: true };
    }
    if (role && ROLE_ACTIVE[role]) return { role: role, check: !!ROLE_CHECK[role], edit: !!ROLE_EDIT[role] };
    if (el.hasAttribute('onclick') || typeof el.onclick === 'function') return { role: 'button', generic: true };
    if (cs.cursor === 'pointer' && tag !== 'html' && tag !== 'body') {
      var p = el.parentElement;
      var pointerParent = p && el.ownerDocument.defaultView.getComputedStyle(p).cursor === 'pointer';
      if (!pointerParent) return { role: 'button', generic: true };
    }
    var ti = el.getAttribute && el.getAttribute('tabindex');
    if (ti !== null && Number(ti) >= 0 && (attr(el, 'aria-label') || collapse(el.textContent || ''))) return { role: 'button', generic: true };
    if (C && C.heard && tag !== 'html' && tag !== 'body' && heardControl(el, C)) return { role: 'button', generic: true, heard: true };
    return null;
  }

  function controlHidden(c) {
    var cs = c.ownerDocument.defaultView.getComputedStyle(c);
    var r = c.getBoundingClientRect();
    return parseFloat(cs.opacity) < 0.01 || r.width < 2 || r.height < 2 || cs.visibility !== 'visible';
  }

  // ---- the dump --------------------------------------------------------------------------

  function Ctx(opts) {
    var vb = viewBox();
    this.vb = vb;
    this.view = [vb.x, vb.y, vb.x + vb.w, vb.y + vb.h];
    this.page = opts.scope === 'page';
    this.lim = this.page ? [-BIG, -BIG, BIG, BIG] : this.view;
    this.count = 0;
    this.max = opts.max || (this.page ? 2500 : 900);
    this.deadline = Date.now() + 1800;
    this.truncated = false;
    this.frames = 0;
    this.blocked = [];
    this.rows = new WeakMap();
    this.live = null;  // 'alert' or 'status' while inside a region the page announces
    this.described = describedEls(document);
    this.heard = typeof opts.listeners === 'function' ? listenerProbe(opts.listeners) : null;
    this.heardCount = 0;
    this.stats ={ text_runs: 0, no_rect: 0, outside: 0, covered_text: 0, covered_controls: 0 };
  }

  function newRun() { return { parts: [], rects: [], owner: null, label: null, role: null, level: 0 }; }

  function textRect(tn, d) {
    var rg = d.doc.createRange();
    rg.selectNodeContents(tn);
    var rs = rg.getClientRects(), out = null;
    for (var i = 0; i < rs.length; i++) {
      if (rs[i].width <= 0 || rs[i].height <= 0) continue;
      var r = [rs[i].left + d.ox, rs[i].top + d.oy, rs[i].right + d.ox, rs[i].bottom + d.oy];
      out = out ? union(out, r) : r;
    }
    return out;
  }

  function addText(tn, C, d, clip, S) {
    var raw = tn.nodeValue;
    if (!raw || S.noText) return;
    if (!/\S/.test(raw)) {
      var sink = S.label || (S.parts.length ? S.parts : null);
      if (sink) sink.push(' ');
      return;
    }
    var t = raw.replace(/\s+/g, ' ');
    if (S.label) { S.label.push(t); return; }
    if (!clip) return;
    var r = textRect(tn, d);
    var eff = inter(clip, C.lim);
    var vis = r && eff && inter(r, eff);
    if (!vis) { if (!r) C.stats.no_rect++; else C.stats.outside++; return; }
    if (!S.owner) S.owner = tn.parentElement || (tn.parentNode && tn.parentNode.host) || null;
    S.parts.push(t);
    S.rects.push(vis);
  }

  function flush(S, C, d, out) {
    if (S.label || !S.parts.length) return;
    var text = collapse(S.parts.join(''));
    var rects = S.rects, owner = S.owner;
    S.parts = []; S.rects = []; S.owner = null;
    if (!text || !rects.length) return;
    var b = rects.reduce(union);
    if (owner && inter(b, C.view)) {  // text under a modal backdrop is not what the user sees
      var cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2;
      var top = hitAt(owner, cx, cy, d);
      if (!related(top, owner) && top && hides(top)) { C.stats.covered_text++; return; }
    }
    C.stats.text_runs++;
    var node = { cls: 'web:text', b: round(b), text: clipText(text) };
    if (S.role) { node.wrole = S.role; if (S.level) node.level = S.level; }
    else if (C.live) node.wrole = C.live;
    out.push(node);
    C.count++;
  }

  function walkNodes(nodes, C, d, clip, S, out) {
    for (var k = 0; k < nodes.length; k++) {
      if (C.count >= C.max || Date.now() > C.deadline) { C.truncated = true; return; }
      var n = nodes[k];
      if (n.nodeType === 3) addText(n, C, d, clip, S);
      else if (n.nodeType === 1) visit(n, C, d, clip, S, out);
    }
  }

  function isListContainer(el, tag) {
    return tag === 'ul' || tag === 'ol' || tag === 'menu' || tag === 'table' || !!LIST_ROLES[attr(el, 'role')];
  }

  function rowsOf(el, tag) {
    var rows = [];
    var kids = el.children;
    for (var i = 0; i < kids.length; i++) {
      var k = kids[i], kt = k.localName;
      if (kt === 'li' || kt === 'tr' || ROW_ROLES[attr(k, 'role')]) rows.push(k);
      else if (tag === 'table' && (kt === 'tbody' || kt === 'thead' || kt === 'tfoot')) {
        for (var j = 0; j < k.children.length; j++) if (k.children[j].localName === 'tr') rows.push(k.children[j]);
      }
    }
    return rows;
  }

  function scrollFlags(el, cs, flags) {
    var oy = cs.overflowY, ox = cs.overflowX;
    if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay') && el.scrollHeight > el.clientHeight + 1) {
      if (el.scrollTop + el.clientHeight < el.scrollHeight - 1) flags.push('can_scroll_down');
      if (el.scrollTop > 0) flags.push('can_scroll_up');
    }
    if ((ox === 'auto' || ox === 'scroll' || ox === 'overlay') && el.scrollWidth > el.clientWidth + 1) {
      if (el.scrollLeft + el.clientWidth < el.scrollWidth - 1) flags.push('can_scroll_right');
      if (el.scrollLeft > 0) flags.push('can_scroll_left');
    }
  }

  function visit(el, C, d, clip, S, out) {
    if (!clip) return;
    var tag = el.localName;
    if (SKIP[tag]) return;
    if (el.inert) return;  // the browser lets no one reach it: nothing to read or act on
    var cs = d.win.getComputedStyle(el);
    var disp = cs.display;
    if (disp === 'none') return;
    if (disp === 'contents') { walkNodes(childNodesOf(el), C, d, clip, S, out); return; }
    if (parseFloat(cs.opacity) < 0.01) return;
    var rect = el.getBoundingClientRect();
    var abs = [rect.left + d.ox, rect.top + d.oy, rect.right + d.ox, rect.bottom + d.oy];
    var sized = rect.width > 0 && rect.height > 0;
    var myClip = cs.position === 'fixed' ? C.view : clip;
    var eff = inter(myClip, C.lim);
    if (sized && (!eff || !inter(abs, eff))) return;  // outside what the user can see
    var shown = cs.visibility === 'visible';
    var childClip = myClip;
    if (tag !== 'html' && tag !== 'body' && (cs.overflowX !== 'visible' || cs.overflowY !== 'visible')) {
      childClip = inter(myClip, abs);
    }
    var vis = sized && eff ? inter(abs, eff) : null;
    var inLabel = !!S.label;

    if (shown && vis) {
      var info = interactiveInfo(el, cs, tag, C);
      if (info) {
        if (!inLabel) flush(S, C, d, out);
        else S.label.push(' ');
        var node = buildInteractive(el, cs, info, tag, vis, childClip, C, d);
        if (node) { out.push(node); C.count++; return; }
      }
    }

    // A progress bar or meter reads as its value; one with no value yet is still loading. A div that only
    // claims the role without a value keeps its own words (a "Step 2 of 4" strip).
    var ownRole = attr(el, 'role');
    if (tag === 'progress' || tag === 'meter' || ownRole === 'progressbar' || ownRole === 'meter') {
      var amount = valueText(el, tag);
      if (amount || tag === 'progress' || tag === 'meter') {
        if (shown && vis && !inLabel) {
          flush(S, C, d, out);
          var what = attr(el, 'aria-label') || labelledBy(el) || labelsText(el);
          out.push({ cls: 'web:' + tag, wrole: 'progress', b: round(vis), text: clipText((what ? what + ': ' : '') + (amount || 'in progress')) });
          C.count++;
        }
        return;
      }
    }

    // A control's help or error text is said with the control (note=), as a label is: not again here.
    if (C.described.has(el) && !inLabel) {
      flush(S, C, d, out);
      var S5 = newRun();
      S5.noText = true;
      walkNodes(childNodesOf(el), C, d, childClip, S5, out);
      return;
    }

    // A block of words with an accessible name reads as that name (aria-label: what a screen reader says,
    // "October 29, 2026" for a day drawn as "29"); one shown as not available is marked so.
    if (shown && vis && !inLabel && disp !== 'inline' && !el.firstElementChild && /\S/.test(el.textContent || '')) {
      var named = attr(el, 'aria-label'), off = unavailable(el, cs);
      if (named || off) {
        flush(S, C, d, out);
        var S4 = newRun(), before = out.length;
        S4.parts.push(named || el.textContent); S4.rects.push(vis); S4.owner = el;
        flush(S4, C, d, out);
        if (off && out.length > before) out[out.length - 1].f = ['disabled'];
        return;
      }
    }

    if (tag === 'iframe' || tag === 'frame') {
      if (!inLabel) flush(S, C, d, out);
      frameNode(el, C, d, abs, vis, myClip, out);
      return;
    }
    if (tag === 'img') {
      var alt = attr(el, 'alt');
      if (vis && inLabel && alt) S.label.push(' ' + alt + ' ');
      var big = rect.width >= 48 && rect.height >= 48;
      if (vis && (inLabel || !alt ? big : rect.width >= 16 && rect.height >= 16)) {
        // A picture is a node of its own, also without words and also inside a link or button (a product card's
        // photo): it says nothing on screen, but it has a handle, so the host can ask for a picture of it for the
        // user (capture). Inside a label the alt text is already the label's.
        if (!inLabel) flush(S, C, d, out);
        var img = { cls: 'web:img', wrole: 'img', b: round(vis), i: idOf(el) };
        if (alt && !inLabel) img.desc = clipText(alt);
        else if (!inLabel) {  // no words of its own: its file name is the one hint the page gives of what it shows
          var file = (el.currentSrc || el.src || '').split('#')[0].split('?')[0].split('/').pop();
          if (file && file.indexOf('data:') !== 0 && file.length <= 80) img.file = file;
        }
        out.push(img);
        C.count++;
      }
      return;
    }
    if (tag === 'svg' || tag === 'math') {
      var label = attr(el, 'aria-label');
      if (!label) { var tt = el.querySelector && el.querySelector('title'); label = tt ? collapse(tt.textContent || '') : ''; }
      if (label && inLabel) S.label.push(' ' + label + ' ');
      return;
    }
    if (tag === 'canvas' || tag === 'video') {
      if (vis && !inLabel && rect.width >= 40 && rect.height >= 40) {
        flush(S, C, d, out);
        out.push({ cls: 'web:' + tag, wrole: tag, b: round(vis), desc: attr(el, 'aria-label'), f: ['opaque'] });
        C.count++;
      }
      return;
    }
    if (tag === 'br') { if (inLabel) S.label.push(' '); else if (S.parts.length) S.parts.push(' '); return; }

    var block = disp !== 'inline';
    var role = attr(el, 'role');

    if (tag === 'label' && !inLabel && el.control && !attr(el.control, 'aria-label') && !attr(el.control, 'aria-labelledby')) {
      // The control carries this label as its name; the words would only repeat it.
      flush(S, C, d, out);
      var S3 = newRun();
      S3.noText = true;
      walkNodes(childNodesOf(el), C, d, childClip, S3, out);
      return;
    }

    if (tag === 'time') {  // "Tomorrow" or "Thu" with the exact date the page means beside it
      var when = attr(el, 'datetime'), said = collapse(el.textContent || '');
      if (when && said && said.indexOf(when) < 0) {
        walkNodes(childNodesOf(el), C, d, childClip, S, out);
        var sink = S.label || (S.parts.length ? S.parts : null);
        if (sink) sink.push(' (' + when + ')');
        return;
      }
    }

    if (inLabel) {  // inside an interactive element: its words belong to its label
      if (block) S.label.push(' ');
      walkNodes(childNodesOf(el), C, d, childClip, S, out);
      if (block) S.label.push(' ');
      return;
    }

    if (!block && !HEADING[tag]) {  // inline text keeps flowing into the surrounding run
      walkNodes(childNodesOf(el), C, d, childClip, S, out);
      return;
    }

    flush(S, C, d, out);
    var S2 = newRun();
    var level = HEADING[tag] || (role === 'heading' ? Number(attr(el, 'aria-level')) || 2 : 0);
    if (level) { S2.role = 'heading'; S2.level = level; }
    var rows = null;
    if (isListContainer(el, tag)) {
      rows = rowsOf(el, tag);
      for (var i = 0; i < rows.length; i++) C.rows.set(rows[i], i);
    }
    // What the page announces (role=alert or status, an assertive live region): its words, however deep,
    // are marked so, like a heading's.
    var outerLive = C.live;
    if (role === 'alert' || attr(el, 'aria-live') === 'assertive') C.live = 'alert';
    else if (role === 'status' && !C.live) C.live = 'status';
    var kids = [];
    walkNodes(childNodesOf(el), C, d, childClip, S2, kids);
    flush(S2, C, d, kids);
    C.live = outerLive;

    var flags = [], props = null;
    if (attr(el, 'aria-busy') === 'true') flags.push('loading');  // a mark to read, never something to wait on
    var row = C.rows.get(el);
    if (row !== undefined) { props = props || {}; props.li = row; }
    if (rows) { flags.push('list'); props = props || {}; props.items = rows.length; }
    if (el !== d.doc.body && el !== d.doc.documentElement) scrollFlags(el, cs, flags);
    var dialog = tag === 'dialog' || role === 'dialog' || role === 'alertdialog';
    if (dialog) { props = props || {}; props.wrole = 'dialog'; var nm = attr(el, 'aria-label') || labelledBy(el); if (nm) props.desc = clipText(nm); flags.push('modal'); }
    if (flags.length || props || kids.length >= 2) {
      var g = { cls: 'web:' + tag, b: round(vis || abs), c: kids };
      if (props) for (var key in props) g[key] = props[key];
      if (flags.length) g.f = flags;
      for (var fi = 0; fi < flags.length; fi++) {
        if (flags[fi].indexOf('can_scroll_') === 0) { g.i = idOf(el); break; }  // a scroll area is acted on
      }
      out.push(g);
      C.count++;
    } else {
      for (var m = 0; m < kids.length; m++) out.push(kids[m]);
    }
  }

  function frameNode(el, C, d, abs, vis, clip, out) {
    var doc = null;
    try { doc = el.contentDocument; } catch (e) { /* cross-origin */ }
    C.frames++;
    var title = attr(el, 'title') || attr(el, 'name');
    if (doc && doc.documentElement && doc.defaultView) {
      var d2 = { win: doc.defaultView, doc: doc, ox: abs[0] + el.clientLeft, oy: abs[1] + el.clientTop };
      var S2 = newRun(), kids = [];
      walkNodes([doc.body || doc.documentElement], C, d2, inter(clip, abs), S2, kids);
      flush(S2, C, d2, kids);
      if (kids.length) {
        var n = { cls: 'web:frame', wrole: 'frame', b: round(vis || abs), c: kids };
        if (title) n.desc = title;
        out.push(n);
        C.count++;
      }
      return;
    }
    var src = el.getAttribute('src') || '';
    var host = '', href = '';
    try { var u = new URL(src, d.doc.baseURI); host = u.host; href = u.href; } catch (e) { /* data: and about: have no host */ }
    C.blocked.push(host || (src.split(':')[0] + ':'));
    if (vis) {
      // Its handle, URL and the origin of its content box (top-window CSS px): a host that can run the script
      // inside the frame (Chrome over DevTools) hangs the frame's own nodes here, placed by `fo`.
      out.push({ cls: 'web:frame', wrole: 'frame', b: round(vis), desc: title || host || 'frame', f: ['blocked_frame'],
                 i: idOf(el), src: href, fo: [Math.round(abs[0] + el.clientLeft), Math.round(abs[1] + el.clientTop)] });
      C.count++;
    }
  }

  /** Where a frame element's content box is now, in top-window CSS px (a frame's own coordinates are offset by it). */
  function frameOrigin(el) {
    var r = absRect(el, docCtx(el));
    return { ok: true, fo: [Math.round(r[0] + el.clientLeft), Math.round(r[1] + el.clientTop)] };
  }

  function buildInteractive(el, cs, info, tag, vis, childClip, C, d) {
    var ctl = info.control || el;  // a label standing in for a hidden checkbox
    var type = tag === 'input' ? String(el.type || 'text').toLowerCase() : '';
    var onScreen = !!inter(vis, C.view);
    var covered = false;
    if (onScreen) {
      covered = !probePoint(el, vis, d);
      if (covered) C.stats.covered_controls++;
    }
    var node = { cls: 'web:' + tag, wrole: info.role, b: round(vis), i: idOf(ctl) };
    var id = stableId(ctl.id) || stableId(attr(ctl, 'data-testid') || attr(ctl, 'data-test') || attr(ctl, 'data-test-id') || attr(ctl, 'data-qa') || attr(ctl, 'data-cy'));
    if (id) node.id = id;
    var nm = attr(ctl, 'name');
    if (nm && (tag === 'input' || tag === 'select' || tag === 'textarea' || info.control)) node.name = clipText(nm, 40);
    if (info.type && info.type !== 'text') node.type = info.type;

    var flags = ['click'];
    var kids = [];
    var text = '';
    if (info.edit && !info.rich && ctl.value !== undefined) {
      text = info.password ? (ctl.value ? '[redacted]' : '') : collapse(ctl.value);
    } else if (info.select) {
      var chosen = [], choices = [];
      for (var i = 0; i < el.options.length; i++) {
        var o = el.options[i], ot = collapse(o.text || o.label || '');
        if (choices.length < 40) choices.push(ot);
        if (o.selected) chosen.push(ot);
      }
      text = chosen.join(', ');
      node.choices = choices;
      flags = ['select'];
    } else if (tag === 'input' && BUTTON_INPUTS[type]) {
      text = collapse(el.value || '');
    } else if (info.rich) {
      var Sr = { label: [], parts: [], rects: [], owner: null };
      walkNodes(childNodesOf(el), C, d, childClip, Sr, kids);
      text = collapse(Sr.label.join(''));
      kids = [];
    } else {
      var S = { label: [], parts: [], rects: [], owner: null };
      walkNodes(childNodesOf(el), C, d, childClip, S, kids);
      text = collapse(S.label.join(''));
      if (!text && el.shadowRoot) text = collapse(el.shadowRoot.textContent || '');
    }
    if (text) node.text = clipText(text);
    var name = nameOf(ctl, ctl.localName, type) || (info.control ? collapse(el.textContent || '') : '');
    if (name && name.toLowerCase() !== String(node.text || '').toLowerCase()) {  // tooltips often just repeat the text
      if (info.control && !node.text) node.text = clipText(name); else node.desc = clipText(name);
    }
    var ph = attr(ctl, 'placeholder');
    if (ph) node.hint = clipText(ph);
    if (tag === 'a' || tag === 'area') {
      try {
        var u = new URL(el.href, d.doc.baseURI);
        var shown = (u.origin !== d.win.location.origin ? u.origin : '') + u.pathname + u.search;
        node.href = shown.length > 100 ? shown.slice(0, 100) + '…' : shown;
        if (u.protocol === 'javascript:') node.href = 'javascript:';
      } catch (e) { /* unparsable href */ }
    }
    var icon = (tag === 'input' ? '' : iconOf(el));
    if (!icon && info.heard && !node.text && !node.desc) icon = ownHint(el, tag);
    if (icon) node.icon = icon;

    if (info.edit) flags.push('edit');
    if (info.password) flags.push('password');
    if (info.check || ROLE_CHECK[info.role]) {
      flags.push('checkable');
      if (ctl.checked === true || attr(ctl, 'aria-checked') === 'true') flags.push('checked');
    }
    if (attr(ctl, 'aria-selected') === 'true' || (tag === 'option' && el.selected)) flags.push('selected');
    if (isDisabled(ctl)) flags.push('disabled');
    if (isFocused(ctl)) flags.push('focused');
    if (ctl.required || attr(ctl, 'aria-required') === 'true') flags.push('required');
    if (ctl.readOnly || attr(ctl, 'aria-readonly') === 'true') flags.push('readonly');
    var invalid = attr(ctl, 'aria-invalid') === 'true';
    try { invalid = invalid || ctl.matches(':user-invalid'); } catch (e) { /* old engine */ }
    if (invalid) flags.push('invalid');
    var note = noteOf(ctl, invalid);
    if (note && note !== node.desc && note !== node.hint) node.note = clipText(note);
    if (attr(ctl, 'aria-busy') === 'true') flags.push('loading');
    if (covered) flags.push('covered');
    if (!onScreen) flags.push('offscreen');
    scrollFlags(el, cs, flags);
    var states = [];
    if (!node.text && (info.role === 'slider' || info.role === 'spinbutton')) {  // a div slider: its value in words
      var amount = valueText(ctl, tag);
      if (amount) states.push(amount);
    }
    var ex = attr(ctl, 'aria-expanded');
    if (ex) states.push(ex === 'true' ? 'expanded' : 'collapsed');
    if (attr(ctl, 'aria-pressed') === 'true') states.push('pressed');
    var cur = attr(ctl, 'aria-current');
    if (cur && cur !== 'false') states.push('current');
    var pop = attr(ctl, 'aria-haspopup');
    if (pop && pop !== 'false') states.push('opens ' + (pop === 'true' ? 'menu' : pop));
    if (states.length) node.state = states.join(', ');
    var row = C.rows.get(el);
    if (row !== undefined) node.li = row;
    node.f = flags;
    if (kids.length) node.c = kids;
    return node;
  }

  function dump(opts) {
    try {
      opts = opts || {};
      prune();
      var t0 = Date.now();
      var C = new Ctx(opts);
      var out = [];
      var body = document.body;
      if (body) {
        var d0 = { win: top, doc: document, ox: 0, oy: 0 };
        var S = newRun();
        walkNodes([body], C, d0, [-BIG, -BIG, BIG, BIG], S, out);
        flush(S, C, d0, out);
      }
      var se = document.scrollingElement || document.documentElement;
      var vb = C.vb;
      return {
        epoch: EPOCH, url: location.href, title: document.title, ready: document.readyState,
        vv: { x: vb.x, y: vb.y, w: vb.w, h: vb.h, scale: vb.scale }, dpr: top.devicePixelRatio,
        scroll: { x: se.scrollLeft, y: se.scrollTop, w: se.scrollWidth, h: se.scrollHeight },
        nodes: out, count: C.count, truncated: C.truncated,
        frames: { n: C.frames, blocked: C.blocked }, stats: C.stats, heard: C.heardCount, ms: Date.now() - t0
      };
    } catch (e) {
      return { error: 'js: ' + (e && e.message || e), stack: String(e && e.stack || '').slice(0, 400) };
    }
  }

  function pageText(limit) {
    try {
      return { epoch: EPOCH, url: location.href, title: document.title,
               text: clipText(document.body ? document.body.innerText : '', limit || 20000) };
    } catch (e) {
      return { error: 'js: ' + (e && e.message || e) };
    }
  }

  // ---- acting ----------------------------------------------------------------------------

  var rec = null;

  function stopRecording() {
    if (!rec) return null;
    clearTimeout(rec.timer);
    RECORDED.forEach(function (t) { rec.doc.removeEventListener(t, rec.fn, true); });
    var seen = rec.seen;
    if (rec.at) seen.at = rec.at;
    rec = null;
    return seen;
  }

  function startRecording(doc) {
    stopRecording();
    var seen = {};
    var fn = function (e) {
      if (!e.isTrusted) return;
      seen[e.type] = true;
      // When the page got the touch, on the device's clock (ms since 1970): the host tells by it which
      // requests the page sent in answer.
      if (rec && !rec.at) rec.at = performance.timeOrigin + e.timeStamp;
    };
    RECORDED.forEach(function (t) { doc.addEventListener(t, fn, true); });
    rec = { doc: doc, fn: fn, seen: seen, at: 0, timer: setTimeout(stopRecording, 4000) };
  }

  /** The element whose box a person would tap: a hidden checkbox is tapped through its label. */
  function geom(el) {
    var r = el.getBoundingClientRect();
    if ((r.width < 2 || r.height < 2 || parseFloat(el.ownerDocument.defaultView.getComputedStyle(el).opacity) < 0.01) && el.labels && el.labels.length) {
      for (var i = 0; i < el.labels.length; i++) {
        var lr = el.labels[i].getBoundingClientRect();
        if (lr.width >= 2 && lr.height >= 2) return el.labels[i];
      }
    }
    return el;
  }

  function prepare(el) {
    if (isDisabled(el)) return { ok: false, error: 'disabled' };
    var g = geom(el), d = docCtx(g), scrolled = false;
    var vb = viewBox(), view = [vb.x, vb.y, vb.x + vb.w, vb.y + vb.h];
    var r = absRect(g, d);
    if (r[2] - r[0] < 1 || r[3] - r[1] < 1) return { ok: false, error: 'element has no size on the page (hidden)' };
    var cx = (r[0] + r[2]) / 2, cy = (r[1] + r[3]) / 2;
    if (cx < view[0] || cx > view[2] || cy < view[1] || cy > view[3]) {
      try { g.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) { g.scrollIntoView(true); }
      scrolled = true;
      d = docCtx(g);
      r = absRect(g, d);
      vb = viewBox();
      view = [vb.x, vb.y, vb.x + vb.w, vb.y + vb.h];
    }
    var vis = inter(r, view);
    if (!vis) return { ok: false, error: 'not visible even after scrolling it into view' };
    var pt = probePoint(g, vis, d);
    if (!pt) {
      var h = hitAt(g, (vis[0] + vis[2]) / 2, (vis[1] + vis[3]) / 2, d);
      return { ok: false, covered: true, error: 'covered by ' + describeEl(h) };
    }
    startRecording(d.doc);
    return { ok: true, x: pt[0], y: pt[1], scrolled: scrolled, vv: { x: vb.x, y: vb.y, w: vb.w, h: vb.h } };
  }

  function delivered() {
    var seen = stopRecording();
    var at = seen && seen.at || 0;
    if (seen) delete seen.at;
    var keys = seen ? Object.keys(seen) : [];
    return { ok: true, delivered: keys.length > 0, click: !!(seen && seen.click), seen: keys, at: at };
  }

  function fire(win, el, type, x, y, buttons) {
    var Ev = type.indexOf('pointer') === 0 && win.PointerEvent ? win.PointerEvent : win.MouseEvent;
    el.dispatchEvent(new Ev(type, { bubbles: true, cancelable: true, composed: true, view: win, clientX: x, clientY: y,
                                    button: 0, buttons: buttons, pointerType: 'mouse', isPrimary: true }));
  }

  function jsClick(el) {
    if (isDisabled(el)) return { ok: false, error: 'disabled' };
    var win = el.ownerDocument.defaultView, tag = el.localName;
    try { el.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' }); } catch (e) { /* detached */ }
    var r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (!NATIVE_CLICK[tag]) {  // custom controls often listen for pointer/mouse down, not click
      fire(win, el, 'pointerdown', x, y, 1); fire(win, el, 'mousedown', x, y, 1);
      fire(win, el, 'pointerup', x, y, 0); fire(win, el, 'mouseup', x, y, 0);
    }
    try { if (typeof el.focus === 'function') el.focus({ preventScroll: true }); } catch (e) { /* not focusable */ }
    if (typeof el.click === 'function') el.click(); else fire(win, el, 'click', x, y, 0);
    return { ok: true, via: 'web_js_click' };
  }

  function setValue(el, text) {
    var win = el.ownerDocument.defaultView;
    if (el.isContentEditable && el.localName !== 'input' && el.localName !== 'textarea') {
      el.focus();
      var sel = win.getSelection(), range = el.ownerDocument.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges(); sel.addRange(range);
      var done = false;
      try { done = el.ownerDocument.execCommand('insertText', false, text); } catch (e) { /* disabled */ }
      if (!done) {
        el.textContent = text;
        el.dispatchEvent(new win.InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: text }));
      }
      return collapse(el.textContent || '');
    }
    // The prototype's setter, not the instance's: frameworks (React) shadow `value` on the node
    // to track it, and only a write that bypasses the tracker is seen as a change.
    var proto = Object.getPrototypeOf(el), desc = null;
    while (proto && !desc) { desc = Object.getOwnPropertyDescriptor(proto, 'value'); proto = Object.getPrototypeOf(proto); }
    el.focus();
    if (desc && desc.set) desc.set.call(el, text); else el.value = text;
    el.dispatchEvent(new win.InputEvent('input', { bubbles: true, composed: true, inputType: 'insertText', data: text }));
    el.dispatchEvent(new win.Event('change', { bubbles: true }));
    try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) { /* type without selection */ }
    return el.localName === 'input' && isSecret(el, String(el.type || 'text').toLowerCase()) ? '[redacted]' : el.value;
  }

  function choose(el, a) {
    if (el.localName !== 'select') return { ok: false, error: 'not a select' };
    var want = a.text != null ? collapse(a.text).toLowerCase() : null, index = -1;
    if (a.index != null) index = a.index;
    for (var i = 0; index < 0 && i < el.options.length; i++) {
      var o = el.options[i];
      if ((want !== null && (collapse(o.text).toLowerCase() === want || o.value.toLowerCase() === want))) index = i;
    }
    if (index < 0 || index >= el.options.length) return { ok: false, error: 'no such option' };
    var win = el.ownerDocument.defaultView;
    if (el.multiple) el.options[index].selected = !el.options[index].selected; else el.selectedIndex = index;
    el.dispatchEvent(new win.Event('input', { bubbles: true }));
    el.dispatchEvent(new win.Event('change', { bubbles: true }));
    var now = [];
    for (var k = 0; k < el.options.length; k++) if (el.options[k].selected) now.push(collapse(el.options[k].text));
    return { ok: true, via: 'web_select', now: now.join(', ') };
  }

  function submit(el) {
    var win = el.ownerDocument.defaultView;
    el.focus();
    var init = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
    var go = el.dispatchEvent(new win.KeyboardEvent('keydown', init));
    el.dispatchEvent(new win.KeyboardEvent('keypress', init));
    var via = 'enter key events';
    if (go && el.form && el.localName === 'input') {  // the implicit submission a real Enter performs
      if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
      via += ' + form submit';
    }
    el.dispatchEvent(new win.KeyboardEvent('keyup', init));
    return { ok: true, via: 'web_enter (' + via + ')' };
  }

  function scrollEl(el, dir) {
    var dy = 0, dx = 0, h = el.clientHeight * 0.85, w = el.clientWidth * 0.85;
    if (dir === 'down') dy = h; else if (dir === 'up') dy = -h; else if (dir === 'right') dx = w; else if (dir === 'left') dx = -w;
    var t0 = el.scrollTop, l0 = el.scrollLeft;
    el.scrollBy({ top: dy, left: dx, behavior: 'instant' });
    var moved = el.scrollTop !== t0 || el.scrollLeft !== l0;
    return { ok: moved, via: 'web_scroll', error: moved ? null : 'the container did not move (already at its end)' };
  }

  function scrollPage(dir) {
    try {
      var vb = viewBox(), dy = 0, dx = 0;
      if (dir === 'down') dy = vb.h * 0.85; else if (dir === 'up') dy = -vb.h * 0.85;
      else if (dir === 'right') dx = vb.w * 0.85; else if (dir === 'left') dx = -vb.w * 0.85;
      var y0 = top.pageYOffset, x0 = top.pageXOffset;
      top.scrollBy({ top: dy, left: dx, behavior: 'instant' });
      var moved = top.pageYOffset !== y0 || top.pageXOffset !== x0;
      return { ok: moved, via: 'web_scroll_page', from: y0, to: top.pageYOffset,
               error: moved ? null : 'the page did not move (already at its end, or it scrolls inside an inner container)' };
    } catch (e) {
      return { ok: false, error: 'js: ' + (e && e.message || e) };
    }
  }

  function act(epoch, id, action, a) {
    try {
      if (epoch !== EPOCH) return { ok: false, error: 'stale handle' };
      var el = byId(id);
      if (!el) return { ok: false, error: 'stale handle' };
      a = a || {};
      switch (action) {
        case 'prepare': return prepare(el);
        case 'delivered': return delivered();
        case 'rect': return frameOrigin(el);
        case 'box': {
          // Where the element is drawn now, for a picture of it: CSS px in the viewport (vx, vy) and in the
          // document (x, y), with the viewport's size, so the host can cut it out of a capture of the view
          // or ask DevTools for exactly that clip, bounds and picture from the same instant.
          var r = el.getBoundingClientRect();
          var box = { ok: true, vx: r.left, vy: r.top, x: r.left + window.scrollX, y: r.top + window.scrollY, w: r.width, h: r.height,
                      vw: window.innerWidth, vh: window.innerHeight, dpr: window.devicePixelRatio || 1 };
          // An <img> names its source too: the browser holds the original in its cache, better than any crop.
          if (el.localName === 'img' && (el.currentSrc || el.src)) {
            box.src = el.currentSrc || el.src; box.nw = el.naturalWidth; box.nh = el.naturalHeight;
          } else {
            var own = el.ownerDocument;
            var bg = /url\(["']?([^"')]+)["']?\)/.exec(own.defaultView.getComputedStyle(el).backgroundImage || '');
            if (bg) { try { box.src = new URL(bg[1], own.baseURI).href; } catch (e) { /* unparsable */ } }
          }
          return box;
        }
        case 'click': return jsClick(el);
        case 'set_text':
          if (!(el.localName === 'input' || el.localName === 'textarea' || el.isContentEditable)) return { ok: false, error: 'not a text field' };
          return { ok: true, via: 'web_set_text', now: setValue(el, String(a.text)) };
        case 'select': return choose(el, a);
        case 'focus': el.focus(); return { ok: isFocused(el), via: 'web_focus', text_field: el.localName === 'input' || el.localName === 'textarea' || !!el.isContentEditable };
        case 'submit': return submit(el);
        case 'scroll': return scrollEl(el, a.dir);
        default: return { ok: false, error: 'unknown web action ' + action };
      }
    } catch (e) {
      return { ok: false, error: 'js: ' + (e && e.message || e) };
    }
  }

  var api = { v: VERSION, epoch: EPOCH, dump: dump, text: pageText, act: act, scrollPage: scrollPage };
  try { Object.defineProperty(top, '__relay', { value: api, configurable: true }); } catch (e) { top.__relay = api; }
  return api;
})()
