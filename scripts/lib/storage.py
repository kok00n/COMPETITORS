"""Minimal Supabase Storage REST helpers - upload/download/exists/list
operacje dla bucketu 'raw-pdfs'.

Storage API doc: https://supabase.com/docs/reference/javascript/storage-from-upload
REST endpoint: {SUPABASE_URL}/storage/v1/object/{bucket}/{path}
"""

import os
import time

import requests

# Reuse env vars from lib.supabase (ten sam URL i klucz)
SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

BUCKET = "raw-pdfs"


def _storage_url(path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path.lstrip('/')}"


def _headers(content_type: str | None = None, upsert: bool = False) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    if content_type:
        h["Content-Type"] = content_type
    if upsert:
        h["x-upsert"] = "true"
    return h


def upload_pdf(path: str, data: bytes, *, upsert: bool = True,
               content_type: str = "application/pdf") -> str:
    """Upload bytes do bucketu raw-pdfs pod sciezka `path`.

    Args:
        path: sciezka w bucketcie, np. 'PCS05/2026-04-30.pdf'
        data: bytes pliku (PDF default; tez JSON dla LLM cache)
        upsert: True = overwrite jesli juz istnieje
        content_type: 'application/pdf' (default) lub 'application/json' (dla cache_llm/)

    Returns:
        full path w buckecie (do zapisu w portfolio_snapshots.pdf_path)
    """
    url = _storage_url(path)
    r = _retry(
        lambda: requests.post(url, headers=_headers(content_type, upsert=upsert), data=data, timeout=60),
        op="upload",
        path=path,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Storage upload failed {r.status_code}: {r.text[:300]}")
    return f"{BUCKET}/{path}"


def download_pdf(path: str) -> bytes:
    """Pobierz PDF z bucketu raw-pdfs po sciezce.

    Args:
        path: sciezka bez prefixu 'raw-pdfs/', np. 'PCS05/2026-04-30.pdf'
            (akceptujemy tez z prefixem - automatyczny strip).
    """
    if path.startswith(f"{BUCKET}/"):
        path = path[len(BUCKET) + 1:]
    url = _storage_url(path)
    r = _retry(
        lambda: requests.get(url, headers=_headers(), timeout=60),
        op="download",
        path=path,
    )
    if r.status_code == 404:
        raise FileNotFoundError(f"Storage object not found: {path}")
    if r.status_code >= 400:
        raise RuntimeError(f"Storage download failed {r.status_code}: {r.text[:300]}")
    return r.content


def object_exists(path: str) -> bool:
    """Sprawdz czy obiekt istnieje w bucketcie (HEAD request)."""
    if path.startswith(f"{BUCKET}/"):
        path = path[len(BUCKET) + 1:]
    url = _storage_url(path)
    r = requests.head(url, headers=_headers(), timeout=15)
    return r.status_code == 200


def delete_object(path: str) -> None:
    """Usun obiekt z bucketu. Idempotent (404 nie rzuca)."""
    if path.startswith(f"{BUCKET}/"):
        path = path[len(BUCKET) + 1:]
    url = _storage_url(path)
    r = requests.delete(url, headers=_headers(), timeout=15)
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"Storage delete failed {r.status_code}: {r.text[:300]}")


def _retry(call, *, op: str, path: str, attempts: int = 3, base_wait: float = 2.0):
    """Retry HTTP na sieci/5xx z exponential backoff."""
    last_err = None
    for i in range(attempts):
        try:
            r = call()
            if 500 <= r.status_code < 600 and i < attempts - 1:
                wait = base_wait * (2 ** i)
                print(f"  storage {op} {path}: HTTP {r.status_code}, retry za {wait}s", flush=True)
                time.sleep(wait)
                continue
            return r
        except requests.RequestException as e:
            last_err = e
            wait = base_wait * (2 ** i)
            print(f"  storage {op} {path}: {type(e).__name__}, retry za {wait}s", flush=True)
            time.sleep(wait)
    raise last_err  # type: ignore[misc]
