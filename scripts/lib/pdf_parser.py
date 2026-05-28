"""Parser PDFow ze skladami portfeli (format KNF dla TFI).

KNF nie wymusza jednego ukladu kolumn - rozne TFI uzywaja roznych szerokosci tabel:

  PKO style (18 kolumn):
    [Identyfikator funduszu] [Nazwa funduszu] [Typ] [Standardowy id] [Waluta wyceny]
    [Nazwa emitenta] [Identyfikator instrumentu] [Alt id] [Typ instrumentu]
    [Kategoria] [Kraj emitenta] [Kraj ryzyka] [Waluta instrumentu] [Ilosc]
    [Wartosc w PLN] [Udzial aktywa %] [Udzial NAV %] [Info]

  BNP Paribas style (16 kolumn):
    [Nazwa subfunduszu] [Typ funduszu] [Standardowy id subfunduszu] [Data wyceny]
    [Waluta wyceny aktywow i zobowiazan funduszu] [Nazwa emitenta]
    [Identyfikator instrumentu - kod ISIN] [Inny niz kod ISIN] [Typ instrumentu]
    [Kategoria] [Kraj emitenta] [Waluta wykorzystywana do wyceny] [Ilosc]
    [Wartosc instrumentu w walucie wyceny funduszu] [Procentowy udzial w Aktywach ogolem]
    [Informacje uzupelniajace]

Strategia parsera:
  1. Iteruj wszystkie tabele ze wszystkich stron.
  2. Znajdz pierwszy wiersz naglowkowy (zawiera 'Nazwa emitenta' albo
     'Identyfikator instrumentu').
  3. Zbuduj header_index: mapping field_name -> column index na podstawie HEADER_MAP.
  4. Iteruj wiersze danych (skip header rows ktore powtarzaja sie na kolejnych
     stronach), parsuj wedlug header_index.
  5. Specjalne reguly:
     - fund_id: jesli brak kolumny "Identyfikator funduszu" (BNP), uzyj
       "Nazwa subfunduszu" jako fund_id (te dwie kolumny zwykle dubluja swoje dane).
     - weight_nav_pct: jesli brak (BNP), uzyj weight_assets_pct jako fallback.
"""

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pdfplumber

# Mapowanie znormalizowanego headera (lowercased, whitespace zwiniety, polskie znaki
# zachowane) -> nasze pole. Headery moga byc wielolinijne ('Nazwa\nsubfunduszu') -
# clean_cell skleja \n na spacje przed lookupem.
HEADER_MAP = {
    # === Pole: fund_id ===
    "identyfikator funduszu": "fund_id",                          # PKO
    "standardowy identyfikator subfunduszu": "fund_id",           # BNP (zwykle pusty - fallback do subfund_name)
    "standardowy identifikator funduszu": "fund_id",              # PKO alternatywna pisownia
    # === Pole: subfund_name ===
    "nazwa funduszu": "subfund_name",                             # PKO
    "nazwa subfunduszu": "subfund_name",                          # BNP (tu jest faktyczny fund_id u BNP, np. BNPP_Akcji_Selektywny)
    # === Pole: fund_type ===
    "typ funduszu": "fund_type",
    "typ subfunduszu": "fund_type",
    # === Pole: report_date_cell (cross-check, nie uzywany jako primary) ===
    "data wyceny": "report_date_cell",
    # === Pole: fund_valuation_currency ===
    "waluta wyceny funduszu": "fund_valuation_currency",
    "waluta wyceny aktywów i zobowiązań funduszu": "fund_valuation_currency",
    "waluta wyceny aktywow i zobowiazan funduszu": "fund_valuation_currency",
    "waluta wyceny": "fund_valuation_currency",                   # short form
    # === Pole: issuer_name ===
    "nazwa emitenta": "issuer_name",
    # === Pole: isin ===
    "identyfikator instrumentu": "isin",                          # PKO
    "identyfikator instrumentu - kod isin": "isin",               # BNP
    # === Pole: alt_id ===
    "alternatywny identyfikator": "alt_id",                       # PKO
    "inny niż kod isin standardowy identyfikator instrumentu": "alt_id",  # BNP
    "inny niz kod isin standardowy identyfikator instrumentu": "alt_id",
    # === Pole: instrument_type ===
    "typ instrumentu": "instrument_type",
    # === Pole: instrument_category ===
    "kategoria instrumentu": "instrument_category",
    # === Pole: issuer_country ===
    "kraj emitenta": "issuer_country",
    # === Pole: risk_country (rzadkie - tylko PKO) ===
    "kraj ryzyka": "risk_country",
    # === Pole: currency ===
    "waluta instrumentu": "currency",                             # PKO
    "waluta wykorzystywana do wyceny instrumentu": "currency",    # BNP
    # === Pole: quantity ===
    "ilość instrumentów w portfelu": "quantity",                  # PKO (mnogi)
    "ilość instrumentu w portfelu": "quantity",                   # BNP (pojedynczy)
    "ilosc instrumentow w portfelu": "quantity",
    "ilosc instrumentu w portfelu": "quantity",
    # === Pole: value_pln ===
    "wartość instrumentu w pln": "value_pln",                     # PKO
    "wartość instrumentu w walucie wyceny funduszu": "value_pln", # BNP (dla PLN-funduszy = PLN)
    "wartosc instrumentu w pln": "value_pln",
    "wartosc instrumentu w walucie wyceny funduszu": "value_pln",
    # === Pole: weight_assets_pct ===
    "udział w wartości aktywów ogółem (%)": "weight_assets_pct",  # PKO (z (%))
    "udzial w wartosci aktywow ogolem (%)": "weight_assets_pct",
    "procentowy udział w aktywach ogółem": "weight_assets_pct",   # BNP (bez (%))
    "procentowy udzial w aktywach ogolem": "weight_assets_pct",
    # === Pole: weight_nav_pct (tylko PKO ma osobno, BNP nie ma) ===
    "udział w nav (%)": "weight_nav_pct",
    "udzial w nav (%)": "weight_nav_pct",
    # === Pole: info ===
    "informacje uzupełniające": "info",
    "informacje uzupelniajace": "info",
}

