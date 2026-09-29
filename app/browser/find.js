// Find elements by text anywhere on the page (after snapshot.js assigned refs). Returns matches with a bit of
// surrounding context (e.g. the product card's price) so the agent can pick the right one.
(q) => {
  const words = String(q || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = [];
  const all = document.querySelectorAll('[data-persona-ref]');
  for (const el of all) {
    const name = clean(el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || (el.querySelector && el.querySelector('img[alt]') ? el.querySelector('img[alt]').alt : ''));
    const low = name.toLowerCase();
    const hit = words.filter(w => low.includes(w)).length;
    if (!hit || hit < Math.ceil(words.length * 0.6)) continue;
    // context: the largest ancestor that is still card-sized (≤ 600 chars) — e.g. the product card with rating and price
    let ctx = '', p = el.parentElement;
    for (let i = 0; p && i < 10; i++, p = p.parentElement) {
      const t = clean(p.innerText);
      if (t.length > 600) break;
      if (t.length > name.length + 15) ctx = t;
    }
    ctx = clean(ctx.replace(name, ' ')).slice(0, 300);
    const r = el.getBoundingClientRect();
    out.push({ ref: el.getAttribute('data-persona-ref'), role: el.getAttribute('role') || el.tagName.toLowerCase(), name: name.slice(0, 120),
               score: hit / words.length + (low === words.join(' ') ? 0.5 : 0), context: ctx,
               where: r.bottom < 0 ? 'above' : r.top > (window.innerHeight || 800) ? 'below' : 'in view' });
    if (out.length > 200) break;
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, 25);
}
