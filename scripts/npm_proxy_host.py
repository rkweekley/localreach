"""Create the NPM proxy host + Let's Encrypt cert for reach.cyberalsolutions.com.

Idempotent: re-running reuses an existing proxy host and re-issues nothing if a
cert is already attached. Run ON the Mac mini; NPM listens on 127.0.0.1:81.
"""
import json
import os
import urllib.error
import urllib.request

NPM = "http://127.0.0.1:81"
DOMAIN = "reach.cyberalsolutions.com"
UPSTREAM = ("http", "localreach", 8000)
IDENTITY = os.environ.get("NPM_IDENTITY", "rweekley@cyberalsolutions.com")
SECRET = os.environ["NPM_SECRET"]  # never commit credentials; export before running


def api(path, payload=None, method=None, token=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(NPM + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = r.read()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:400]


st, body = api("/api/tokens", {"identity": IDENTITY, "secret": SECRET})
if st != 200 or "token" not in body:
    raise SystemExit(f"login failed: {st} {body}")
tok = body["token"]
print("login ok")

scheme, host, port = UPSTREAM
st, hosts = api("/api/nginx/proxy-hosts", token=tok)
if st != 200:
    raise SystemExit(f"list failed: {st} {hosts}")
existing = next((h for h in hosts if DOMAIN in h.get("domain_names", [])), None)

payload = {
    "domain_names": [DOMAIN],
    "forward_scheme": scheme,
    "forward_host": host,
    "forward_port": port,
    "access_list_id": 0,
    "certificate_id": 0,
    "ssl_forced": True,
    "caching_enabled": False,
    "block_exploits": True,
    "advanced_config": "",
    "meta": {"letsencrypt_agree": True, "dns_challenge": False},
    "allow_websocket_upgrade": False,
    "http2_support": True,
    "forward_scheme_hint": "http",
    "locations": [],
}

if existing:
    ph_id = existing["id"]
    cert_id = existing.get("certificate_id") or 0
    payload["certificate_id"] = cert_id
    st, body = api(f"/api/nginx/proxy-hosts/{ph_id}", payload, method="PUT", token=tok)
    print("proxy host updated:", st, ph_id)
else:
    st, body = api("/api/nginx/proxy-hosts", payload, token=tok)
    if st not in (200, 201):
        raise SystemExit(f"create failed: {st} {body}")
    ph_id = body["id"]
    cert_id = 0
    print("proxy host created:", st, ph_id)

if not cert_id:
    st, cert = api("/api/nginx/certificates", {"domain_names": [DOMAIN], "provider": "letsencrypt"}, token=tok)
    print("cert request:", st, json.dumps(cert)[:200] if isinstance(cert, dict) else cert)
    if st in (200, 201) and isinstance(cert, dict) and cert.get("id"):
        cert_id = cert["id"]

if cert_id:
    payload["certificate_id"] = cert_id
    st, body = api(f"/api/nginx/proxy-hosts/{ph_id}", payload, method="PUT", token=tok)
    print("cert attached:", st, "cert_id=", cert_id)
else:
    print("WARNING: no certificate id")

st, hosts = api("/api/nginx/proxy-hosts", token=tok)
final = next((h for h in hosts if DOMAIN in h.get("domain_names", [])), None)
print("final:", json.dumps({k: final.get(k) for k in ("id", "domain_names", "forward_host", "forward_port", "ssl_forced", "certificate_id", "enabled")}) if final else "MISSING")
