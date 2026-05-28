"""Parser PDFow ze skladami portfeli (format KNF dla TFI).

PDF KNF ma regulacyjny stały layout 18 kolumn:
  [0]  Identyfikator funduszu     -> fund_id          np. 'PKO005'
  [1]  Nazwa funduszu             -> subfund_name     (czesto zlepiona bez spacji)
  [2]  Typ funduszu               -> fund_type        np. 'FIO'
  [3]  Standardowy identifikator  -> standard_fund_id (zwykle 'N/D')
  [4]  Waluta wyceny funduszu     -> fund_valuation_currency
  [5]  Nazwa emitenta             -> issuer_name      (zlepiona bez spacji)
  [6]  Identyfikator instrumentu  -> isin             ('N/D' dla derywatow)
  [7]  Alternatywny identyfikator -> alt_id
  [8]  Typ instrumentu            -> instrument_type
  [9]  Kategoria instrumentu      -> instrument_category
  [10] Kraj emitenta              -> issuer_country
  [11] Kraj ryzyka                -> risk_country
  [12] Waluta instrumentu         -> currency
  [13] Ilosc instrumentow         -> quantity
  [14] Wartosc instrumentu w PLN  -> value_pln
  [15] Udzial w wartosci aktywow  -> weight_assets_pct
  [16] Udzial w NAV (%)           -> weight_nav_pct   ← primary waga do analizy
  [17] Informacje uzupelniajace   -> info

pdfplumber.extract_tables() traktuje kazdy wiersz jako osobna "tabele" (bo PDF ma
horizontal borders) - akumulujemy wszystkie 18-kolumnowe tabele jako wiersze danych.

Naglowek strony 1 zawiera 'Sklad portfela dla funduszu {parasol_name} na dzien {YYYY-MM-DD}'
(zlepiony bez spacji w extract_text(), parsujemy regexem ktory ignoruje whitespace).
"""

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pdfplumber

# Stałe pozycje kolumn (format KNF standard - 18 kolumn).
# Pierwszy wiersz w PDFie zawsze ma te 18 komorek w tej kolejnosci.
COLUMN_INDICES = {
    "fund_id": 0,
    "subfund_name": 1,
    "fund_type": 2,
    "standard_fund_id": 3,
    "fund_valuation_currency": 4,
    "issuer_name": 5,
    "isin": 6,
    "alt_id": 7,
    "instrument_type": 8,
    "instrument_category": 9,
    "issuer_country": 10,
    "risk_country": 11,
    "currency": 12,
    "quantity": 13,
    "value_pln": 14,
    "weight_assets_pct": 15,
    "weight_nav_pct": 16,
    "info": 17,
}

EXPECTED_COLUMNS = 18

# Wartosci traktowane jako pusta komorka (-> None)
NULL_TOKENS = {"", "N/D", "n/d", "—", "-", "–"}

# pdfplumber czesto zwraca instrument_type ze zlepionymi slowami bez spacji
# (bo PDF KNF nie ma 'word spacing' miedzy multi-word headers). Normalizujemy do
# kanonicznych nazw - tym samym matchujemy w bbg_queue WHERE instrument_type IN (...).
#
# Klucz: lowercased + usuniete biale znaki. Wartosc: kanoniczna nazwa.
INSTRUMENT_TYPE_NORMALIZE = {
    "obligacje": "Obligacje",
    "akcje": "Akcje",
    "aktywareverserepo": "Aktywa Reverse Repo",
    "aktywa reverse repo": "Aktywa Reverse Repo",
    "zobowiązaniarepo": "Zobowiązania Repo",
    "zobowiązania repo": "Zobowiązania Repo",
    "zobowiazaniarepo": "Zobowiązania Repo",
    "tytułyijednostkiuczestnictwa": "Tytuły i jednostki uczestnictwa",
    "tytuły i jednostki uczestnictwa": "Tytuły i jednostki uczestnictwa",
    "tytulyijednostkiuczestnictwa": "Tytuły i jednostki uczestnictwa",
    "kontraktterminowy": "Kontrakt terminowy",
    "kontrakt terminowy": "Kontrakt terminowy",
    "spot-forward": "Spot-Forward",
    "fxswap": "FX Swap",
    "fx swap": "FX Swap",
    "swapwalutowy": "Swap walutowy",
    "swap walutowy": "Swap walutowy",
    "irs": "IRS",
    "pożyczkipapierówwartościowych": "Pożyczki papierów wartościowych",
    "pożyczki papierów wartościowych": "Pożyczki papierów wartościowych",
    "pozyczkipapierowwartosciowych": "Pożyczki papierów wartościowych",
    "gotówka/depozyty/należności": "Gotówka/Depozyty/Należności",
    "gotowka/depozyty/naleznosci": "Gotówka/Depozyty/Należności",
}

