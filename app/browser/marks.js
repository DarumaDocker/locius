// Set-of-marks: draw a labelled box on every element (with a snapshot ref) that is visible in the viewport, so a
// vision model can say "click [e12]". mode 'draw' adds the overlay and returns {marks, text} (text = the words visible
// on screen, so a small model can quote prices exactly); 'clear' removes it.
(mode) => {
  const ID = '__persona_marks__';
  const old = document.getElementById(ID);
  if (old) old.remove();
  if (mode === 'clear') return { marks: [], text: '' };
  const VW = window.innerWidth, VH = window.innerHeight;
  const layer = document.createElement('div');
  layer.id = ID;
  layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
  const marks = [];
  const deepAll = sel => { const out = []; const visit = root => { root.querySelectorAll(sel).forEach(e => out.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) visit(e.shadowRoot); }); }; visit(document); return out; };
  // elementFromPoint stops at a shadow host; follow it down, and compare across shadow boundaries
  const deepPoint = (x, y) => { let t = document.elementFromPoint(x, y);
    while (t && t.shadowRoot) { const i = t.shadowRoot.elementFromPoint(x, y); if (!i || i === t) break; t = i; } return t; };
  const within = (a, b) => { for (let n = b; n; n = n.parentNode || n.host) if (n === a) return true; return false; };
  for (const el of deepAll('[data-persona-ref]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4 || r.bottom < 0 || r.right < 0 || r.top > VH || r.left > VW) continue;
    const cx = Math.min(Math.max(r.left + r.width / 2, 0), VW - 1), cy = Math.min(Math.max(r.top + r.height / 2, 0), VH - 1);
    const top = deepPoint(cx, cy);
    if (top && top !== el && !within(el, top) && !within(top, el)) continue;      // covered by something else
    const ref = el.getAttribute('data-persona-ref');
    const box = document.createElement('div');
    box.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;border:2px solid #e11;box-sizing:border-box;`;
    const tag = document.createElement('div');
    tag.textContent = ref;
    tag.style.cssText = `position:fixed;left:${Math.max(r.left, 0)}px;top:${Math.max(r.top - 14, 0)}px;background:#e11;color:#fff;font:bold 11px/13px monospace;padding:0 3px;`;
    layer.appendChild(box); layer.appendChild(tag);
    const name = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
    const tg = el.tagName.toLowerCase(), ty = (el.type || '').toLowerCase();
    const role = el.getAttribute('role') || ({ a: 'link', button: 'button', select: 'combobox', textarea: 'textbox', summary: 'button' }[tg])
      || (tg === 'input' ? (ty === 'search' ? 'searchbox' : ['checkbox', 'radio'].includes(ty) ? ty : ['button', 'submit', 'image', 'reset'].includes(ty) ? 'button' : 'textbox') : tg);
    marks.push({ ref, role, name: name.slice(0, 80),
                 box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)] });
    if (marks.length >= 120) break;
  }
  // the text a person would read on screen (before the labels are added)
  let text = '';
  const roots = [document.body || document.documentElement, ...deepAll('*').filter(e => e.shadowRoot).map(e => e.shadowRoot)];
  for (const root of roots) {
  const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = tw.nextNode(); n && text.length < 2500; n = tw.nextNode()) {
    const t = n.nodeValue.replace(/\s+/g, ' ').trim();
    const p = n.parentElement;
    if (!t || !p || /^(SCRIPT|STYLE|NOSCRIPT|TEMPLATE)$/.test(p.tagName)) continue;
    const r = p.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || r.bottom < 0 || r.top > VH || r.right < 0 || r.left > VW) continue;
    const cs = getComputedStyle(p);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
    text += (/^(DIV|P|LI|H\d|TR|SECTION|ARTICLE|BUTTON|A)$/.test(p.tagName) ? '\n' : ' ') + t;
  }
  }
  document.documentElement.appendChild(layer);
  return { marks, text: text.replace(/\n\s*\n+/g, '\n').trim().slice(0, 2500) };
}
