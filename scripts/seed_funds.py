"""Seed tabeli `funds` + `fund_peer_groups` w Supabase na podstawie
config/funds.yaml.

Idempotentny: kazde uruchomienie upsertuje stan z YAML do bazy. Jesli usuniesz
fundusz z YAML, NIE jest on automatycznie usuwany z bazy (zachowanie defensywne
- jest is_active flag w funds zeby moc 'soft delete'; jak chcesz hard delete,
zrob to recznie w Supabase SQL editor).

Wymaga:
    pip install -r requirements.txt
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_ROLE_KEY=...

Usage:
    python scripts/seed_funds.py                # upsert wszystkich z YAML
    python scripts/seed_funds.py --dry-run      # tylko pokaz co bedzie wyslane
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

# Lokalizujemy lib/supabase.py - import dopiero w main() bo lib/supabase.py
# waliduje SUPABASE_URL/SERVICE_ROLE_KEY na top-level. Dry-run nie wymaga env.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

CONFIG_PATH = REPO_ROOT / "config" / "funds.yaml"


def load_funds_yaml() -> list[dict]:
    with CONFIG_PATH.open(encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    funds = data.get("funds", [])
    if not isinstance(funds, list):
        raise ValueError(f"funds.yaml: 'funds' must be a list, got {type(funds)}")
    return funds


def split_into_tables(funds: list[dict]) -> tuple[list[dict], list[dict]]:
    """Rozdziel wpisy z YAML na 2 rzedy bazodanowe:
    - rows_funds:           wiersze do tabeli `funds`
    - rows_peer_groups:     wiersze do junction `fund_peer_groups`
    """
    rows_funds: list[dict] = []
    rows_peer_groups: list[dict] = []
    seen_codes: set[str] = set()

    for entry in funds:
        code = entry.get("parasol_code")
        if not code:
            print(f"  WARN: skipping entry without parasol_code: {entry}", file=sys.stderr)
            continue
        if code in seen_codes:
            print(f"  WARN: duplicate parasol_code {code} - skipping", file=sys.stderr)
            continue
        seen_codes.add(code)

        rows_funds.append({
            "parasol_code": code,
            "fund_id": entry.get("fund_id"),
            "parasol_name": entry.get("parasol_name"),
            "subfund_name": entry["subfund_name"],
            "tfi_name": entry.get("tfi_name"),
            "analizy_slug": entry.get("analizy_slug", ""),
            "refresh_freq": entry.get("refresh_freq", "monthly"),
            "is_active": entry.get("is_active", True),
            "notes": entry.get("notes"),
        })

        for pg in entry.get("peer_groups", []) or []:
            if pg not in ("fsk", "fod"):
                print(f"  WARN: invalid peer_group '{pg}' for {code} - skipping", file=sys.stderr)
                continue
            rows_peer_groups.append({"parasol_code": code, "peer_group": pg})

    return rows_funds, rows_peer_groups


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed funds + fund_peer_groups do Supabase z config/funds.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Pokaz co bedzie wyslane, nie wysylaj")
    args = parser.parse_args()

    funds_yaml = load_funds_yaml()
    print(f"Wczytano {len(funds_yaml)} funduszy z {CONFIG_PATH.relative_to(REPO_ROOT)}")

    rows_funds, rows_peer_groups = split_into_tables(funds_yaml)
    print(f"Do upsert: funds={len(rows_funds)}, fund_peer_groups={len(rows_peer_groups)}")

    if args.dry_run:
        print("\n[DRY RUN] funds (pierwsze 3):")
        for r in rows_funds[:3]:
            print(f"  {r}")
        print("\n[DRY RUN] fund_peer_groups (pierwsze 5):")
        for r in rows_peer_groups[:5]:
            print(f"  {r}")
        return

    # Sprawdz srodowiskowe SUPABASE_* (lib/supabase.py wymaga ich, podniesie wczesnie blad).
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(var):
            print(f"ERR: brak env var {var}", file=sys.stderr)
            sys.exit(2)

    # Lazy import - lib/supabase.py waliduje env na top-level.
    from lib.supabase import upsert

    # Najpierw funds (klucz FK dla fund_peer_groups).
    posted_funds = upsert("funds", rows_funds, on_conflict="parasol_code")
    print(f"  funds: {posted_funds} wierszy upserted")

    # Junction: kasujemy obecne wpisy dla seedowanych parasol_code i wstawiamy nowe
    # (zeby zmiana z {fsk} -> {fsk,fod} albo wykluczenie z grupy znalazlo odzwierciedlenie).
    # ON CONFLICT DO UPDATE nie wystarczy - musimy zlapac KASOWANIE poprzednich par
    # ktore w nowym stanie nie wystepuja. Robimy to przez ignore-duplicates upsert +
    # nastepnie skrypt mozna ulepszyc o explicit DELETE w razie potrzeby. Na start
    # ignore-duplicates jest OK bo funds.yaml jest authoritative i discover dodaje
    # nowe wiersze, nie usuwa starych.
    posted_pg = upsert(
        "fund_peer_groups",
        rows_peer_groups,
        on_conflict="parasol_code,peer_group",
        ignore_duplicates=True,
    )
    print(f"  fund_peer_groups: {posted_pg} wierszy upserted (ignore-duplicates)")

    print("\nGotowe.")


if __name__ == "__main__":
    main()
