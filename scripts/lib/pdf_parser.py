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
#
# Wartosc 'parasol_name_cell' / 'fund_alt_id' / 'report_date_cell' nie mapuje na
# kolumne w DB - tylko crosscheck/debug.
HEADER_MAP = {
    # === Pole: fund_id ===
    "identyfikator funduszu": "fund_id",                          # PKO
    "standardowy identyfikator subfunduszu": "fund_id",           # BNP/Erste/Skarbiec (czesto pusty - fallback do subfund_name)
    "standardowy identifikator funduszu": "fund_id",              # PKO alternatywna pisownia
    "oznaczenie izfia": "fund_id",                                # Allianz (np. PLSFIO00208)
    "knf_id": "fund_id",                                          # Goldman Sachs
    "identyfikator krajowy": "fund_id",                           # IPOPEMA/Pocztowy
    "identyfikator izfia funduszu lub subfunduszu": "fund_id",    # ALIOR/PZU
    "kod izfia": "fund_id",                                       # VeloBank/NOB
    "identyfikator funduszu lub subfunduszu": "fund_id",          # Skarbiec
    "identyfikator subfunduszu": "fund_id",                       # Generali/SGB
    # === Pole: subfund_name ===
    "nazwa funduszu": "subfund_name",                             # PKO + Rockbridge (gdy brak 'nazwa subfunduszu' - resolved kontekstowo)
    "nazwa subfunduszu": "subfund_name",                          # BNP/Allianz/ALIOR/VeloBank/PZU/Skarbiec/Erste
    "nazwa funduszu / nazwa subfunduszu": "subfund_name",         # Goldman Sachs
    "nazwa funduszu/subfunduszu": "subfund_name",                 # Allianz alt
    "nazwa funduszu / subfunduszu": "subfund_name",               # IPOPEMA/Generali
    # === Pole: parasol_name_cell (debug, nie w DB) ===
    "pełna nazwa funduszu": "parasol_name_cell",                  # Erste/Skarbiec
    "nazwa parasola": "parasol_name_cell",                        # Goldman Sachs
    # === Pole: fund_type ===
    "typ": "fund_type",                                           # Goldman Sachs, IPOPEMA (krotkie)
    "typ funduszu": "fund_type",
    "typ subfunduszu": "fund_type",
    # === Pole: fund_alt_id (alternatywny ID na poziomie funduszu, nie w DB osobno) ===
    "inny identyfikator funduszu": "fund_alt_id",                 # ALIOR/PZU
    "isin funduszu (id krajowy)": "fund_alt_id",                  # VeloBank
    "izfdia_i": "fund_alt_id",                                    # Goldman Sachs
    # === Pole: report_date_cell (debug) ===
    "data wyceny": "report_date_cell",                            # BNP
    "data": "report_date_cell",                                   # Rockbridge
    # === Pole: fund_valuation_currency ===
    "waluta wyceny funduszu": "fund_valuation_currency",
    "waluta wyceny aktywów i zobowiązań funduszu": "fund_valuation_currency",
    "waluta wyceny aktywow i zobowiazan funduszu": "fund_valuation_currency",
    "waluta wyceny aktywów i zobowiązań subfunduszu": "fund_valuation_currency",  # Generali
    "waluta wyceny": "fund_valuation_currency",                   # Allianz short
    "waluta wyceny fund.": "fund_valuation_currency",             # VeloBank short
    # === Pole: issuer_name ===
    "nazwa emitenta": "issuer_name",
    "emitent": "issuer_name",                                     # Allianz/ALIOR/PZU
    "nazwa pełna instrumentu": "issuer_name",                     # Goldman Sachs
    "nazwa instrumentu": "issuer_name",                           # Rockbridge (compound z "nazwa emitenta")
    # === Pole: isin ===
    "identyfikator instrumentu": "isin",                          # PKO
    "identyfikator instrumentu - kod isin": "isin",               # BNP/Erste
    "identyfikator instrumentu (kod isin)": "isin",               # Skarbiec/Generali
    "kod isin instrumentu": "isin",                               # Allianz/ALIOR/ESALIENS/PZU
    "kod isin": "isin",
    "isin instrumentu": "isin",                                   # VeloBank
    "isin": "isin",                                               # Goldman Sachs/IPOPEMA short
    "kod instrumentu": "isin",                                    # Rockbridge
    # === Pole: alt_id ===
    "alternatywny identyfikator": "alt_id",                       # PKO
    "inny niż kod isin standardowy identyfikator instrumentu": "alt_id",  # BNP/Erste/Skarbiec
    "inny niz kod isin standardowy identyfikator instrumentu": "alt_id",
    "inny standardowy identyfikator instrumentu": "alt_id",       # PZU
    "inne identyfikatory instrumentu": "alt_id",                  # Allianz
    "inne id instr.": "alt_id",                                   # VeloBank
    "dostępny, standardowy identyfikatory instrumentu (inny niż kod isin)": "alt_id",  # Generali
    "nazwa skrócona instrumentu": "alt_id",                       # Goldman Sachs
    # === Pole: instrument_type ===
    "typ instrumentu": "instrument_type",
    "kategoria / typ instrumentu": "instrument_type",             # Goldman Sachs
    "typ\ninstrumentu": "instrument_type",
    "kategoria": "instrument_type",                               # Rockbridge (jednoslowo) - kontrastowo z 'kategoria instrumentu'
    # === Pole: instrument_category ===
    "kategoria instrumentu": "instrument_category",
    "klasyfikacja eiopa": "instrument_category",                  # Allianz (Solvency II klasyfikacja)
    # === Pole: issuer_country ===
    "kraj emitenta": "issuer_country",
    "kod kraju emitenta": "issuer_country",                       # IPOPEMA
    # === Pole: risk_country (rzadkie - tylko PKO) ===
    "kraj ryzyka": "risk_country",
    # === Pole: currency ===
    "waluta instrumentu": "currency",                             # PKO
    "waluta wykorzystywana do wyceny instrumentu": "currency",    # BNP/ESALIENS
    "waluta wyceny instr.": "currency",                           # VeloBank
    "waluta": "currency",                                         # Rockbridge/Generali (krotkie - wymaga kontekstu)
    "waalut": "currency",                                         # Goldman Sachs (typo)
    # === Pole: quantity ===
    "ilość instrumentów w portfelu": "quantity",                  # PKO (mnogi)
    "ilość instrumentu w portfelu": "quantity",                   # BNP (pojedynczy)
    "ilosc instrumentow w portfelu": "quantity",
    "ilosc instrumentu w portfelu": "quantity",
    "ilość instr.": "quantity",                                   # VeloBank
    "ilość": "quantity",                                          # IPOPEMA/Generali (krotkie)
    "liczba": "quantity",                                         # Rockbridge
    # === Pole: value_pln ===
    "wartość instrumentu w pln": "value_pln",                     # PKO
    "wartość instrumentu w walucie wyceny funduszu": "value_pln", # BNP/ESALIENS (dla PLN-fundu = PLN)
    "wartosc instrumentu w pln": "value_pln",
    "wartosc instrumentu w walucie wyceny funduszu": "value_pln",
    "wartość instrumentu": "value_pln",                           # IPOPEMA
    "wartość instr.": "value_pln",                                # VeloBank
    "wartość": "value_pln",                                       # Rockbridge/Generali (krotkie)
    # === Pole: weight_assets_pct ===
    "udział w wartości aktywów ogółem (%)": "weight_assets_pct",  # PKO
    "udzial w wartosci aktywow ogolem (%)": "weight_assets_pct",
    "procentowy udział w aktywach ogółem": "weight_assets_pct",   # BNP
    "procentowy udzial w aktywach ogolem": "weight_assets_pct",
    "procentowy": "weight_assets_pct",                            # Generali (krotki nieintuicyjny)
    "udział": "weight_assets_pct",                                # Rockbridge (krotki)
    "udział w portfelu [%]": "weight_nav_pct",                    # IPOPEMA (faktycznie NAV-based)
    # === Pole: weight_nav_pct (tylko PKO ma osobno, reszta uzywa fallback z weight_assets_pct) ===
    "udział w nav (%)": "weight_nav_pct",
    "udzial w nav (%)": "weight_nav_pct",
    # === Pole: info ===
    "informacje uzupełniające": "info",
    "informacje uzupelniajace": "info",
    "instrument bazowy": "info",                                  # Rockbridge
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

# Heurystyka wykrycia wiersza naglowkowego: zawiera ktorys z tych tokenow.
# Pokrywa PKO, BNP, Allianz, Goldman Sachs, IPOPEMA, ESALIENS, ALIOR, VeloBank,
# PZU, Skarbiec, Generali, Rockbridge layouts.
HEADER_DETECTION_TOKENS = (
    "nazwa emitenta",
    "identyfikator instrumentu",
    "nazwa subfunduszu",
    "nazwa funduszu / nazwa subfunduszu",
    "nazwa funduszu / subfunduszu",
    "nazwa funduszu/subfunduszu",
    "emitent",
    "kod isin",
    "isin instrumentu",
    "knf_id",
    "kod izfia",
    "oznaczenie izfia",
    "identyfikator subfunduszu",
    "identyfikator funduszu lub subfunduszu",
    "identyfikator izfia",
    "nazwa instrumentu",     # Rockbridge
    "kod instrumentu",        # Rockbridge
)

# Minimum N zmapowanych pol w header_index zeby uznac wykryty header za prawdziwy.
# Jesli mniej, prawdopodobnie to junk row (np. Skarbiec sklejony header w cell[0]) -
# fallback to PKO/SKARBIEC fixed headers.
MIN_HEADER_INDEX_FIELDS = 5

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

# Fallback header dla Skarbiec/PZU/ALIOR-style PDFow (17 kolumn).
# Header w extract_text() jako float, w tabelach jest pierwszy wiersz z junk text
# rozdzielonym przez \n (multi-line cell). Drugi wiersz to juz dane.
SKARBIEC_FIXED_HEADER_17 = [
    "Identyfikator funduszu lub subfunduszu",
    "Pełna nazwa funduszu",
    "Nazwa subfunduszu",
    "Typ funduszu",
    "Standardowy identyfikator subfunduszu",
    "Waluta wyceny aktywów i zobowiązań funduszu",
    "Nazwa emitenta",
    "Identyfikator instrumentu (kod ISIN)",
    "Inny niż kod ISIN standardowy identyfikator instrumentu",
    "Typ instrumentu",
    "Kategoria instrumentu",
    "Kraj emitenta",
    "Waluta wykorzystywana do wyceny instrumentu",
    "Ilość instrumentu w portfelu",
    "Wartość instrumentu w walucie wyceny funduszu",
    "Procentowy udział w Aktywach ogółem",
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

        # Sprawdz czy detected header daje sensowny header_index. Jesli nie (junk row,
        # cell[0] sklejony z calym tekstem) - odrzuc i sprobuj fallback.
        def _try_header(row):
            cleaned = [_clean_cell(c) for c in row]
            idx = _build_header_index(cleaned)
            return cleaned, idx

        cleaned_header: list[str] = []
        header_index: dict[str, int] = {}
        if header_row:
            cleaned_header, header_index = _try_header(header_row)
            if len(header_index) < MIN_HEADER_INDEX_FIELDS:
                # Header zbyt biedny - reject, sprobujemy fallback
                header_row = None
                cleaned_header = []
                header_index = {}

        # Fallback dla PDFow gdzie naglowek NIE jest w extract_tables() albo header_row
        # byl junk (PKO, Skarbiec): wykrywamy po dominujacej liczbie kolumn:
        #   - 18 col → PKO_FIXED_HEADER_18
        #   - 17 col → SKARBIEC_FIXED_HEADER_17
        if not header_row:
            cells_18 = sum(1 for tbl in all_tables for row in tbl if row and len(row) == 18)
            cells_17 = sum(1 for tbl in all_tables for row in tbl if row and len(row) == 17)
            total = max(result.raw_rows_count, 1)
            if cells_18 / total > 0.5:
                header_row = PKO_FIXED_HEADER_18
            elif cells_17 / total > 0.5:
                header_row = SKARBIEC_FIXED_HEADER_17
            if header_row:
                cleaned_header, header_index = _try_header(header_row)

        if not header_row or not header_index:
            return result

        result.header_columns = cleaned_header
        result.header_index = header_index

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
    """Mapuj field_name -> column index na podstawie naglowkow w PDFie.

    Kontekst-aware: rozwiazuje konflikt 'Nazwa funduszu' = parasol vs subfundusz.
    Jesli w PDFie istnieje kolumna 'Nazwa subfunduszu' lub 'Nazwa funduszu/subfunduszu'
    (ALIOR/PZU/VeloBank/Skarbiec), to 'Nazwa funduszu' oznacza nazwe parasola, nie
    subfunduszu. Inaczej (Rockbridge) 'Nazwa funduszu' = subfund_name (compound
    parasol+subfund w jednym polu).
    """
    normalized = [c.lower().strip() for c in cleaned_header]
    norm_set = set(normalized)

    # Czy istnieje wyraznie nazwa subfunduszu w innej kolumnie?
    has_explicit_subfund = any(h in norm_set for h in (
        "nazwa subfunduszu",
        "nazwa funduszu / nazwa subfunduszu",
        "nazwa funduszu/subfunduszu",
        "nazwa funduszu / subfunduszu",
    ))

    out: dict[str, int] = {}
    for idx, cell in enumerate(cleaned_header):
        norm = cell.lower().strip()
        if not norm:
            continue

        # Special case: 'nazwa funduszu' (sole) - context-dependent
        if norm == "nazwa funduszu":
            if has_explicit_subfund:
                # ALIOR/PZU/VeloBank: 'Nazwa funduszu' = parasol
                out.setdefault("parasol_name_cell", idx)
            else:
                # Rockbridge: jedyna kolumna z nazwa - traktujemy jako subfund_name
                out.setdefault("subfund_name", idx)
            continue

        field_name = HEADER_MAP.get(norm)
        if field_name:
            out.setdefault(field_name, idx)
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
