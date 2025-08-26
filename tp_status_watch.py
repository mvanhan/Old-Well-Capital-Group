"""
Watch open orders for a given userref.
Usage:
  python tp_status_watch.py <userref>
"""
import sys, time
from env_utils import get_kraken_credentials
from broker.kraken_private import KrakenAuth

def main():
    if len(sys.argv) < 2:
        print("Usage: python tp_status_watch.py <userref>")
        return
    userref = int(sys.argv[1])

    key, sec_b64 = get_kraken_credentials()
    print(f"[env] KRAKEN_API_KEY len={len(key)}  KRAKEN_API_SECRET(_B64) len={len(sec_b64)}")
    auth = KrakenAuth(key, sec_b64)

    while True:
        oo = auth.rest("OpenOrders", {}).get("open", {})
        me = {k:v for k,v in oo.items() if int(v.get("userref", -1)) == userref}
        print(f"\n[userref={userref}] open_order_count={len(me)}")
        for txid, od in me.items():
            d = od.get("descr", {}) or {}
            print(f"  {txid}: {d.get('pair','?')} {d.get('type','?')} {d.get('ordertype','?')} vol={od.get('vol','?')} price={d.get('price','?')}")
        time.sleep(2)

if __name__ == "__main__":
    main()
