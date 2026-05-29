"""Claude PDF parser - fallback dla PDFow z broken pdfplumber output.

WAZNE: parser zwraca WSZYSTKIE wiersze z PDFa parasola (czyli wszystkie
subfundusze). Filtrowanie po fund_id zachodzi w parse_pdfs.py (Python-side).

Powod: jeden PDF parasola (np. Goldman Sachs Parasolowy FIO) zawiera N
subfunduszy (ING03, ING04, ING54, ING90). Robimy 1 Claude call per PDF
(po pdf_hash), nie N - massive cost saving.

Cache strategy:
  - Klucz: pdf_hash (full SHA256)
  - Storage: cache_llm/{prefix2}/{hash}.json
  - Wszystkie snapshoty z tym samym pdf_hash dziela 1 LLM call
  - Re-run = darmowy load z cache

Cost przyklad (Sonnet 4.6):
  - PDF ~50K input tokens + ~10K output JSON
  - Per call: 50K × $3/M + 10K × $15/M = $0.15 + $0.15 = ~$0.30
  - 12 broken-layout fund × ALE ~6 unikalnych parasoli = ~6 calls
  - Z cache dedup: pelny backfill 4 lata = ~6 unique PDFs × 49 dat = ~300 calls × $0.30 = ~$90
  - W praktyce dedup po pdf_hash w czasie (PDFy parasoli czesto sa identyczne
    miedzy snapshotami jesli skladu nie zmieniono) - cache hit rate wysoki.
"""

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field

CACHE_PATH_PREFIX = "cache_llm"
DEFAULT_MODEL = "claude-sonnet-4-6"
# Goldman Sachs Parasolowy FIO ma 4 subfundusze x ~75 holdings = ~300 wierszy JSON
# ≈ 40-50K output tokens. Sonnet 4.6 wspiera do 64K output bez extended thinking.
MAX_TOKENS = 64000

PROMPT_EXTRACT_ALL = """Wyciagnij sklad portfela z zalaczonego PDFa (format raportowy KNF dla TFI).
PDF moze zawierac WIELE subfunduszy - wyciagnij WSZYSTKIE wiersze ze wszystkich
subfunduszy.

Zwroc TYLKO JSON tablica wierszy (bez markdown, bez wyjasnien przed/po).

Dla kazdego wiersza danych (NIE header, NIE summary, NIE puste wiersze) zwroc obiekt:
{{
  "fund_id": "<identyfikator subfunduszu w PDFie - np. PKO005, ING003, PLSFIO00208, BNPP_Akcji_Selektywny, albo dluga nazwa subfunduszu jesli to faktyczny ID w PDFie>",
  "subfund_name": "<pelna nazwa subfunduszu>",
  "issuer_name": "<nazwa emitenta>",
  "isin": "<ISIN 12 znakow albo null jesli brak ISIN-a>",
  "instrument_type": "<EXACT jedna z: Obligacje | Akcje | Aktywa Reverse Repo | Zobowiazania Repo | Tytuly i jednostki uczestnictwa | Kontrakt terminowy | Spot-Forward | FX Swap | Swap walutowy | IRS | Gotowka/Depozyty/Naleznosci | Pozyczki papierow wartosciowych>",
  "instrument_category": "<kategoria albo null>",
  "issuer_country": "<2-letter ISO code: PL, US, RO, etc.>",
  "risk_country": "<2-letter code albo null jesli brak kolumny>",
  "currency": "<3-letter code: PLN, EUR, USD, etc.>",
  "quantity": <float ilosc w portfelu>,
  "value_pln": <float wartosc w PLN>,
  "weight_assets_pct": <float % udzial w aktywach ogolem>,
  "weight_nav_pct": <float % udzial w NAV (jesli kolumna brak, uzyj weight_assets_pct)>,
  "info": "<informacje uzupelniajace albo null>"
}}

WAZNE:
- Konwertuj polskie liczby: "2 399,00" -> 2399.00 (spacja=thousands, przecinek=decimal)
- ISIN: 12 znakow alfanumerycznych, np. PLxxxxxxxxxx, XSxxxxxxxxxx, DExxxxxxxxxx. Jesli widzisz inny identyfikator (np. PLXXXX bez liczb), traktuj jako null
- Pomijaj wiersze "summary" / "razem" / "podsumowanie"
- Pamietaj: fund_id musi byc TEN SAM dla wszystkich wierszy tego samego subfunduszu
- Jezeli PDF zawiera tylko jeden subfundusz, wszystkie wiersze maja ten sam fund_id
"""


