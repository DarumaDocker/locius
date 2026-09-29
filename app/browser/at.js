// What is under the point (x, y)? Follows open shadow roots and walks up to the nearest clickable element.
// If the point is over an <iframe>, returns {iframe: true, box} so the broker can repeat inside that frame.
([x, y]) => {
  let el = document.elementFromPoint(x, y);
  while (el && el.shadowRoot) { const i = el.shadowRoot.elementFromPoint(x, y); if (!i || i === el) break; el = i; }
  if (!el) return null;
  if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
    const r = el.getBoundingClientRect();
    return { iframe: true, box: [r.left + el.clientLeft, r.top + el.clientTop, r.width, r.height], src: (el.src || '').split('?')[0].slice(0, 160) };
  }
  const CLICK = 'a[href],button,input,select,textarea,summary,label,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],' +
    '[role=menuitem],[role=option],[role=switch],[role=combobox],[role=textbox],[contenteditable=""],[contenteditable=true],[onclick],[tabindex]';
  let hit = el;
  for (let n = el, i = 0; n && i < 8; i++, n = n.parentNode || n.host) {
    if (n.nodeType === 1 && n.matches && n.matches(CLICK)) { hit = n; break; }
  }
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const tg = hit.tagName.toLowerCase(), ty = (hit.type || '').toLowerCase();
  const role = hit.getAttribute('role') || ({ a: 'link', button: 'button', select: 'combobox', textarea: 'textbox', summary: 'button' }[tg])
    || (tg === 'input' ? (ty === 'search' ? 'searchbox' : ['checkbox', 'radio'].includes(ty) ? ty : ['button', 'submit', 'image', 'reset'].includes(ty) ? 'button' : 'textbox')
    : hit.isContentEditable ? 'textbox' : tg);
  const name = clean(hit.getAttribute('aria-label') || (tg === 'input' && ['button', 'submit', 'reset'].includes(ty) ? hit.value : '') ||
    hit.innerText || hit.getAttribute('placeholder') || hit.getAttribute('title') || hit.getAttribute('alt') || '').slice(0, 200);
  const form = (() => { for (let n = hit; n; n = n.parentNode || n.host) if (n.tagName === 'FORM') return true; return false; })();
  const r = hit.getBoundingClientRect();
  return { tag: tg, role, name, input_type: ty, in_form: form,
           is_password: ty === 'password' || (hit.getAttribute('autocomplete') || '').includes('password'),
           box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)] };
}
