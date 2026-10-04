// Is this page a bot-protection wall (not the site's real content)? Returns {kind, detail} or null.
// Read-only; used so the agent stops retrying a site that blocks automated browsers.
(status) => {
  const title = (document.title || '').toLowerCase();
  const body = document.body ? (document.body.innerText || '').slice(0, 4000) : '';
  const text = body.toLowerCase();
  const url = location.href.toLowerCase();
  const short = body.trim().length < 1500;
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => (f.src || '').toLowerCase());
  // captcha widgets the user would actually have to solve: visible, not the invisible reCAPTCHA "aframe" that ad and
  // analytics tags load on ordinary pages (2026-10-04: demoqa.com forms were reported as a bot wall because of it)
  const visibleCaptcha = Array.from(document.querySelectorAll('iframe')).some(f => {
    const s = (f.src || '').toLowerCase();
    if (!(s.includes('recaptcha') || s.includes('hcaptcha.com') || s.includes('arkoselabs') || s.includes('funcaptcha'))) return false;
    if (s.includes('/aframe') || s.includes('size=invisible')) return false;
    const r = f.getBoundingClientRect();
    return r.width > 60 && r.height > 40 && getComputedStyle(f).visibility !== 'hidden';
  });
  const fields = document.querySelectorAll('input:not([type=hidden]), select, textarea').length;
  const has = (s) => text.includes(s) || title.includes(s);
  const hit = (kind, detail) => ({ kind, detail, status: status || 0 });
  if (url.includes('google.') && url.includes('/sorry/')) return hit('google-unusual-traffic', 'Google "unusual traffic" check');
  if (has('our systems have detected unusual traffic')) return hit('google-unusual-traffic', 'Google "unusual traffic" check');
  if (title.includes('just a moment') || title.includes('attention required! | cloudflare') ||
      (short && (has('checking your browser') || has('verify you are human') || has('enable javascript and cookies to continue'))) ||
      frames.some(s => s.includes('challenges.cloudflare.com')) && short) return hit('cloudflare', 'Cloudflare bot check');
  if (has('access denied') && (has('reference #') || has("you don't have permission to access") || has('errors.edgesuite.net')))
    return hit('akamai', 'Akamai "Access Denied"');
  if (frames.some(s => s.includes('captcha-delivery.com'))) return hit('datadome', 'DataDome bot check');
  if (has('press & hold') || document.getElementById('px-captcha')) return hit('perimeterx', 'PerimeterX "press & hold" check');
  if (has('request unsuccessful. incapsula') || has('incapsula incident id')) return hit('imperva', 'Imperva/Incapsula block');
  if (has('to discuss automated access to amazon data') || has("sorry, we just need to make sure you're not a robot"))
    return hit('amazon', 'Amazon robot check');
  if (short && (has('are you a robot') || has('are you a human') || has('not a robot') || has('bot detection') ||
      has('unusual traffic') || has('automated access') || has('blocked for security reasons') ||
      (visibleCaptcha && fields < 4)))
    return hit('captcha', 'CAPTCHA / robot check');
  if ((status === 403 || status === 429 || status === 451) && short)
    return hit('http-' + status, 'HTTP ' + status + (status === 429 ? ' Too Many Requests' : ' Forbidden'));
  return null;
}
