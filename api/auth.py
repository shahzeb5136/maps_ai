"""
Clerk session-token verification.

The Next.js app sends `Authorization: Bearer <clerk session jwt>`. We verify it
against Clerk's public JWKS — no Clerk secret key, no network call to Clerk on
the request path once the key set is cached.
"""

import logging
from typing import Dict, Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from . import config

log = logging.getLogger(__name__)

# Clocks drift; Clerk tokens are short-lived (60s by default) and a strict
# comparison rejects perfectly good tokens.
LEEWAY_SECONDS = 30

# One JWKS client per configured issuer, built lazily and kept for the life of
# the process.
_jwk_clients: Dict[str, PyJWKClient] = {}

# auto_error=False so we can return our own JSON shape instead of FastAPI's.
_bearer = HTTPBearer(auto_error=False)


def _client(issuer: str) -> PyJWKClient:
    client = _jwk_clients.get(issuer)
    if client is None:
        # PyJWKClient keeps its own LRU cache and re-fetches on an unknown kid,
        # which is what makes Clerk's key rotation a non-event.
        client = PyJWKClient(config.jwks_url_for(issuer), cache_keys=True, lifespan=3600)
        _jwk_clients[issuer] = client
    return client


def _issuer_of(token: str) -> str:
    """
    Read `iss` without verifying, purely to pick which key set to check the
    signature against.

    Trusting an unverified claim is only safe because of what happens next: the
    value must appear in our configured allowlist, the signature is then checked
    against *that* issuer's published keys, and `jwt.decode` re-verifies the
    same `iss` for real. An attacker naming an issuer we don't host gets
    rejected; naming one we do still requires that issuer's private key.
    """
    claims = jwt.decode(token, options={"verify_signature": False})
    return str(claims.get("iss", "")).rstrip("/")


def verify_token(token: str) -> str:
    """Return the Clerk user id (`sub`), or raise 401."""
    try:
        issuer = _issuer_of(token)
    except jwt.InvalidTokenError as exc:
        log.warning("Rejected unparseable token: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token.")

    if issuer not in config.CLERK_ISSUERS:
        # Overwhelmingly the symptom of a CLERK_ISSUER that lists the wrong
        # Clerk instance — e.g. only the development one while the live site
        # authenticates against production. Log both sides; the answer is
        # always visible in the diff.
        log.warning("Rejected token from issuer %r — configured issuers are %s",
                    issuer, config.CLERK_ISSUERS)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token.")

    try:
        signing_key = _client(issuer).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            leeway=LEEWAY_SECONDS,
            issuer=issuer,
            # Clerk session tokens carry `azp` (authorised party), not `aud`.
            options={"verify_aud": False, "require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Session expired. Refresh the page and try again.")
    except PyJWKClientConnectionError as exc:
        # We could not reach Clerk. Genuinely our problem, so don't blame the
        # caller with a 401 they can do nothing about.
        log.error("JWKS fetch failed for %s: %s", config.jwks_url_for(issuer), exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Authentication is temporarily unavailable.")
    except PyJWKClientError as exc:
        # Reached Clerk but the token's `kid` is absent or unknown to the key
        # set — a bad token, not an outage. Note this is NOT a subclass of
        # InvalidTokenError, so it needs its own arm or it lands in 503.
        log.warning("Rejected token (key lookup): %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token.")
    except jwt.InvalidTokenError as exc:
        # Includes InvalidIssuerError — the usual symptom of a CLERK_ISSUER that
        # doesn't match the instance the website authenticates against.
        log.warning("Rejected token: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session token.")
    except Exception as exc:
        log.exception("Token verification failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Authentication is temporarily unavailable.")

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has no subject.")
    return user_id


def probe_jwks() -> dict:
    """
    Diagnostic for /health: for every configured issuer, can we actually fetch
    its key set? A CLERK_ISSUER listing the wrong Clerk instance is otherwise
    invisible until a real user hits a 401 they can't explain.
    """
    if not config.CLERK_ISSUERS:
        return {"configured": False, "issuers": [], "error": "CLERK_ISSUER not set"}

    issuers = []
    for issuer in config.CLERK_ISSUERS:
        try:
            keys = _client(issuer).get_jwk_set().keys
            issuers.append({"issuer": issuer, "reachable": True, "keys": len(keys)})
        except Exception as exc:
            issuers.append({"issuer": issuer, "reachable": False, "error": str(exc)[:200]})
    return {"configured": True, "issuers": issuers}


async def current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """FastAPI dependency yielding the Clerk user id."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    return verify_token(credentials.credentials)
