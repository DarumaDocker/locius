"""Fake Microsoft identity platform (device code + refresh) and an OAuth introspection endpoint for Dovecot."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

STATE = {"polls": 0, "n": 0}
USER = "me@outlook.com"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        if self.path == "/introspect":
            tok = f.get("token", "")
            return self._send(200, {"active": "true", "email": USER} if tok.startswith("at-") else {"active": "false"})
        if self.path.endswith("/oauth2/v2.0/devicecode"):
            if f.get("client_id") != "11111111-2222-3333-4444-555555555555":
                return self._send(400, {"error": "unauthorized_client", "error_description": "AADSTS700016: app not found"})
            assert "IMAP.AccessAsUser.All" in f.get("scope", "") and "offline_access" in f.get("scope", "")
            return self._send(200, {"device_code": "dc-1", "user_code": "ABCD-1234", "expires_in": 900, "interval": 1,
                                    "verification_uri": "https://microsoft.com/devicelogin"})
        if self.path.endswith("/oauth2/v2.0/token"):
            if f.get("grant_type", "").endswith("device_code"):
                STATE["polls"] += 1
                if STATE["polls"] < 2:
                    return self._send(400, {"error": "authorization_pending"})
                STATE["n"] += 1
                return self._send(200, {"access_token": f"at-{STATE['n']}", "refresh_token": f"rt-{STATE['n']}", "expires_in": 3600})
            if f.get("grant_type") == "refresh_token":
                if not f.get("refresh_token", "").startswith("rt-"):
                    return self._send(400, {"error": "invalid_grant", "error_description": "AADSTS70000: bad refresh token"})
                STATE["n"] += 1
                return self._send(200, {"access_token": f"at-{STATE['n']}", "refresh_token": f"rt-{STATE['n']}", "expires_in": 3600})
        self._send(404, {"error": "not_found"})


def serve(port=18999):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    return srv


if __name__ == "__main__":
    serve(int(sys.argv[1]) if len(sys.argv) > 1 else 18999).serve_forever()
