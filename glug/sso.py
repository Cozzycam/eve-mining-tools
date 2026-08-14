"""EVE SSO (OAuth2 + PKCE) for Glug — authenticated ESI access.

Stdlib only. Tokens live in ~/.eve-tools-cache/glug_sso.json (outside the repo).

Usage:
    python sso.py login <client_id>   # one-time browser login (register app first)
    python sso.py status              # smoke test: wallet, ship, location
    python sso.py selftest            # offline checks

Register the app at https://developers.eveonline.com:
    - Connection type: "Authentication & API Access"
    - Callback URL: http://localhost:8749/callback   (must match exactly)
    - Scopes: see SCOPES below
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eve_common

SSO_AUTHORIZE = "https://login.eveonline.com/v2/oauth/authorize/"
SSO_TOKEN = "https://login.eveonline.com/v2/oauth/token"
CALLBACK_PORT = 8749
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"
TOKEN_FILE = os.path.join(eve_common.CACHE_DIR, "glug_sso.json")

SCOPES = [
    "esi-wallet.read_character_wallet.v1",
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-location.read_online.v1",
    "esi-industry.read_character_mining.v1",
    "esi-planets.manage_planets.v1",
    "esi-universe.read_structures.v1",
    "esi-killmails.read_killmails.v1",
]


# ── Token store ───────────────────────────────────────────────

def _load():
    try:
        with open(TOKEN_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save(store):
    os.makedirs(eve_common.CACHE_DIR, exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(store, f, indent=2)
    try:
        os.chmod(TOKEN_FILE, 0o600)  # no-op on Windows, matters on the VPS
    except OSError:
        pass


# ── PKCE / JWT helpers ────────────────────────────────────────

def _b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_pkce_pair():
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def decode_jwt_payload(token):
    """Decode (without verifying) the payload of a JWT access token."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _token_request(form, secret=None):
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": eve_common.ESI_HEADERS["User-Agent"],
    }
    if secret:  # confidential app: authenticate with Basic client_id:secret
        creds = base64.b64encode(f"{form.pop('client_id')}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(SSO_TOKEN, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"SSO token request failed: HTTP {e.code}: {e.read().decode()}",
              file=sys.stderr)
        return None
    except (urllib.error.URLError, OSError) as e:
        print(f"SSO token request failed: {e}", file=sys.stderr)
        return None


def _store_tokens(store, tok):
    """Merge a token response into the store and persist. Returns store."""
    claims = decode_jwt_payload(tok["access_token"])
    store.update({
        "access_token": tok["access_token"],
        # SSO may rotate the refresh token — always keep the latest
        "refresh_token": tok.get("refresh_token", store.get("refresh_token")),
        "expires_at": time.time() + tok.get("expires_in", 1199),
        "character_id": int(claims["sub"].split(":")[-1]),
        "character_name": claims.get("name", "?"),
    })
    _save(store)
    return store


# ── Login flow ────────────────────────────────────────────────

class _CallbackHandler(BaseHTTPRequestHandler):
    code = None
    expected_state = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        if params.get("state", [None])[0] != _CallbackHandler.expected_state:
            self.send_error(400, "state mismatch")
            return
        _CallbackHandler.code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<h1>Glug&trade; thanks you for your compliance.</h1>"
            b"<p>Authentication complete. You may close this tab and "
            b"return to your assigned workstation.</p>")

    def log_message(self, *args):
        pass


def login(client_id, secret=None):
    verifier, challenge = make_pkce_pair()
    state = _b64url(secrets.token_bytes(16))
    _CallbackHandler.expected_state = state
    auth_url = SSO_AUTHORIZE + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "redirect_uri": CALLBACK_URL,
        "client_id": client_id,
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })

    server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    print(f"Opening browser for EVE SSO login...\nIf it doesn't open: {auth_url}")
    webbrowser.open(auth_url)
    while _CallbackHandler.code is None:
        server.handle_request()  # one request at a time until we get the code
    server.server_close()

    tok = _token_request({
        "grant_type": "authorization_code",
        "code": _CallbackHandler.code,
        "client_id": client_id,
        "code_verifier": verifier,
    }, secret=secret)
    if not tok:
        sys.exit("Token exchange failed.")
    store = _store_tokens({"client_id": client_id, "secret": secret}, tok)
    print(f"Logged in as {store['character_name']} ({store['character_id']}).")
    print(f"Tokens saved to {TOKEN_FILE}")


# ── Public API ────────────────────────────────────────────────

def get_access_token():
    """Return a valid access token, refreshing if needed. None if not logged in."""
    store = _load()
    if not store or "refresh_token" not in store:
        return None
    if time.time() < store.get("expires_at", 0) - 60:
        return store["access_token"]
    tok = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": store["refresh_token"],
        "client_id": store["client_id"],
    }, secret=store.get("secret"))
    if not tok:
        return None
    return _store_tokens(store, tok)["access_token"]


def character_id():
    store = _load()
    return store["character_id"] if store else None


def esi_auth_get(path):
    """Authenticated ESI GET. path e.g. '/characters/123/wallet/'.

    Returns parsed JSON, or None on error (details to stderr).
    """
    token = get_access_token()
    if not token:
        print("Not logged in — run: python sso.py login <client_id>", file=sys.stderr)
        return None
    url = eve_common.ESI_BASE + path
    sep = "&" if "?" in path else "?"
    url += f"{sep}datasource=tranquility"
    req = urllib.request.Request(url, headers={
        **eve_common.ESI_HEADERS,
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"ESI {path}: HTTP {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return None
    except (urllib.error.URLError, OSError) as e:
        print(f"ESI {path}: {e}", file=sys.stderr)
        return None


# ── CLI ───────────────────────────────────────────────────────

def status():
    store = _load()
    if not store:
        sys.exit(f"No token file at {TOKEN_FILE} — run: python sso.py login <client_id>")
    cid = store["character_id"]
    print(f"Character: {store['character_name']} ({cid})")

    wallet = esi_auth_get(f"/characters/{cid}/wallet/")
    print(f"Wallet:    {wallet:,.2f} ISK" if wallet is not None else "Wallet:    FAILED")

    ship = esi_auth_get(f"/characters/{cid}/ship/")
    if ship:
        info = eve_common.get_type_info(ship["ship_type_id"]) or {}
        print(f"Ship:      {ship.get('ship_name')} ({info.get('name', ship['ship_type_id'])})")
    else:
        print("Ship:      FAILED")

    loc = esi_auth_get(f"/characters/{cid}/location/")
    if loc:
        print(f"System:    {eve_common.resolve_system_name(loc['solar_system_id'])}")
    else:
        print("System:    FAILED")


def selftest():
    v, c = make_pkce_pair()
    assert len(v) == 43 and len(c) == 43, "PKCE lengths"
    assert c == _b64url(hashlib.sha256(v.encode()).digest()), "PKCE challenge"
    fake = "x." + _b64url(json.dumps(
        {"sub": "CHARACTER:EVE:12345", "name": "Test"}).encode()) + ".y"
    claims = decode_jwt_payload(fake)
    assert int(claims["sub"].split(":")[-1]) == 12345, "JWT decode"
    print("selftest OK")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "login":
        if len(sys.argv) < 3:
            sys.exit("Usage: python sso.py login <client_id> [secret]")
        login(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    elif cmd == "status":
        status()
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(f"Unknown command: {cmd}")
