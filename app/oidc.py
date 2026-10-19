"""Sign in with Google (OpenID Connect).

The browser goes to Google with a random state, a nonce and a PKCE challenge;
Google sends back a one-time code that only this server can swap (with its
client secret and the PKCE verifier) for a signed ID token. The token's
signature is checked against Google's published keys, along with its issuer,
audience, expiry and nonce. Only addresses Google has verified are accepted."""
import base64
import hashlib
import hmac
import secrets
from urllib.parse import urlencode

import httpx
import jwt

from .config import settings

_jwks: dict[str, jwt.PyJWKClient] = {}


class ProviderError(Exception):
    """Shown to the person on the login page, so keep it plain."""


def providers() -> dict[str, dict]:
    """The providers whose app credentials are configured."""
    out = {}
    if settings.google_client_id and settings.google_client_secret:
        out["google"] = {
            "name": "Google", "kind": "oidc", "id": settings.google_client_id, "secret": settings.google_client_secret,
            "authorize": "https://accounts.google.com/o/oauth2/v2/auth", "exchange_url": "https://oauth2.googleapis.com/token",
            "jwks": "https://www.googleapis.com/oauth2/v3/certs", "scope": "openid email profile",
            "issuers": {"https://accounts.google.com", "accounts.google.com"}}
    return out


def redirect_uri(provider: str) -> str:
    return f"{settings.public_url}/api/auth/oauth/{provider}/callback"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(provider: str, state: str, nonce: str, challenge: str) -> str:
    cfg = providers()[provider]
    q = {"client_id": cfg["id"], "redirect_uri": redirect_uri(provider), "response_type": "code",
         "scope": cfg["scope"], "state": state}
    if cfg["kind"] == "oidc":
        q.update(nonce=nonce, code_challenge=challenge, code_challenge_method="S256", prompt="select_account")
    return cfg["authorize"] + "?" + urlencode(q)


def fetch_identity(provider: str, code: str, verifier: str, nonce: str) -> dict:
    """Swap the one-time code for the person's verified identity:
    {"sub": provider's stable id, "email": verified address, "name": display name}."""
    cfg = providers()[provider]
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(provider),
            "client_id": cfg["id"], "client_secret": cfg["secret"]}
    if cfg["kind"] == "oidc":
        data["code_verifier"] = verifier
    try:
        with httpx.Client(timeout=10, headers={"Accept": "application/json", "User-Agent": settings.site_name}) as http:
            r = http.post(cfg["exchange_url"], data=data)
            tok = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200 or "error" in tok:
                raise ProviderError(f"{cfg['name']} didn't accept the sign-in. Try again.")
    except httpx.HTTPError as e:
        raise ProviderError(f"Couldn't reach {cfg['name']}. Try again in a moment.") from e
    return verify_id_token(provider, tok.get("id_token", ""), nonce)


def verify_id_token(provider: str, id_token: str, nonce: str) -> dict:
    cfg = providers()[provider]
    try:
        client = _jwks.setdefault(provider, jwt.PyJWKClient(cfg["jwks"], cache_keys=True, lifespan=3600, timeout=10))
        key = client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, key.key, algorithms=["RS256"], audience=cfg["id"], leeway=60,
                            options={"require": ["exp", "iat", "iss", "sub", "aud"]})
    except jwt.PyJWTError as e:
        raise ProviderError(f"{cfg['name']} sent a sign-in we couldn't verify. Try again.") from e
    ok_issuer = claims["iss"] in cfg["issuers"]
    verified = claims.get("email_verified") in (True, "true")
    if not ok_issuer:
        raise ProviderError(f"That sign-in didn't come from {cfg['name']}.")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise ProviderError("That sign-in was replayed or expired. Try again.")
    email = str(claims.get("email") or "").strip().lower()
    if not email or not verified:
        raise ProviderError(f"Your {cfg['name']} account doesn't have a verified email address we can use.")
    return {"sub": str(claims["sub"]), "email": email, "name": str(claims.get("name") or "")[:80]}