@dataclass
class LLMParseResult:
    rows: list[dict] = field(default_factory=list)
    source: str = "llm"
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False
    error: str | None = None


def _cache_storage_path(pdf_hash: str) -> str:
    """Path w buckecie raw-pdfs/cache_llm/{prefix2}/{full_hash}.json"""
    return f"{CACHE_PATH_PREFIX}/{pdf_hash[:2]}/{pdf_hash}.json"


def parse_pdf_with_claude(
    pdf_bytes: bytes,
    pdf_hash: str,
    *,
    model: str = DEFAULT_MODEL,
    use_cache: bool = True,
) -> LLMParseResult:
    """Parsuj PDF przez Claude. Zwraca WSZYSTKIE wiersze (wszystkich subfunduszy).

    Cache po pdf_hash - jezeli PDF byl juz przetworzony, zwraca z cache.

    Filtrowanie do konkretnego fund_id zachodzi w parse_pdfs.py
    (po stronie Python, na liscie rows).
    """
    cache_path = _cache_storage_path(pdf_hash)

    if use_cache:
        cached = _load_cache(cache_path)
        if cached is not None:
            return LLMParseResult(
                rows=cached.get("rows", []),
                source="llm-cache",
                model=cached.get("model"),
                input_tokens=cached.get("input_tokens", 0),
                output_tokens=cached.get("output_tokens", 0),
                cache_hit=True,
            )

    try:
        import anthropic
    except ImportError as e:
        return LLMParseResult(error=f"anthropic package not installed: {e}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return LLMParseResult(error="ANTHROPIC_API_KEY not set")

    # max_retries=8 + timeout=300: Anthropic SDK auto-retry z exp backoff dla 429
    client = anthropic.Anthropic(max_retries=8, timeout=300.0)
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {"type": "text", "text": PROMPT_EXTRACT_ALL},
                ],
            }],
        )
    except Exception as e:
        return LLMParseResult(error=f"Claude API error: {type(e).__name__}: {e}")

    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    rows = _parse_claude_json(text)
    input_tokens = getattr(msg.usage, "input_tokens", 0)
    output_tokens = getattr(msg.usage, "output_tokens", 0)

    if use_cache and rows:
        _save_cache(cache_path, {
            "pdf_hash": pdf_hash,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "rows": rows,
            "cached_at": time.time(),
        })

    return LLMParseResult(
        rows=rows,
        source="llm",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit=False,
    )


def _parse_claude_json(text: str) -> list[dict]:
    """Bezpieczne sparsowanie JSON z odpowiedzi Claude."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []

    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict)]


def _load_cache(cache_path: str) -> dict | None:
    """Zaladuj cached JSON z storage. None jesli nie ma."""
    try:
        from . import storage
    except ImportError:
        import storage
    try:
        raw = storage.download_pdf(cache_path)
        return json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _save_cache(cache_path: str, data: dict) -> None:
    """Zapisz JSON do storage cache."""
    try:
        from . import storage
    except ImportError:
        import storage
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    try:
        storage.upload_pdf(cache_path, raw, upsert=True, content_type="application/json")
    except Exception as e:
        print(f"  [WARN] cache save failed: {e}", flush=True)


def compute_pdf_hash(pdf_bytes: bytes) -> str:
    """SHA256 hex digest pliku PDF."""
    return hashlib.sha256(pdf_bytes).hexdigest()


# =====================================================================
#  TEXT MODE: Claude prompt z tekstem PDFa (po OCR)
#  Massive cost saving vs PDF vision tokens (~35x).
# =====================================================================

PROMPT_EXTRACT_FROM_TEXT = """Ponizej znajduje sie tekst wyciagniety z PDFa raportu sklladu portfela TFI
(format KNF). Tekst zostal sparsowany przez OCR, wiec moze zawierac drobne
bledy rozpoznawania znakow.

