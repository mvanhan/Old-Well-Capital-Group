# Run: python env_check.py
import os

try:
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except Exception:
    pass

k = os.getenv("KRAKEN_API_KEY","")
s = os.getenv("KRAKEN_API_SECRET_B64","")
print("KRAKEN_API_KEY length:", len(k))
print("KRAKEN_API_SECRET_B64 length:", len(s))
