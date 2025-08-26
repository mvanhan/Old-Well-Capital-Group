"""
ws_token_check.py
Checks whether your Spot API key can obtain a WebSockets Auth v2 token.
Usage:
  python ws_token_check.py
"""
from __future__ import annotations
from env_utils import get_kraken_credentials, get_loaded_dotenv_path
from broker.kraken_private import KrakenAuth

def main():
    key, sec = get_kraken_credentials()
    print(f"[env] loaded .env: {get_loaded_dotenv_path() or '<none>'}")
    print(f"[env] key len={len(key)} secret_b64 len={len(sec)}")
    auth = KrakenAuth(key, sec)
    try:
        tok = auth.get_ws_token()
        print(f"[ok] WS token acquired. length={len(tok)}")
    except Exception as e:
        print(f"[warn] WS token failed: {e}")
        print("Hint: Enable 'Access WebSockets API' on your Spot key (Kraken UI) "
              "or proceed with REST fallback (our code does this automatically).")

if __name__ == "__main__":
    main()
