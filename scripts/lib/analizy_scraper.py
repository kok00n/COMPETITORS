"""Pobieranie PDFow ze skladami portfeli z dokumenty.analizy.pl.

URL pattern: https://dokumenty.analizy.pl/pobierz/fi/{parasol_code}/SP/{YYYY-MM-DD}
  - SP = 'Sklad Portfela'
  - data = ostatni dzien miesiaca raportowego
  - dostepne bez auth/captcha
  - status 200 dla istniejacego raportu, 404 dla nieistniejacego (fundusze
    kwartalne na "luczne" miesiace, fundusze nieistniejace jeszcze w danej dacie)

Pobrany PDF moze zawierac wiele subfunduszy z tego samego parasola - parser
filtruje wiersze po kolumnie 'Identyfikator funduszu'.
"""

import hashlib
import time
from dataclasses import dataclass
from datetime import date

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PDF_URL = "https://dokumenty.analizy.pl/pobierz/fi/{parasol_code}/SP/{date}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class FetchResult:
    """Wynik proby pobrania PDFa dla pary (parasol_code, report_date)."""
    parasol_code: str
    report_date: date
    status: str          # 'ok', 'not_found', 'error'
    http_status: int     # 200, 404, 500, 0 dla network error
    content: bytes | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    error_message: str | None = None
    pdf_url: str = ""


def _build_url(parasol_code: str, report_date: date) -> str:
    return PDF_URL.format(parasol_code=parasol_code, date=report_date.isoformat())


def fetch_pdf(
    parasol_code: str,
    report_date: date,
    *,
    session: requests.Session | None = None,
    timeout: int = 60,
    max_retries: int = 3,
) -> FetchResult:
    """Pobierz jeden PDF z dokumenty.analizy.pl.

    Args:
        parasol_code: kod URL np. 'PCS05'
        report_date: data raportu (zwykle month-end), np. date(2026, 4, 30)
        session: opcjonalna requests.Session do reuse (warmup cookies / connection pooling)
        timeout: read timeout per request (sekundy)
        max_retries: ile razy retry na network error / 5xx (4xx nie retry)

    Returns:
        FetchResult ze statusem 'ok' (200), 'not_found' (404), albo 'error'.
    """
    sess = session or requests.Session()
    sess.verify = False
    url = _build_url(parasol_code, report_date)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/pdf, */*",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            r = sess.get(url, headers=headers, timeout=timeout)
            # 404 = brak raportu (kwartalne fundusze / fundusz nie istnial jeszcze) - nie retryjemy
            if r.status_code == 404:
                return FetchResult(parasol_code, report_date, "not_found", 404, pdf_url=url)
            # 4xx inne niz 404 - blad klienta, nie retry
            if 400 <= r.status_code < 500:
                return FetchResult(parasol_code, report_date, "error", r.status_code,
                                   error_message=f"HTTP {r.status_code}: {r.text[:200]}", pdf_url=url)
            # 5xx - retry z backoff
            if r.status_code >= 500:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"  {parasol_code} {report_date}: HTTP {r.status_code}, retry za {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                return FetchResult(parasol_code, report_date, "error", r.status_code,
                                   error_message=f"HTTP {r.status_code} after {max_retries} retries", pdf_url=url)
            # 200 = sukces. Sprawdzmy ze faktycznie dostalismy PDF
            content = r.content
            content_type = r.headers.get("Content-Type", "")
            if not content.startswith(b"%PDF") and "pdf" not in content_type.lower():
                # Czasami analizy.pl zwraca 200 z HTML errorem ("strona w przebudowie") albo redirect.
                return FetchResult(parasol_code, report_date, "error", r.status_code,
                                   error_message=f"Not a PDF response (Content-Type: {content_type})",
                                   pdf_url=url)
            sha = hashlib.sha256(content).hexdigest()
            return FetchResult(
                parasol_code, report_date, "ok", 200,
                content=content, sha256=sha, size_bytes=len(content), pdf_url=url,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  {parasol_code} {report_date}: {type(e).__name__}, retry za {wait}s", flush=True)
                time.sleep(wait)

    return FetchResult(
        parasol_code, report_date, "error", 0,
        error_message=f"{type(last_exc).__name__}: {last_exc}" if last_exc else "Unknown error",
        pdf_url=url,
    )


def month_end_dates(start: date, end: date) -> list[date]:
    """Wygeneruj liste month-end dates od start do end (wlacznie).

    Month-end = ostatni dzien miesiaca. analizy.pl publikuje raporty pod tymi datami.
    """
    from calendar import monthrange
    out: list[date] = []
    cur = date(start.year, start.month, 1)
    end_month = date(end.year, end.month, 1)
    while cur <= end_month:
        last_day = monthrange(cur.year, cur.month)[1]
        me = date(cur.year, cur.month, last_day)
        if me >= start and me <= end:
            out.append(me)
        # Inkrement do nastepnego miesiaca
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return out
