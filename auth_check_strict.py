"""
auth_check_strict.py
Diagnose 'EAPI:Invalid key' on Kraken Spot by exposing hidden characters and
letting you bypass .env completely.

Usage:
  # Use environment variables if set
  python auth_check_strict.py

  # OR pass values explicitly (recommended for isolation)
  python auth_check_strict.py --key YOUR_PUBLIC_KEY --secret YOUR_PRIVATE_KEY
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import time
import requests


def _clean(s: str | None) -> str:
    return s if s is None else s.strip().strip('"').strip("'")


def show_hidden(label: str, s: str) -> None:
    print(f"{label}: len={len(s)} repr={s!r}")
    if s:
        hb = " ".join(f"{ord(c):02x}" for c in s[:16])
        print(f"{label} first16 hex: {hb}")


def to_base64_secret(secret: str) -> tuple[str, str]:
    """
    Ensure the secret is base64. If it's already valid base64, keep it;
    otherwise, base64-encode the raw string.
    """
    sec = _clean(secret or "")
    if not sec:
        return "", "secret missing/empty"
    try:
        base64.b64decode(sec, validate=True)
        return sec, "provided secret looks like base64"
    except Exception:
        return base64.b64encode(sec.encode()).decode(), "provided secret did NOT look base64; encoded as base64"


def sign(path: str, data: dict, api_key: str, secret_b64: str) -> tuple[dict, dict]:
    """
    Kraken Spot private REST signature.
    """
    nonce = str(int(time.time() * 1000))
    body = {"nonce": nonce, **data}
    postdata = "&".join(f"{k}={v}" for k, v in body.items())
    sha256 = hashlib.sha256((nonce + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret_b64), path.encode() + sha256, hashlib.sha512)
    headers = {"API-Key": api_key, "API-Sign": base64.b64encode(mac.digest()).decode()}
    return headers, body


def main():
    # No dotenv here on purpose—avoid accidental .env shadowing.
    p = argparse.ArgumentParser()
    p.add_argument("--key", help="Kraken Spot API key (public)")
    p.add_argument("--secret", help="Kraken Spot API secret (private; base64 or raw)")
    args = p.parse_args()

    api_key = _clean(args.key or os.getenv("KRAKEN_API_KEY", ""))
    api_secret_in = _clean(
        args.secret
        or os.getenv("KRAKEN_API_SECRET_B64", "")
        or os.getenv("KRAKEN_API_SECRET", "")
    )
    secret_b64, secret_note = to_base64_secret(api_secret_in)

    print("\n== INPUTS ==")
    show_hidden("API_KEY", api_key)
    show_hidden("API_SECRET", api_secret_in)
    print("SECRET note:", secret_note)

    b64ish = bool(re.match(r"^[A-Za-z0-9+/]+={0,2}$", api_key)) if api_key else False
    print("\nHeuristics:")
    print(" - api_key contains '+' or '/':", ("+" in api_key) or ("/" in api_key))
    print(" - api_key looks base64-ish:", b64ish)

    if not api_key or not secret_b64:
        print("\n[fail] Missing API key or secret. Provide --key/--secret or set env vars.")
        return

    print("\n== CALL /0/private/Balance ==")
    path = "/0/private/Balance"
    url = "https://api.kraken.com" + path
    headers, body = sign(path, {}, api_key, secret_b64)
    print("API-Key header length:", len(headers.get("API-Key", "")))

    try:
        r = requests.post(url, data=body, headers=headers, timeout=12)
        print("HTTP status:", r.status_code)
        try:
            j = r.json()
            print("Raw Kraken JSON:", json.dumps(j))
            if j.get("error"):
                print("Kraken error array:", j["error"])
            else:
                print("[ok] Balance payload received.")
        except Exception:
            print("Non-JSON response body (truncated):", r.text[:300])
    except Exception as e:
        print("HTTP/Exception:", repr(e))


if __name__ == "__main__":
    main()
