// Optional OMuse Google token broker (Cloudflare Worker). Serves the relay page at GET / and adds the OAuth client
// secret to token requests at POST /token, so the secret never ships inside OMuse. Stateless: nothing is stored.
// Secrets (wrangler secret put): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET. Var: RELAY_URL (this worker's https URL + "/").
// In OMuse's google_managed.json: {"client_id": "...", "relay": "<RELAY_URL>", "broker": "<worker URL>"}
import RELAY_HTML from './index.html';

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === 'GET' && (url.pathname === '/' || url.pathname === '/index.html')) {
      return new Response(RELAY_HTML, { headers: { 'content-type': 'text/html; charset=utf-8', 'referrer-policy': 'no-referrer',
        'cache-control': 'no-store' } });
    }
    if (req.method === 'POST' && url.pathname === '/token') {
      const f = await req.formData();
      const grant = f.get('grant_type');
      if (f.get('client_id') !== env.GOOGLE_CLIENT_ID) return json({ error: 'invalid_client' }, 401);
      const body = new URLSearchParams({ client_id: env.GOOGLE_CLIENT_ID, client_secret: env.GOOGLE_CLIENT_SECRET, grant_type: grant });
      if (grant === 'authorization_code') {
        if (f.get('redirect_uri') !== env.RELAY_URL) return json({ error: 'invalid_request', error_description: 'redirect_uri' }, 400);
        for (const k of ['code', 'redirect_uri', 'code_verifier']) if (f.get(k)) body.set(k, f.get(k));
        if (!f.get('code_verifier')) return json({ error: 'invalid_request', error_description: 'PKCE required' }, 400);
      } else if (grant === 'refresh_token') {
        body.set('refresh_token', f.get('refresh_token') || '');
      } else {
        return json({ error: 'unsupported_grant_type' }, 400);
      }
      const r = await fetch('https://oauth2.googleapis.com/token', { method: 'POST', body,
        headers: { 'content-type': 'application/x-www-form-urlencoded' } });
      return new Response(await r.text(), { status: r.status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' } });
    }
    return new Response('Not found', { status: 404 });
  },
};

function json(o, status) {
  return new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json' } });
}
