// Visual targeting (browser_locate): draw a labelled grid over the page (or over one region of it) so a vision model can
// say "C4" / "17", and optionally a red marker at a point so it can confirm "yes, that's the chat bubble".
// opts: {mode: 'draw'|'clear', region: [x, y, w, h], cols, rows, labels: 'letters'|'numbers', mark: [x, y]}
(opts) => {
  const ID = '__persona_grid__';
  const old = document.getElementById(ID);
  if (old) old.remove();
  if (!opts || opts.mode === 'clear') return true;
  const VW = window.innerWidth, VH = window.innerHeight;
  const [rx, ry, rw, rh] = opts.region || [0, 0, VW, VH];
  const layer = document.createElement('div');
  layer.id = ID;
  layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;';
  const add = css => { const d = document.createElement('div'); d.style.cssText = 'position:fixed;' + css; layer.appendChild(d); return d; };
  if (opts.cols && opts.rows) {
    const cols = opts.cols, rows = opts.rows, cw = rw / cols, ch = rh / rows;
    const LINE = 'rgba(255,0,200,0.85)';
    for (let c = 0; c <= cols; c++) add(`left:${rx + c * cw - 1}px;top:${ry}px;width:2px;height:${rh}px;background:${LINE};`);
    for (let r = 0; r <= rows; r++) add(`left:${rx}px;top:${ry + r * ch - 1}px;width:${rw}px;height:2px;background:${LINE};`);
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      const label = opts.labels === 'numbers' ? String(r * cols + c + 1) : 'ABCDEFGHIJKL'[c] + (r + 1);
      const small = opts.labels === 'numbers';   // zoomed cells are small: keep the labels from hiding the target
      const d = add(`left:${rx + c * cw + 1}px;top:${ry + r * ch + 1}px;background:rgba(0,0,0,${small ? 0.55 : 0.72});color:#fff;` +
                    `font:bold ${small ? 10 : 12}px/${small ? 11 : 14}px monospace;padding:0 2px;border-radius:2px;`);
      d.textContent = label;
    }
  }
  if (opts.mark) {
    const [x, y] = opts.mark;
    add(`left:${x - 14}px;top:${y - 14}px;width:28px;height:28px;border:3px solid #ff0000;border-radius:50%;box-sizing:border-box;`);
    add(`left:${x - 22}px;top:${y - 1}px;width:44px;height:2px;background:#ff0000;`);
    add(`left:${x - 1}px;top:${y - 22}px;width:2px;height:44px;background:#ff0000;`);
  }
  document.documentElement.appendChild(layer);
  return true;
}
