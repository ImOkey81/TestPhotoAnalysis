import os, time, requests
from functools import wraps
from flask import request, jsonify
from jwt import PyJWKClient, decode as jwt_decode

_jwks_client = None

def get_jwks_client():
    global _jwks_client
    if _jwks_client is None:
        url = os.environ["AUTHENTIK_JWKS_URL"]
        _jwks_client = PyJWKClient(url)
    return _jwks_client

def verify_token(token):
    client = get_jwks_client()
    signing_key = client.get_signing_key_from_jwt(token)
    return jwt_decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        options={"verify_aud": False}
    )

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        try:
            verify_token(auth.split(" ", 1)[1])
        except Exception as e:
            print(f"[AUTH ERROR] {type(e).__name__}: {e}")
            return jsonify({"error": "Invalid token", "detail": str(e)}), 401
        return f(*args, **kwargs)
    return decorated