# Wartosci traktowane jako pusta komorka (-> None)
NULL_TOKENS = {"", "N/D", "n/d", "—", "-", "–"}

# pdfplumber zwraca instrument_type czasem ze zlepionymi slowami bez spacji.
# Normalizacja do kanonicznych nazw - matchujemy w bbg_queue WHERE instrument_type IN (...).
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

# Naglowek strony 1 (PKO ma w extract_text, BNP nie ma): 'Sklad portfela dla funduszu X na dzien YYYY-MM-DD'.
HEADER_RE = re.compile(
    r"Skład\s*portfela\s*dla\s*funduszu\s*(?P<parasol>.+?)\s*na\s*dzień\s*(?P<date>\d{4}-\d{2}-\d{2})",
    re.IGNORECASE | re.DOTALL,
)

# Heurystyka wykrycia wiersza naglowkowego: zawiera 'Nazwa emitenta' albo
# 'Identyfikator instrumentu' (te wystepuja w obu stylach PKO i BNP).
HEADER_DETECTION_TOKENS = ("nazwa emitenta", "identyfikator instrumentu", "nazwa subfunduszu")

# Fallback header dla PKO-style PDFow gdzie naglowek NIE jest w extract_tables()
# (pdfplumber zwraca tylko wiersze danych, header jest jako float text na page 1).
# Uzywany kiedy wszystkie wiersze maja dokladnie 18 kolumn ale zaden nie jest headerem.
PKO_FIXED_HEADER_18 = [
    "Identyfikator funduszu",
    "Nazwa funduszu",
    "Typ funduszu",
    "Standardowy identifikator funduszu",
    "Waluta wyceny funduszu",
    "Nazwa emitenta",
    "Identyfikator instrumentu",
    "Alternatywny identyfikator",
    "Typ instrumentu",
    "Kategoria instrumentu",
    "Kraj emitenta",
    "Kraj ryzyka",
    "Waluta instrumentu",
    "Ilość instrumentów w portfelu",
    "Wartość instrumentu w PLN",
    "Udział w wartości aktywów ogółem (%)",
    "Udział w NAV (%)",
    "Informacje uzupełniające",
]


@dataclass
class ParsedPDF:
    parasol_name: str | None = None
    report_date: date | None = None
    rows: list[dict] = field(default_factory=list)
    pages_count: int = 0
    raw_tables_count: int = 0
    raw_rows_count: int = 0
    skipped_rows_count: int = 0
    header_columns: list[str] = field(default_factory=list)
    header_index: dict[str, int] = field(default_factory=dict)
    layout_style: str | None = None     # 'pko' (18-col), 'bnp' (16-col), 'unknown'