Wyciagnij WSZYSTKIE wiersze danych (NIE header, NIE summary, NIE puste wiersze).
Zwroc TYLKO JSON tablica (bez markdown, bez wyjasnien przed/po).

Format obiektu dla kazdego wiersza:
{
  "fund_id": "<identyfikator subfunduszu - np. ING003, PIO048>",
  "subfund_name": "<pelna nazwa subfunduszu>",
  "issuer_name": "<nazwa emitenta>",
  "isin": "<ISIN 12 znakow albo null>",
  "instrument_type": "<EXACT: Obligacje | Akcje | Aktywa Reverse Repo | Zobowiazania Repo | Tytuly i jednostki uczestnictwa | Kontrakt terminowy | Spot-Forward | FX Swap | Swap walutowy | IRS | Gotowka/Depozyty/Naleznosci | Pozyczki papierow wartosciowych>",
  "instrument_category": "<albo null>",
  "issuer_country": "<2-letter, np. PL>",
  "risk_country": "<2-letter albo null>",
  "currency": "<3-letter, np. PLN>",
  "quantity": <float>,
  "value_pln": <float>,
  "weight_assets_pct": <float>,
  "weight_nav_pct": <float (jesli brak, uzyj weight_assets_pct)>,
  "info": "<albo null>"
}

WAZNE:
- Polskie liczby: "2 399,00" -> 2399.00 (spacja=thousands, przecinek=decimal)
- ISIN: 12 znakow PLxxxxxxxxxx / XSxxxxxxxxxx / DExxxxxxxxxx. Inny identyfikator -> null
- OCR czasem mylil znaki: 0/O, 1/I/l, 5/S, 8/B - probuj rozsadnie odgadnac
- Pomijaj wiersze "summary" / "razem" / "podsumowanie"

