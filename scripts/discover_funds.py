"""Pobiera liste funduszy z 'Grupa porownawcza' przez bezposredni endpoint
analizy.pl: GET /api/competitors/{KOD}? - zwraca JSON z polem 'render' (HTML
tabeli konkurencji). Parsujemy linki + TFI z tego HTML.

Bez Playwright - dziala w GH Actions bez headless browsera.

Usage:
    python scripts/discover_funds.py             # tylko drukuje
    python scripts/discover_funds.py --write     # nadpisuje config/funds.yaml
    python scripts/discover_funds.py --debug     # zapisuje _sample_*.{json,html}

Wymaga:
    pip install -r requirements.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
import urllib3
import yaml

# Firmowy proxy z MITM-em - wylaczamy weryfikacje SSL na requests.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Strony funduszy referencyjnych. API endpoint: /api/competitors/{KOD}?
REFERENCE_FUNDS = {
    "fsk": {
        "ref_code": "PCS05",
        "ref_name": "PKO Obligacji Skarbowych Krótkoterminowy",
        "description": "Fundusze dluzne polskie skarbowe krotkoterminowe",
        "ref_slug": "pko-obligacji-skarbowych-krotkoterminowy",
    },
    "fod": {
        "ref_code": "PCS91",
        "ref_name": "PKO Obligacji Skarbowych Średnioterminowy",
        "description": "Fundusze dluzne polskie skarbowe srednio/dlugoterminowe",
        "ref_slug": "pko-obligacji-skarbowych-srednioterminowy",
    },
}

API_URL = "https://www.analizy.pl/api/competitors/{code}?"

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "funds.yaml"

# Fundusze do wykluczenia z analizy (zwykle wlasne fundusze TFI uzytkownika).
# Jesli analizy.pl doda kolejne PKO fundusze do grup FSK/FOD, dopisz je tutaj.
EXCLUDED_PARASOL_CODES: set[str] = {
    "PCS95",  # PKO Konserwatywny - wlasny fundusz PKO TFI
}

# Pattern URL w polu render: /fundusze-inwestycyjne-otwarte/{KOD}/{slug}
URL_FUND_PATTERN = re.compile(
    r'/fundusze-inwestycyjne-otwarte/([A-Z]{2,5}\d{0,4})/([a-z0-9\-]+)'
)

# TFI wnioskowane po prefiksie nazwy subfunduszu. Wystarczajacy proxy dla startu
# (precyzyjna nazwa zostanie nadpisana z pola Identyfikator/Nazwa funduszu w PDFie).
# Kolejnosc ma znaczenie - dluzsze keyword'y najpierw (np. "Credit Agricole" przed "Credit").
NAME_PREFIX_TO_TFI = [
    ("Allianz", "Allianz Polska TFI"),
    ("Rockbridge", "Rockbridge TFI"),
    ("Erste", "Erste TFI"),
    ("UNIQA", "UNIQA TFI"),
    ("Caspar", "Caspar TFI"),
    ("Investor", "Investors TFI"),
    ("BNP Paribas", "BNP Paribas TFI"),
    ("Goldman Sachs", "Goldman Sachs TFI"),
    ("Pocztowy", "Pocztowy TFI"),
    ("Esaliens", "Esaliens TFI"),
    ("Credit Agricole", "Credit Agricole TFI"),
    ("mFundusz", "Goldman Sachs TFI"),  # mFundusz -> przejety przez Goldman
    ("mBank", "Goldman Sachs TFI"),
    ("ALIOR", "Alior TFI"),
    ("VeloFund", "VeloBank TFI"),
    ("VeloBank", "VeloBank TFI"),
    ("PKO", "PKO TFI"),
    ("Pekao", "Pekao TFI"),
    ("inPZU", "PZU TFI"),
    ("PZU", "PZU TFI"),
    ("QUERCUS", "QUERCUS TFI"),
    ("Skarbiec", "Skarbiec TFI"),
    ("Generali", "Generali Investments TFI"),
    ("SGB", "SGB TFI"),
    ("Noble", "Noble Funds TFI"),
    ("AXA", "AXA TFI"),
]


def infer_tfi(name: str) -> str | None:
    """Zgadnij TFI po pierwszym slowie/keywordzie w nazwie subfunduszu."""
    if not name:
        return None
    for keyword, tfi in NAME_PREFIX_TO_TFI:
        if name.startswith(keyword + " ") or name.startswith(keyword + "/"):
            return tfi
    return None


def fetch_competitors(ref_code: str, ref_slug: str, debug: bool) -> dict:
    """Pobiera JSON z /api/competitors/{ref_code}?

    Backend wymaga sesji (cookies) ustanowionej przez wczesniejsze pobranie
    strony funduszu. Bez tego API ma tendencje do timeoutow.
    """
    session = requests.Session()
    session.verify = False
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    fund_page = f"https://www.analizy.pl/fundusze-inwestycyjne-otwarte/{ref_code}/{ref_slug}"

    # 1. Warmup: pobierz strone funduszu zeby ustanowic sesje (cookies).
    warmup_headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
    }
    session.get(fund_page, headers=warmup_headers, timeout=30)

    # 2. Wlasciwy request - wymaga Referer i XMLHttpRequest header.
    # Backend ma rate limiting / sporadyczne timeouty - retry z backoff.
    url = API_URL.format(code=ref_code)
    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": fund_page,
    }
    last_err = None
    for attempt in range(4):
        try:
            r = session.get(url, headers=headers, timeout=60)
            r.raise_for_status()
            data = r.json()
            break
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            wait = 2 ** attempt  # 1, 2, 4, 8s
            print(f"  retry {attempt + 1}/4 po {wait}s ({type(e).__name__})", flush=True)
            time.sleep(wait)
    else:
        raise RuntimeError(f"Nie udalo sie pobrac {url} po 4 probach: {last_err}")
    if debug:
        sample_path = REPO_ROOT / f"_sample_competitors_{ref_code}.json"
        sample_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  [debug] zapisano JSON do {sample_path.name}", flush=True)
        if "render" in data:
            html_path = REPO_ROOT / f"_sample_competitors_{ref_code}.html"
            html_path.write_text(data["render"], encoding="utf-8")
            print(f"  [debug] zapisano render HTML do {html_path.name}", flush=True)
    return data


def parse_competitors_html(render_html: str, ref_code: str) -> list[dict]:
    """Parsuje pole 'render' (HTML tabeli konkurencji) i wyciaga fundusze.

    Struktura HTML per fundusz (z analizy probki PCS05):
      <a href="/fundusze-inwestycyjne-otwarte/{KOD}/{slug}"
         class="linkDiv" title="Profil funduszu: {NAZWA}"></a>
      <div class="productCell productBasicInfo productCompetitors">
        <h2 class="productName">{NAZWA}</h2>
      </div>

    TFI nie jest w tabeli konkurencji - uzupelnimy z PDFa parasola po pierwszym
    udanym scrape. (Mozna by tez pobrac strone /fundusz/{KOD}, ale to extra 40
    requestow przy starcie - tania pol-automatyzacja jest OK.)
    """
    funds_data: dict[str, dict] = {}

    # Glowny link dla kazdego funduszu konkurencji - linkDiv z title='Profil funduszu: NAZWA'
    link_div_pattern = re.compile(
        r'<a[^>]+href="/fundusze-inwestycyjne-otwarte/([A-Z]{2,5}\d{0,4})/([a-z0-9\-]+)"[^>]*class="linkDiv"[^>]*title="Profil funduszu:\s*([^"]+)"',
    )
    for m in link_div_pattern.finditer(render_html):
        code, slug, name = m.group(1), m.group(2), m.group(3).strip()
        if code == ref_code:
            continue
        funds_data[code] = {"code": code, "slug": slug, "name": name, "tfi": ""}

    # Fallback: linki bez linkDiv (np. ikona porownywania /produkt/fundusz/{KOD}).
    # Czasem jednak nazwy moga byc tylko w <h2 class="productName"> - skanujemy
    # caly HTML pod katem wzorca [link do {KOD}] + nastepny <h2 class="productName">.
    h2_after_link_pattern = re.compile(
        r'/fundusze-inwestycyjne-otwarte/([A-Z]{2,5}\d{0,4})/([a-z0-9\-]+)[^"]*"[^>]*>.*?<h2[^>]+class="productName"[^>]*>([^<]+)</h2>',
        re.DOTALL,
    )
    for m in h2_after_link_pattern.finditer(render_html):
        code, slug, name = m.group(1), m.group(2), m.group(3).strip()
        if code == ref_code:
            continue
        if code not in funds_data:
            funds_data[code] = {"code": code, "slug": slug, "name": name, "tfi": ""}
        elif not funds_data[code]["name"]:
            funds_data[code]["name"] = name

    return sorted(funds_data.values(), key=lambda x: x["code"])


def merge_groups(fsk_results: list[dict], fod_results: list[dict]) -> list[dict]:
    """Laczy fundusze z obu grup - subfund moze byc w obu (np. flagship FOD).

    Klucz: parasol_code. Jesli kod jest w obu grupach, peer_groups = ['fsk', 'fod'].
    """
    merged: dict[str, dict] = {}
    for peer_group, results in [("fsk", fsk_results), ("fod", fod_results)]:
        for r in results:
            code = r["code"]
            if code in EXCLUDED_PARASOL_CODES:
                continue
            if code not in merged:
                merged[code] = {
                    "parasol_code": code,
                    "fund_id": None,  # uzupelnia parser po pierwszym PDFie
                    "parasol_name": None,
                    "subfund_name": r["name"],
                    "tfi_name": r.get("tfi") or infer_tfi(r["name"]),
                    "peer_groups": [peer_group],
                    "refresh_freq": "monthly",
                    "analizy_slug": r["slug"],
                    "notes": None,
                }
            else:
                if peer_group not in merged[code]["peer_groups"]:
                    merged[code]["peer_groups"].append(peer_group)
    return sorted(merged.values(), key=lambda x: x["parasol_code"])


def print_summary(funds: list[dict]) -> None:
    print(f"\n{'=' * 110}")
    print(f"PODSUMOWANIE: znaleziono {len(funds)} unikalnych funduszy")
    print(f"{'=' * 110}")
    fsk_only = [f for f in funds if f["peer_groups"] == ["fsk"]]
    fod_only = [f for f in funds if f["peer_groups"] == ["fod"]]
    both = [f for f in funds if len(f["peer_groups"]) > 1]
    print(f"  tylko FSK: {len(fsk_only)}    tylko FOD: {len(fod_only)}    w obu grupach: {len(both)}")
    print()
    print(f"{'KOD':<10} {'NAZWA':<60} {'TFI':<25} {'GRUPY':<10}")
    print("-" * 110)
    for f in funds:
        groups = "+".join(f["peer_groups"])
        name = (f.get("subfund_name") or "")[:58]
        tfi = (f.get("tfi_name") or "")[:23]
        print(f"{f['parasol_code']:<10} {name:<60} {tfi:<25} {groups:<10}")


def write_yaml(funds: list[dict]) -> None:
    payload = {"funds": funds}
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(
            payload,
            fp,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=200,
        )
    print(f"\nZapisano {len(funds)} funduszy do {CONFIG_PATH.relative_to(REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover peer-group funds via analizy.pl /api/competitors/")
    parser.add_argument("--write", action="store_true", help="Zapisz wynik do config/funds.yaml")
    parser.add_argument("--debug", action="store_true", help="Zapisz _sample_*.json/html dla inspekcji")
    args = parser.parse_args()

    results_by_group: dict[str, list[dict]] = {}
    for peer_group, cfg in REFERENCE_FUNDS.items():
        print(f"\n=== Grupa {peer_group.upper()}: {cfg['description']} ===")
        print(f"  Referencja: {cfg['ref_code']} ({cfg['ref_name']})")
        try:
            data = fetch_competitors(cfg["ref_code"], cfg["ref_slug"], args.debug)
        except requests.HTTPError as e:
            print(f"  ERR fetch competitors: {e}", file=sys.stderr)
            sys.exit(1)

        found = data.get("found", 0)
        grupa = data.get("params", {}).get("grupa", [])
        common_date = data.get("commonDate", "?")
        print(f"  API: found={found} grupa={grupa} commonDate={common_date}")

        render_html = data.get("render", "")
        if not render_html:
            print(f"  WARN: brak pola 'render' w odpowiedzi API")
            results_by_group[peer_group] = []
            continue

        parsed = parse_competitors_html(render_html, cfg["ref_code"])
        print(f"  wyparsowano {len(parsed)} funduszy z pola 'render'")
        results_by_group[peer_group] = parsed

    funds = merge_groups(results_by_group.get("fsk", []), results_by_group.get("fod", []))
    if EXCLUDED_PARASOL_CODES:
        print(f"\nWykluczone (EXCLUDED_PARASOL_CODES): {sorted(EXCLUDED_PARASOL_CODES)}")
    print_summary(funds)
    if args.write:
        write_yaml(funds)
    else:
        print("\nUruchom z --write aby zapisac do config/funds.yaml.")


if __name__ == "__main__":
    main()