def parse_pdf(source: bytes | str | Path) -> ParsedPDF:
    """Parsuj PDF skladu portfela. Zwroc metadata + wszystkie sparsowane wiersze.

    Obsluguje wiele layoutow KNF - header-based column mapping.
    """
    if isinstance(source, bytes):
        opener = pdfplumber.open(io.BytesIO(source))
    else:
        opener = pdfplumber.open(source)

    result = ParsedPDF()
    with opener as pdf:
        result.pages_count = len(pdf.pages)

        # 1. Probuj wyciagnac naglowek (parasol_name, report_date) z tekstu pierwszej strony.
        if pdf.pages:
            page1_text = pdf.pages[0].extract_text() or ""
            m = HEADER_RE.search(page1_text)
            if m:
                result.parasol_name = m.group("parasol").strip()
                try:
                    result.report_date = datetime.strptime(m.group("date"), "%Y-%m-%d").date()
                except ValueError:
                    pass

        # 2. Akumuluj wszystkie tabele.
        all_tables: list[list[list]] = []
        for page in pdf.pages:
            tables = page.extract_tables() or []
            all_tables.extend(tables)
            result.raw_tables_count += len(tables)
            for tbl in tables:
                result.raw_rows_count += len(tbl)

        if not all_tables:
            return result

        # 3. Znajdz wiersz naglowkowy. Pierwsza tabela ma zwykle header w wierszu [0],
        #    ale czasem tabel jest mnogo i header jest w innym miejscu - skanujemy
        #    wszystkie tabele liniowo.
        header_row: list | None = None
        for tbl in all_tables:
            for row in tbl:
                if _is_header_row(row):
                    header_row = row
                    break
            if header_row:
                break

        # Fallback dla PKO: brak header w tabelach (pdfplumber traktuje kazdy wiersz
        # jako osobna tabele 1-wierszowa). Jesli wiekszosc wierszy ma 18 komorek,
        # uzywamy PKO_FIXED_HEADER_18 jako defaultowy header.
        if not header_row:
            cells_18_count = sum(
                1 for tbl in all_tables for row in tbl
                if row and len(row) == 18
            )
            if cells_18_count > 0 and cells_18_count / max(result.raw_rows_count, 1) > 0.9:
                header_row = PKO_FIXED_HEADER_18

        if not header_row:
            return result

        cleaned_header = [_clean_cell(c) for c in header_row]
        result.header_columns = cleaned_header
        result.header_index = _build_header_index(cleaned_header)

        # 4. Wykryj layout style (do debugowania)
        if len(cleaned_header) == 18 and "fund_id" in result.header_index:
            # Sprawdzamy ze fund_id wskazuje na 'identyfikator funduszu' (PKO) a nie
            # 'standardowy identyfikator subfunduszu' (BNP).
            if result.header_index.get("fund_id") == 0:
                result.layout_style = "pko"
        elif len(cleaned_header) == 16 and "subfund_name" in result.header_index:
            result.layout_style = "bnp"
        else:
            result.layout_style = "unknown"

        # 5. Iteruj wszystkie wiersze, pomijaj headery, parsuj dane.
        for tbl in all_tables:
            for row in tbl:
                if not row:
                    continue
                cells = [_clean_cell(c) for c in row]
                if all(c == "" for c in cells):
                    continue
                if _is_header_row(row):
                    continue
                if len(cells) != len(cleaned_header):
                    # Wiersz o innej liczbie kolumn - pomijamy, ale nie liczymy jako blad.
                    # Czasami pdfplumber rozdziela jeden wiersz na 2 po naprawie tabeli.
                    result.skipped_rows_count += 1
                    continue
                parsed = _parse_data_row(cells, result.header_index)
                if parsed:
                    result.rows.append(parsed)

    return result


def _is_header_row(row) -> bool:
    """Wiersz to header jesli zawiera jeden z HEADER_DETECTION_TOKENS."""
    if not row:
        return False
    text = " ".join(str(c or "").replace("\n", " ").lower() for c in row)
    return any(tok in text for tok in HEADER_DETECTION_TOKENS)


def _clean_cell(c) -> str:
    if c is None:
        return ""
    s = str(c).replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip()


