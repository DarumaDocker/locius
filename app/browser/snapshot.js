(opts) => {
  // opts: {prefix, near}. near=true -> only what is around the current scroll position (so scrolling shows new content)
  const prefix = typeof opts === 'string' ? opts : ((opts && opts.prefix) || '');
  const near = !!(opts && opts.near);
  const VH = window.innerHeight || 800;
  const ATTR = 'data-persona-ref';
  const lines = [];
  let n = 0;
  // open shadow roots (chat widgets such as Tidio, many web components) are walked too
  const deepAll = sel => { const out = []; const visit = root => { root.querySelectorAll(sel).forEach(e => out.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) visit(e.shadowRoot); }); }; visit(document); return out; };
  // Refs are STABLE while the page lives: an element keeps the ref it got in an earlier snapshot, and only new elements
  // get new numbers. (Renumbering on every snapshot made single-page forms shift their refs after each keystroke, so the
  // agent typed a name into the booking-number box.) Elements that are no longer shown lose their ref.
  const PREV = 'data-persona-prev';
  const KEY = '__personaRefMax_' + (prefix || 'main');
  const used = new Set();
  try { deepAll('[' + ATTR + ']').forEach(e => { e.setAttribute(PREV, e.getAttribute(ATTR)); e.removeAttribute(ATTR); }); } catch (e) {}
  let maxN = window[KEY] || 0;
  const nextRef = el => {
    const old = el.getAttribute(PREV);
    if (old && !used.has(old) && old.startsWith(prefix + 'e')) { used.add(old); return old; }
    let r; do { r = prefix + 'e' + (++maxN); } while (used.has(r));
    used.add(r);
    return r;
  };
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const cut = (s, k) => { s = clean(s); return s.length > k ? s.slice(0, k) + '…' : s; };
  const SR_ONLY = /(^|\s)(a-offscreen|sr-only|visually-?hidden|visuallyhidden|screen-reader-text|screenreader-only|offscreen)(\s|$)/i;
  const SKIP = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'CANVAS', 'IFRAME', 'OBJECT', 'EMBED', 'HEAD', 'META', 'LINK']);
  const visible = el => {
    try {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) return false;
      const s = getComputedStyle(el);
      return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
    } catch (e) { return false; }
  };
  const INTERACTIVE = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],' +
    '[role=menuitem],[role=option],[role=switch],[role=combobox],[role=textbox],[role=searchbox],[role=slider],[contenteditable=""],[contenteditable=true]';
  const isInteractive = el => { try { return el.matches(INTERACTIVE); } catch (e) { return false; } };
  const roleOf = el => {
    const r = el.getAttribute('role');
    if (r) return r;
    const t = el.tagName;
    if (t === 'A') return 'link';
    if (t === 'BUTTON' || t === 'SUMMARY') return 'button';
    if (t === 'SELECT') return 'combobox';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'INPUT') {
      const ty = (el.type || 'text').toLowerCase();
      if (['button', 'submit', 'reset', 'image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      if (ty === 'search') return 'searchbox';
      if (ty === 'range') return 'slider';
      if (ty === 'file') return 'filechooser';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return t.toLowerCase();
  };
  const nameOf = el => {
    let s = el.getAttribute('aria-label');
    if (s) return s;
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(/\s+/).map(id => { const x = document.getElementById(id); return x ? x.innerText : ''; }).join(' '); if (clean(t)) return t; }
    if (el.labels && el.labels.length) { const t = Array.from(el.labels).map(l => l.innerText).join(' '); if (clean(t)) return t; }
    if (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes((el.type || '').toLowerCase())) return el.value || el.type;
    if (el.tagName === 'IMG') return el.alt;
    const inner = el.innerText || '';
    if (clean(inner)) return inner;
    const img = el.querySelector && el.querySelector('img[alt]');
    if (img && img.alt) return img.alt;
    return el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || '';
  };
  const sensitive = el => {
    const ty = (el.type || '').toLowerCase();
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    const nm = ((el.name || '') + ' ' + (el.id || '')).toLowerCase();
    return ty === 'password' || ac.startsWith('cc-') || ac.includes('password') || ac === 'one-time-code' || /(card.?num|cvv|cvc|ssn|passw|otp)/.test(nm);
  };
  let buf = '';
  // prices drawn in pieces (Amazon: "S$ 24 67", others "S$ 24 . 67") -> "S$24.67", so they read (and quote) as one price
  const PRICE_PIECES = /((?:[A-Z]{1,3})?\$|€|£|¥|￥|RM)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\s*\.\s+|\s+\.\s*|\s+)(\d{2})(?![\d.,%])/g;
  const glue = s => (s || '').replace(PRICE_PIECES, '$1$2.$3');
  const flush = () => { const t = glue(clean(buf)); if (t) lines.push('  ' + cut(t, 400)); buf = ''; };
  const emitEl = el => {
    const ref = nextRef(el); n++;
    el.setAttribute(ATTR, ref);
    const role = roleOf(el);
    let line = `[${ref}] ${role} "${cut(glue(clean(nameOf(el))), 90)}"`;
    const ph = el.getAttribute('placeholder');
    if (ph && !clean(nameOf(el)).includes(clean(ph))) line += ` placeholder="${cut(ph, 60)}"`;
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      const ty = (el.type || 'text').toLowerCase();
      if (ty === 'checkbox' || ty === 'radio') line += el.checked ? ' [checked]' : ' [unchecked]';
      else if (ty === 'password') line += ' (password field — agent must NOT type here)';
      else if (ty === 'file') line += ' (file upload)';
      else if (!['button', 'submit', 'reset', 'image'].includes(ty)) line += sensitive(el) ? ' value=[hidden]' : ` value="${cut(el.value, 80)}"`;
      if (ty === 'submit') line += ' (submit)';
    } else if (el.tagName === 'SELECT') {
      const opts = Array.from(el.options).slice(0, 12).map(o => cut(o.text, 30));
      line += ` selected="${cut(el.selectedOptions[0] ? el.selectedOptions[0].text : '', 40)}" options=[${opts.join(' | ')}]`;
    } else if (el.isContentEditable) {
      line += ` value="${cut(el.innerText, 80)}"`;
    }
    if (el.getAttribute('aria-checked')) line += ` [${el.getAttribute('aria-checked') === 'true' ? 'checked' : 'unchecked'}]`;
    if (el.getAttribute('aria-expanded')) line += ` [expanded=${el.getAttribute('aria-expanded')}]`;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') line += ' [disabled]';
    if (el.tagName === 'A') {
      const h = el.getAttribute('href') || '';
      if (h && !h.startsWith('javascript')) {
        let abs = h; try { const u = new URL(h, location.href); abs = u.href; if (abs.length > 140 && u.search) abs = u.origin + u.pathname; } catch (e) {}
        line += ` → ${abs.length > 160 ? abs.slice(0, 160) + '…' : abs}`;
      }
    }
    lines.push(line);
  };
  const walk = node => {
    for (let c = node.firstChild; c; c = c.nextSibling) {
      if (c.nodeType === 3) { buf += ' ' + c.nodeValue; continue; }
      if (c.nodeType !== 1) continue;
      const el = c;
      if (SKIP.has(el.tagName)) continue;
      if (el.getAttribute('aria-hidden') === 'true') continue;
      if (!visible(el) && !['OPTION'].includes(el.tagName)) {
        // invisible containers can still hold visible fixed children; cheap check
        if (el.children.length === 0 && !el.shadowRoot) {
          // screen-reader-only text (Amazon's a-offscreen price, sr-only labels): hidden from the eye, but it IS the page's
          // text — often the only readable copy of a price whose visible pieces are aria-hidden
          if (SR_ONLY.test(typeof el.className === 'string' ? el.className : '') && getComputedStyle(el).display !== 'none') buf += ' ' + el.textContent;
          continue;
        }
        const r = el.getBoundingClientRect();
        if (r.width < 1 && r.height < 1 && getComputedStyle(el).display === 'none') continue;
      }
      if (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'hidden') continue;
      if (near) {
        const r = el.getBoundingClientRect();
        if ((r.width || r.height) && (r.bottom < -150 || r.top > VH + 900)) continue;   // far above/below the viewport
      }
      if (isInteractive(el)) {
        flush();
        emitEl(el);
        continue;
      }
      const block = /^(H[1-6]|P|LI|TR|DIV|SECTION|ARTICLE|HEADER|FOOTER|NAV|MAIN|ASIDE|FORM|TABLE|UL|OL|DL|DT|DD|BLOCKQUOTE|PRE|LABEL|FIELDSET|LEGEND)$/.test(el.tagName);
      if (/^H[1-6]$/.test(el.tagName)) { flush(); const t = clean(el.innerText); if (t) lines.push('#'.repeat(+el.tagName[1]) + ' ' + cut(t, 160)); if (!el.querySelector(INTERACTIVE)) continue; }
      if (block) flush();
      if (el.shadowRoot) walk(el.shadowRoot);
      walk(el);
      if (block) flush();
    }
  };
  try { walk(document.body || document.documentElement); flush(); } catch (e) { lines.push('(snapshot error: ' + e.message + ')'); }
  window[KEY] = maxN;
  try { deepAll('[' + PREV + ']').forEach(e => e.removeAttribute(PREV)); } catch (e) {}
  return lines.join('\n');
}