# fund_id format: 3-6 wielkich liter + cyfry, np. 'PKO005', 'BNP089', 'mFundusz' moze nie pasowac
# - sprawdzamy szerzej.
FUND_ID_RE = re.compile(r"^[A-Z]{2,6}\d{1,5}$")

# Naglowek strony 1: tekst w extract_text() jest zlepiony bez spacji, np.:
#   "SkładportfeladlafunduszuPKOParasolowyFIOnadzień2026-04-30"
# Parsujemy regexem ignorujac biale znaki miedzy slowami.
HEADER_RE = re.compile(
    r"Skład\s*portfela\s*dla\s*funduszu\s*(?P<parasol>.+?)\s*na\s*dzień\s*(?P<date>\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class ParsedPDF:
    parasol_name: str | None = None      # z naglowka strony 1
    report_date: date | None = None      # z naglowka strony 1
    rows: list[dict] = field(default_factory=list)
    pages_count: int = 0
    raw_tables_count: int = 0            # debug
    skipped_rows_count: int = 0          # debug: wiersze ktore nie pasowaly do 18-col layout


def parse_pdf(source: bytes | str | Path) -> ParsedPDF:
    """Parsuj PDF skladu portfela. Zwroc metadata + wszystkie sparsowane wiersze."""
    if isinstance(source, bytes):
        opener = pdfplumber.open(io.BytesIO(source))
    else:
        opener = pdfplumber.open(source)

    result = ParsedPDF()
    with opener as pdf:
        result.pages_count = len(pdf.pages)

        # 1. Naglowek z page 1 (parasol_name, report_date)
        if pdf.pages:
            page1_text = pdf.pages[0].extract_text() or ""
            m = HEADER_RE.search(page1_text)
            if m:
                # Parasol name moze byc bez spacji - 'PKOParasolowyFIO' -> 'PKO Parasolowy FIO'.
                # Nie probujemy poprawiac whitespace tutaj; orchestrator tak czy siak ma
                # parasol_name z funds.yaml jako autorytatywne. Tu trzymamy raw value
                # do crosscheck.
                result.parasol_name = m.group("parasol").strip()
                try:
                    result.report_date = datetime.strptime(m.group("date"), "%Y-%m-%d").date()
                except ValueError:
                    pass

        # 2. Iteruj wszystkie strony, akumuluj 18-kolumnowe wiersze.
        for page in pdf.pages:
            for tbl in (page.extract_tables() or []):
                result.raw_tables_count += 1
                for row in tbl:
                    if not row:
                        continue
                    cells = [_clean_cell(c) for c in row]
                    if len(cells) != EXPECTED_COLUMNS:
                        # Wiersz o innej liczbie kolumn - czasem header strony, czasem
                        # spacja w PDFie. Pomijamy.
                        result.skipped_rows_count += 1
                        continue
                    # Czy to wiersz naglowkowy tabeli? Heurystyka: fund_id (col 0)
                    # zaczyna sie wielkimi literami + cyframi.
                    fund_id_raw = cells[COLUMN_INDICES["fund_id"]]
                    if not _looks_like_fund_id(fund_id_raw):
                        result.skipped_rows_count += 1
                        continue
                    parsed = _parse_data_row(cells)
                    if parsed:
                        result.rows.append(parsed)

    return result


def _clean_cell(c) -> str:
    if c is None:
        return ""
    s = str(c).replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _looks_like_fund_id(s: str) -> bool:
    """fund_id ma format krotkiej alfanumerycznej etykiety, np. PKO005, BNP123."""
    return bool(FUND_ID_RE.match(s))


def _parse_data_row(cells: list[str]) -> dict | None:
    """Parsuj jeden wiersz danych (18-col layout) do slownika."""
    out: dict = {}
    for field_name, idx in COLUMN_INDICES.items():
        raw = cells[idx]
        out[field_name] = None if raw in NULL_TOKENS else raw

    # Wymagaj minimum: fund_id i instrument_type
    if not out.get("fund_id") or not out.get("instrument_type"):
        return None

    # Normalizacja instrument_type - mapuje zlepione warianty na kanoniczne nazwy.
    itype = out["instrument_type"]
    key = re.sub(r"\s", "", itype).lower()
    out["instrument_type"] = INSTRUMENT_TYPE_NORMALIZE.get(key, itype)

    # Numeryczne pola
    for nf in ("quantity", "value_pln", "weight_assets_pct", "weight_nav_pct"):
        out[nf] = _parse_number(out.get(nf))

    # Normalizacja ISIN - usun spacje, upper case
    isin = out.get("isin")
    if isin:
        isin_clean = re.sub(r"\s", "", isin).upper()
        out["isin"] = isin_clean if isin_clean else None

    return out


def _parse_number(s: str | None) -> float | None:
    """Konwertuj string liczbowy z PDFa do float.

    PDFy KNF maja zwykle:
      - kropka jako separator dziesietny: '11173994.3'
      - lub czasem spacja jako thousand separator: '11 173 994.3'
      - 'N/D' / '-' / pusty dla braku danych
    """
    if s is None or s == "":
        return None
    s = s.strip()
    if s in NULL_TOKENS:
        return None
    # Usun bialy znak (thousand separators), zamien przecinek na kropke (na wszelki wypadek)
    cleaned = re.sub(r"\s", "", s).replace(",", ".")
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


# =====================================================================
#  Public helpers do filtrowania / agregacji
# =====================================================================

def filter_rows_by_fund_id(rows: list[dict], fund_id: str) -> list[dict]:
    return [r for r in rows if r.get("fund_id") == fund_id]


def unique_fund_ids(rows: list[dict]) -> list[str]:
    """Lista unikalnych fund_id z parsowanego PDFa, w kolejnosci pierwszego wystapienia."""
    seen: list[str] = []
    for r in rows:
        fid = r.get("fund_id")
        if fid and fid not in seen:
            seen.append(fid)
    return seen


def compute_holdings_hash(rows: list[dict]) -> str:
    """SHA256 holdings_hash - posortowana lista (isin lub issuer_name, weight_nav_pct).

    Stable hash do wykrywania "no change" miedzy snapshotami subfunduszu.
    weight zaokraglony do 4 decimals zeby floating point noise nie zmienial hasha.
    """
    items: list[tuple[str, float]] = []
    for r in rows:
        key = r.get("isin") or r.get("issuer_name") or ""
        weight = r.get("weight_nav_pct")
        weight_rounded = round(weight, 4) if weight is not None else 0.0
        items.append((str(key), weight_rounded))
    items.sort()
    s = "|".join(f"{k}:{w}" for k, w in items)
    return hashlib.sha256(s.encode()).hexdigest()


def aggregate_aum(rows: list[dict]) -> float | None:
    """Suma value_pln (NAV portfela). Trzymamy w portfolio_snapshots.aum_pln."""
    vals = [r["value_pln"] for r in rows if r.get("value_pln") is not None]
    return float(sum(vals)) if vals else None


def aggregate_exposures(rows: list[dict], dimension: str) -> dict[str, float]:
    """Agreguj weight_nav_pct po wymiarze.

    Args:
        rows: holdings dla jednego fund_id
        dimension: 'currency' | 'issuer_country' | 'risk_country' | 'instrument_type'

    Returns:
        {bucket: total_weight_pct}
    """
    field_map = {
        "currency": "currency",
        "issuer_country": "issuer_country",
        "risk_country": "risk_country",
        "instrument_type": "instrument_type",
    }
    field_name = field_map.get(dimension)
    if not field_name:
        raise ValueError(f"Unknown dimension: {dimension}")
    out: dict[str, float] = {}
    for r in rows:
        bucket = r.get(field_name)
        if bucket is None:
            continue
        weight = r.get("weight_nav_pct")
        if weight is None:
            continue
        out[bucket] = out.get(bucket, 0.0) + weight
    return out


def coalesce_pdf_subfund_name(rows: list[dict], fund_id: str) -> str | None:
    """Wyciagnij subfund_name z pierwszego wiersza dla fund_id (do crosscheck)."""
    for r in rows:
        if r.get("fund_id") == fund_id:
            return r.get("subfund_name")
    return None
