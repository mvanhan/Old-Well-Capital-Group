"""
auth_check.py
Deterministic diagnostics for Kraken Spot creds using env_utils.
- Loads .env with override=True, replacing any stale exported env vars.
- Prints which .env file was loaded.
- Calls /0/private/Balance to verify the key actually works on Spot.

Usage:
  python auth_check.py
"""
from __future__ import annotations
import json
from env_utils import get_kraken_credentials, diagnose_credentials, get_loaded_dotenv_path
from broker.kraken_private import KrakenAuth

def main():
    diag = diagnose_credentials()
    print("[env] diagnostics:", json.dumps(diag, indent=2))

    key, sec_b64 = get_kraken_credentials()
    if not key or not sec_b64:
        print("\n[fail] Missing API key or secret.")
        print("-> Ensure .env has:")
        print("   KRAKEN_API_KEY=<public key from Kraken Spot UI>")
        print("   KRAKEN_API_SECRET=<private key (base64) or raw>")
        print("   (Loaded .env: %s)" % (get_loaded_dotenv_path() or "<none>"))
        return

    auth = KrakenAuth(key, sec_b64)
    print("\n[test] Calling /0/private/Balance (read-only)...")
    try:
        res = auth.rest("Balance", {})
        ok = isinstance(res, dict)
        print("[ok]" if ok else "[warn]", "Balance returned a dict:", ok)
    except Exception as e:
        emsg = str(e)
        print("[error] Balance call failed:", emsg)
        if "EAPI:Invalid key" in emsg:
            print("-> Spot server does not recognize the API KEY you sent.")
            print("   Common causes: FUTURES key on Spot, pasted SECRET into KRAKEN_API_KEY,")
            print("   key disabled, IP restriction, or different .env loaded.")
            print("   Loaded .env:", get_loaded_dotenv_path() or "<none>")
        elif "EAPI:Invalid signature" in emsg:
            print("-> The SECRET does not match that key (wrong secret or base64 corruption).")
        elif "EAPI:Invalid nonce" in emsg:
            print("-> Clock skew/nonce reuse (rare with ms nonce).")
        elif "EGeneral:Permission denied" in emsg:
            print("-> Missing permission for this endpoint. For trading you'll need Add/Cancel Orders.")
        else:
            print("-> Unexpected; re-check key/secret formatting and permissions in Kraken Spot UI.")
            print("   Loaded .env:", get_loaded_dotenv_path() or "<none>")

if __name__ == "__main__":
    main()