TEKST PDF:
---
{text}
---
"""


def parse_text_with_claude(
    text: str,
    pdf_hash: str,
    *,
    model: str = DEFAULT_MODEL,
    use_cache: bool = True,
) -> LLMParseResult:
    """Parsuj tekst (po OCR) przez Claude text prompt. ~35x tanszy niz vision PDF.

    Cache key oparty na pdf_hash + sufix '_ocr' zeby nie kolidowal z cache vision.
    """
    cache_key = f"{pdf_hash}_ocr"
    cache_path = f"{CACHE_PATH_PREFIX}/{pdf_hash[:2]}/{cache_key}.json"

    if use_cache:
        cached = _load_cache(cache_path)
        if cached is not None:
            return LLMParseResult(
                rows=cached.get("rows", []),
                source="llm-cache-text",
                model=cached.get("model"),
                input_tokens=cached.get("input_tokens", 0),
                output_tokens=cached.get("output_tokens", 0),
                cache_hit=True,
            )

    try:
        import anthropic
    except ImportError as e:
        return LLMParseResult(error=f"anthropic package not installed: {e}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return LLMParseResult(error="ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(max_retries=8, timeout=180.0)
    prompt = PROMPT_EXTRACT_FROM_TEXT.replace("{text}", text)

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }],
        )
    except Exception as e:
        return LLMParseResult(error=f"Claude API error: {type(e).__name__}: {e}")

    response_text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    rows = _parse_claude_json(response_text)
    input_tokens = getattr(msg.usage, "input_tokens", 0)
    output_tokens = getattr(msg.usage, "output_tokens", 0)

    if use_cache and rows:
        _save_cache(cache_path, {
            "pdf_hash": pdf_hash,
            "mode": "text",
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "rows": rows,
            "cached_at": time.time(),
        })

    return LLMParseResult(
        rows=rows,
        source="llm-text",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit=False,
    )


# =====================================================================
#  CHUNKED MODE: split PDF na strony, kazda strona osobny Claude call
# =====================================================================

def _split_pdf_to_pages(pdf_bytes: bytes) -> list[bytes]:
    """Split PDF na liste bytes per strona (kazda jako pojedynczy PDF)."""
    import io
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(pdf_bytes))
    out: list[bytes] = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        out.append(buf.getvalue())
    return out


def _cache_chunked_page_path(pdf_hash: str, page_idx: int) -> str:
    """Path do cache per (pdf_hash, page_idx)."""
    return f"{CACHE_PATH_PREFIX}/{pdf_hash[:2]}/{pdf_hash}_p{page_idx:03d}.json"


def parse_pdf_with_claude_chunked(
    pdf_bytes: bytes,
    pdf_hash: str,
    *,
    model: str = DEFAULT_MODEL,
    use_cache: bool = True,
    throttle_between_pages: float = 5.0,
) -> LLMParseResult:
    """Parsuj duzy PDF strona po stronie (dla Goldman 5MB, Pekao 18MB).

    Strategia:
      - Split PDF na N pages
      - Per page: oddzielny Claude call (cache per pdf_hash+page_idx)
      - Agregacja wszystkich rows
      - Throttle miedzy stronami (mala pauza zeby uniknac burst rate limit)

    Cache na poziomie page - jesli jedna strona failuje, retry tylko jej.
    """
    pages = _split_pdf_to_pages(pdf_bytes)
    print(f"    chunked mode: {len(pages)} pages, ~{len(pdf_bytes)/(1024*len(pages)):.0f}KB/page", flush=True)

    try:
        import anthropic
    except ImportError as e:
        return LLMParseResult(error=f"anthropic package not installed: {e}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return LLMParseResult(error="ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(max_retries=8, timeout=180.0)
    all_rows: list[dict] = []
    total_input = 0
    total_output = 0
    cache_hits = 0
    errors: list[str] = []

    for page_idx, page_bytes in enumerate(pages):
        cache_path = _cache_chunked_page_path(pdf_hash, page_idx)
        cached = _load_cache(cache_path) if use_cache else None
        if cached is not None:
            page_rows = cached.get("rows", [])
            all_rows.extend(page_rows)
            cache_hits += 1
            print(f"      page {page_idx+1}/{len(pages)}: cache hit ({len(page_rows)} rows)", flush=True)
            continue

        pdf_b64 = base64.standard_b64encode(page_bytes).decode("ascii")
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": PROMPT_EXTRACT_ALL},
                    ],
                }],
            )
        except Exception as e:
            err = f"page {page_idx+1}: {type(e).__name__}: {e}"
            print(f"      ERR {err}", flush=True)
            errors.append(err)
            continue

        text = "".join(b.text for b in msg.content if hasattr(b, "text"))
        page_rows = _parse_claude_json(text)
        in_tok = getattr(msg.usage, "input_tokens", 0)
        out_tok = getattr(msg.usage, "output_tokens", 0)
        total_input += in_tok
        total_output += out_tok
        all_rows.extend(page_rows)
        print(f"      page {page_idx+1}/{len(pages)}: {len(page_rows)} rows [{in_tok}in/{out_tok}out]", flush=True)

        if use_cache and page_rows:
            _save_cache(cache_path, {
                "pdf_hash": pdf_hash,
                "page_idx": page_idx,
                "model": model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "rows": page_rows,
                "cached_at": time.time(),
            })

        if throttle_between_pages > 0 and page_idx < len(pages) - 1:
            time.sleep(throttle_between_pages)

    return LLMParseResult(
        rows=all_rows,
        source="llm-chunked",
        model=model,
        input_tokens=total_input,
        output_tokens=total_output,
        cache_hit=(cache_hits == len(pages) and not errors),
        error="; ".join(errors) if errors else None,
    )