def _build_header_index(cleaned_header: list[str]) -> dict[str, int]:
    """Mapuj field_name -> column index na podstawie naglowkow w PDFie."""
    out: dict[str, int] = {}
    for idx, cell in enumerate(cleaned_header):
        norm = cell.lower().strip()
        field_name = HEADER_MAP.get(norm)
        if field_name and field_name not in out:
            out[field_name] = idx
    return out


def _parse_data_row(cells: list[str], header_to_idx: dict[str, int]) -> dict | None:
    """Parsuj jeden wiersz danych do slownika wedlug header_to_idx."""
    out: dict = {}
    for field_name, idx in header_to_idx.items():
        if idx >= len(cells):
            out[field_name] = None
            continue
        raw = cells[idx]
        out[field_name] = None if raw in NULL_TOKENS else raw

    # Fallback: jesli brak fund_id ale jest subfund_name (BNP layout), uzyj subfund_name.
    # BNP wpisuje np. 'BNPP_Akcji_Selektywny' jako nazwa subfunduszu - traktujemy to jako fund_id.
    if not out.get("fund_id") and out.get("subfund_name"):
        out["fund_id"] = out["subfund_name"]

    # Wymagaj minimum: fund_id i instrument_type
    if not out.get("fund_id") or not out.get("instrument_type"):
        return None

    # Normalizacja instrument_type
    itype = out["instrument_type"]
    key = re.sub(r"\s", "", itype).lower()
    out["instrument_type"] = INSTRUMENT_TYPE_NORMALIZE.get(key, itype)

    # Numeryczne pola
    for nf in ("quantity", "value_pln", "weight_assets_pct", "weight_nav_pct"):
        out[nf] = _parse_number(out.get(nf))

    # Fallback weight_nav_pct = weight_assets_pct (BNP nie ma osobnego udzial w NAV;
    # roznica miedzy aktywa a NAV dla bond-funduszu jest zwykle <5%).
    if out.get("weight_nav_pct") is None and out.get("weight_assets_pct") is not None:
        out["weight_nav_pct"] = out["weight_assets_pct"]

    # Normalizacja ISIN
    isin = out.get("isin")
    if isin:
        isin_clean = re.sub(r"\s", "", isin).upper()
        out["isin"] = isin_clean if isin_clean else None

    return out


def _parse_number(s: str | None) -> float | None:
    """Konwertuj string liczbowy z PDFa do float.

    Obsluguje dwa formaty:
      - PKO style:   '11173994.3' albo '11 173 994.3' (kropka decimal, spacja thousand)
      - BNP style:   '2 399,00' albo '78 207,40' (przecinek decimal, spacja thousand)
    """
    if s is None or s == "":
        return None
    s = s.strip()
    if s in NULL_TOKENS:
        return None
    # Usun whitespace (thousand separators)
    cleaned = re.sub(r"\s", "", s)
    # Jesli sa przecinki i kropki: prawdopodobnie kropka=thousand, przecinek=decimal (rzadkie)
    # Najprostsze: jezeli ostatni separator to przecinek, zamien przecinek -> kropka.
    # Inaczej zostaw kropke.
    if "," in cleaned and "." in cleaned:
        # Mieszane - patrzymy na ostatnia pozycje
        if cleaned.rfind(",") > cleaned.rfind("."):
            # przecinek to decimal
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            # kropka to decimal
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Tylko przecinek - to decimal separator
        cleaned = cleaned.replace(",", ".")
    # Jesli tylko kropka - juz OK
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
    seen: list[str] = []
    for r in rows:
        fid = r.get("fund_id")
        if fid and fid not in seen:
            seen.append(fid)
    return seen


def compute_holdings_hash(rows: list[dict]) -> str:
    """SHA256 holdings_hash - posortowana lista (isin lub issuer_name, weight_nav_pct).

    Stable hash do wykrywania "no change" miedzy snapshotami subfunduszu.
    Weight zaokraglony do 4 decimals zeby floating point noise nie zmienial hasha.
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
    vals = [r["value_pln"] for r in rows if r.get("value_pln") is not None]
    return float(sum(vals)) if vals else None


def aggregate_exposures(rows: list[dict], dimension: str) -> dict[str, float]:
    """Agreguj weight_nav_pct po wymiarze (currency / issuer_country / risk_country / instrument_type)."""
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
