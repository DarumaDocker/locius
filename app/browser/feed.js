async () => {
  // RSS / Atom feeds: return a compact item list instead of raw XML (which is long, gets truncated, and confuses models).
  // Returns null for anything that is not a feed.
  const FEED = /^(rss|feed|rdf:RDF)$/i;
  const parse = txt => {
    txt = (txt || '').trim();
    if (!/^(<\?xml|<rss|<feed|<rdf:RDF)/i.test(txt)) return null;
    const d = new DOMParser().parseFromString(txt, 'application/xml');
    return d.getElementsByTagName('parsererror').length || !FEED.test(d.documentElement.nodeName) ? null : d;
  };
  let root = null;
  try {
    const de = document.documentElement;
    const viewer = document.getElementById('webkit-xml-viewer-source-xml');   // Chromium's XML viewer keeps the original here
    if (de && FEED.test(de.nodeName)) root = de;
    else if (viewer && viewer.firstElementChild && FEED.test(viewer.firstElementChild.nodeName)) root = viewer.firstElementChild;
    else {
      let d = parse(document.body && document.body.innerText);                // feed shown as plain text
      if (!d && /xml/.test(document.contentType || '')) {                     // XML viewer without the source node: re-read it
        const r = await fetch(location.href, { credentials: 'include' });
        d = parse(await r.text());
      }
      if (!d) return null;
      root = d.documentElement;
    }
  } catch (e) { return null; }
  const strip = s => (s || '').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
  const kid = (el, names) => {
    for (const n of names) {
      for (const c of el.children) {
        if (c.nodeName.toLowerCase() !== n.toLowerCase()) continue;
        if (n === 'link' && c.getAttribute('href') && (c.getAttribute('rel') || 'alternate') === 'alternate') return c.getAttribute('href');
        const t = (c.textContent || '').trim();
        if (t) return t;
      }
    }
    return '';
  };
  const channel = root.getElementsByTagName('channel')[0] || root;
  const nodes = [...root.getElementsByTagName('item'), ...root.getElementsByTagName('entry')].slice(0, 25);
  const items = nodes.map(it => ({
    title: strip(kid(it, ['title'])),
    link: kid(it, ['link', 'guid', 'id']),
    date: kid(it, ['pubDate', 'published', 'updated', 'dc:date']),
    summary: strip(kid(it, ['description', 'summary', 'content'])).slice(0, 160),
  }));
  return { feed: strip(kid(channel, ['title'])), items };
}
