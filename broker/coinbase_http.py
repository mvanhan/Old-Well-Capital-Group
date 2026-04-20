from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode

import certifi
import requests

try:
    from dotenv import find_dotenv, load_dotenv  # type: ignore
    load_dotenv(find_dotenv(), override=False)
except Exception:
    pass

try:
    from coinbase import jwt_generator  # type: ignore
except Exception:
    jwt_generator = None  # type: ignore

API_HOST = "api.coinbase.com"
BASE_URL = f"https://{API_HOST}"


def _sanitize_secret(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return raw
    s = raw.strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        s = s[1:-1]
    return s.replace("\\n", "\n")


def has_auth() -> bool:
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
    return bool(api_key and api_secret)


def _credentials() -> Tuple[str, str]:
    api_key = os.getenv("COINBASE_API_KEY", "").strip()
    api_secret = _sanitize_secret(os.getenv("COINBASE_API_SECRET"))
    if not api_key or not api_secret:
        raise RuntimeError("COINBASE_API_KEY and COINBASE_API_SECRET must be set")
    return api_key, api_secret


def _build_jwt(method: str, path: str) -> str:
    if jwt_generator is None:
        raise RuntimeError("coinbase-advanced-py not installed")
    api_key, api_secret = _credentials()
    jwt_uri = jwt_generator.format_jwt_uri(method.upper(), path)
    return jwt_generator.build_rest_jwt(jwt_uri, api_key, api_secret)


def _decode_response(response: requests.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except Exception:
        return response.text


def request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    auth: bool = False,
    timeout: Optional[float] = None,
) -> Tuple[int, Any]:
    filtered = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    full_path = path
    if filtered:
        full_path = f"{path}?{urlencode(filtered, doseq=True)}"

    headers: Dict[str, str] = {}
    if auth:
        token = _build_jwt(method, path)
        headers["Authorization"] = f"Bearer {token}"
        headers["Content-Type"] = "application/json"
    elif payload is not None:
        headers["Content-Type"] = "application/json"

    try:
        response = requests.request(
            method=method.upper(),
            url=f"{BASE_URL}{full_path}",
            headers=headers,
            json=payload,
            timeout=timeout or float(os.getenv("CB_HTTP_TIMEOUT", os.getenv("CB_SDK_TIMEOUT", "20"))),
            verify=certifi.where(),
        )
        return response.status_code, _decode_response(response)
    except requests.exceptions.RequestException as exc:
        return 0, {"request_error": str(exc)}


def _raise_on_error(status: int, data: Any, path: str) -> None:
    if 200 <= status < 300:
        return

    if isinstance(data, (dict, list)):
        detail = json.dumps(data, sort_keys=True)
    else:
        detail = str(data)

    raise RuntimeError(f"HTTP {status} for {path}: {detail}")


def public_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    status, data = request("GET", path, params=params, auth=False)
    _raise_on_error(status, data, path)
    return data


def authed_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    status, data = request("GET", path, params=params, auth=True)
    _raise_on_error(status, data, path)
    return data


def authed_post(path: str, payload: Dict[str, Any]) -> Any:
    status, data = request("POST", path, payload=payload, auth=True)
    if isinstance(data, dict):
        data = dict(data)
        data["_http_status"] = status
        return data
    return {"raw": data, "_http_status": status}