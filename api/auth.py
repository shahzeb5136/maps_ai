"""
Clerk session-token verification.

The Next.js app sends `Authorization: Bearer <clerk session jwt>`. We verify it
against Clerk's public JWKS — no Clerk secret key, no network call to Clerk on
the request path once the key set is cached.
"""

import logging
from typing import Optional

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

_jwk_client: Optional[PyJWKClient] = None

# auto_error=False so we can return our own JSON shape instead of FastAPI's.
_bearer = HTTPBearer(auto_error=False)


def _client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        if not config.CLERK_JWKS_URL:
            raise RuntimeError("CLERK_JWKS_URL / CLERK_ISSUER is not configured")
        # PyJWKClient keeps its own LRU cache and re-fetches on an unknown kid,
        # which is what makes Clerk's key rotation a non-event.
        _jwk_client = PyJWKClient(config.CLERK_JWKS_URL, cache_keys=True, lifespan=3600)
    return _jwk_client


def verify_token(token: str) -> str:
    """Return the Clerk user id (`sub`), or raise 401."""
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            leeway=LEEWAY_SECONDS,
            issuer=config.CLERK_ISSUER or None,
            # Clerk session tokens carry `azp` (authorised party), not `aud`.
            options={"verify_aud": False, "require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Session expired. Refresh the page and try again.")
    except PyJWKClientConnectionError as exc:
        # We could not reach Clerk. Genuinely our problem, so don't blame the
        # caller with a 401 they can do nothing about.
        log.error("JWKS fetch failed for %s: %s", config.CLERK_JWKS_URL, exc)
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
    Diagnostic for /health: can we actually fetch the key set, and how many
    keys did we get? A misconfigured CLERK_ISSUER is otherwise invisible until
    a real user hits a 401 they can't explain.
    """
    if not config.CLERK_JWKS_URL:
        return {"configured": False, "reachable": False, "error": "CLERK_ISSUER not set"}
    try:
        keys = _client().get_jwk_set().keys
        return {"configured": True, "reachable": True, "keys": len(keys)}
    except Exception as exc:
        return {"configured": True, "reachable": False, "error": str(exc)[:200]}


async def current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """FastAPI dependency yielding the Clerk user id."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    return verify_token(credentials.credentials)
