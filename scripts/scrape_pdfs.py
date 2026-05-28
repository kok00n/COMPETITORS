"""Orchestrator pobierania PDFow ze skladami portfeli z analizy.pl.

Iteruje (parasol_code, report_date) z funds.yaml × month_end_dates, pobiera
PDFy, deduplikuje po pdf_hash, uploaduje do Supabase Storage, upsertuje
portfolio_snapshots (fund_id=NULL - uzupelnia parser w parse_pdfs.py).

Modes:
    backfill    - od --start do --end (zwykle 4 lata wstecz)
    incremental - tylko --date (jedno month-end, default = last month-end)
    refetch     - pobiera ponownie nawet jesli snapshot juz istnieje
                  (do naprawy bledow lub po update'cie parsera)

Usage:
    python scripts/scrape_pdfs.py --mode incremental
    python scripts/scrape_pdfs.py --mode backfill --start 2022-04-30 --end 2026-04-30
    python scripts/scrape_pdfs.py --mode incremental --date 2026-03-31
    python scripts/scrape_pdfs.py --mode incremental --funds PCS05,FOR39
    python scripts/scrape_pdfs.py --mode incremental --dry-run

Wymaga:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY w env
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

CONFIG_PATH = REPO_ROOT / "config" / "funds.yaml"

# Throttle pomiedzy requestami do dokumenty.analizy.pl (sekundy).
# Z testu wczesniej analizy.pl ma sporadyczne timeouty - 1s pauza zmniejsza ryzyko
# rate limiting backendu Symfony. Backfill 39 fundow x 48 dat = ~1900 requestow
# x 1s = 30+ min. Akceptowalne.
DEFAULT_THROTTLE_S = 1.0


def last_month_end(today: date) -> date:
    """Zwroc ostatni month-end <= today. Czyli jak dzis = 2026-05-28 -> 2026-04-30."""
    cur = date(today.year, today.month, 1)
    # Idz wstecz miesiac
    if cur.month == 1:
        prev = date(cur.year - 1, 12, 1)
    else:
        prev = date(cur.year, cur.month - 1, 1)
    last_day = monthrange(prev.year, prev.month)[1]
    return date(prev.year, prev.month, last_day)


def load_funds_to_scrape(funds_filter: set[str] | None = None) -> list[dict]:
    """Czyta funds.yaml, opcjonalnie filtruje po liscie parasol_code."""
    with CONFIG_PATH.open(encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    funds = data.get("funds", []) or []
    if funds_filter:
        funds = [f for f in funds if f.get("parasol_code") in funds_filter]
    return [f for f in funds if f.get("is_active", True)]


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def existing_snapshots(parasol_codes: list[str]) -> dict[tuple[str, str], dict]:
    """Pobierz juz istniejace snapshoty z Supabase do deduplikacji.

    Returns:
        {(parasol_code, 'YYYY-MM-DD'): {scrape_status, pdf_hash, ...}}
    """
    from lib.supabase import select_all
    # Filtrujemy po parasol_code list (in.()  query)
    in_list = ",".join(parasol_codes)
    query = f"?select=parasol_code,report_date,scrape_status,pdf_hash&parasol_code=in.({in_list})"
    rows = select_all("portfolio_snapshots", query)
    return {(r["parasol_code"], r["report_date"]): r for r in rows}


def upsert_snapshot(row: dict) -> None:
    """Upsert pojedynczy snapshot."""
    from lib.supabase import upsert
    upsert("portfolio_snapshots", [row], on_conflict="parasol_code,report_date")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape PDFy ze skladami portfeli z analizy.pl")
    parser.add_argument("--mode", choices=("backfill", "incremental", "refetch"), default="incremental")
    parser.add_argument("--start", type=_parse_date, help="Backfill: start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=_parse_date, help="Backfill: end date (YYYY-MM-DD)")
    parser.add_argument("--date", type=_parse_date, help="Incremental: single date (default = last month-end)")
    parser.add_argument("--funds", type=str, help="Comma-separated parasol_codes filter (np. 'PCS05,FOR39')")
    parser.add_argument("--throttle", type=float, default=DEFAULT_THROTTLE_S,
                        help="Pauza miedzy requestami w sekundach (default 1.0)")
    parser.add_argument("--dry-run", action="store_true", help="Wyswietl plan, nie wykonuj fetch/upload")
    parser.add_argument("--no-storage", action="store_true",
                        help="Pomin upload do Supabase Storage (PDF tylko w base64 lub localfile - debug)")
    args = parser.parse_args()

    # Walidacja args zaleznie od trybu
    if args.mode == "backfill":
        if not args.start or not args.end:
            print("ERR: --mode backfill wymaga --start i --end", file=sys.stderr)
            sys.exit(2)
        from lib.analizy_scraper import month_end_dates
        dates_to_scrape = month_end_dates(args.start, args.end)
    elif args.mode in ("incremental", "refetch"):
        target_date = args.date or last_month_end(date.today())
        dates_to_scrape = [target_date]

    funds_filter: set[str] | None = None
    if args.funds:
        funds_filter = {s.strip() for s in args.funds.split(",") if s.strip()}

    funds = load_funds_to_scrape(funds_filter)
    if not funds:
        print("ERR: brak funduszy do scrapowania", file=sys.stderr)
        sys.exit(1)

    print(f"Tryb:    {args.mode}")
    print(f"Funds:   {len(funds)} ({[f['parasol_code'] for f in funds[:5]]}...)")
    print(f"Daty:    {len(dates_to_scrape)} ({dates_to_scrape[0]} ... {dates_to_scrape[-1]})")
    print(f"Razem:   {len(funds) * len(dates_to_scrape)} prob pobrania")
    print(f"Throttle: {args.throttle}s\n")

    if args.dry_run:
        print("[DRY RUN] - nie wykonuje fetch/upload")
        return

    # Lazy import - lib/supabase.py i lib/storage.py wymagaja SUPABASE_URL/KEY w env.
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(var):
            print(f"ERR: brak env var {var}", file=sys.stderr)
            sys.exit(2)

    from lib.analizy_scraper import fetch_pdf
    import requests
    session = requests.Session()
    session.verify = False

    # Pre-fetch istniejacych snapshotow (dla mode != refetch)
    skip_existing = args.mode != "refetch"
    existing: dict[tuple[str, str], dict] = {}
    if skip_existing:
        try:
            existing = existing_snapshots([f["parasol_code"] for f in funds])
        except Exception as e:
            print(f"  WARN: nie udalo sie pobrac istniejacych snapshotow ({e}), kontynuuje bez deduplikacji")

    stats = {
        "fetched_ok": 0,
        "fetched_not_found": 0,
        "fetched_error": 0,
        "skipped_existing": 0,
        "uploaded": 0,
        "upload_skipped": 0,
        "snapshots_upserted": 0,
    }

    storage_module = None
    if not args.no_storage:
        from lib import storage as storage_module  # noqa: F401

    for d in dates_to_scrape:
        date_str = d.isoformat()
        print(f"\n=== Data {date_str} ===")
        for fund in funds:
            code = fund["parasol_code"]
            key = (code, date_str)
            if skip_existing and key in existing:
                row = existing[key]
                if row["scrape_status"] == "ok" and row.get("pdf_hash"):
                    stats["skipped_existing"] += 1
                    continue

            result = fetch_pdf(code, d, session=session)
            if result.status == "not_found":
                stats["fetched_not_found"] += 1
                # Nie tworzymy snapshotu dla 404 - oszczednosc miejsca, info "no report exists"
                continue
            if result.status == "error":
                stats["fetched_error"] += 1
                # Mozemy zapisac error w snapshocie do debugowania
                upsert_snapshot({
                    "parasol_code": code,
                    "report_date": date_str,
                    "scrape_status": "error",
                    "error_message": result.error_message,
                })
                stats["snapshots_upserted"] += 1
                print(f"  {code}: ERROR HTTP {result.http_status}: {result.error_message[:80]}")
                continue

            # OK - mamy bytes PDFa
            stats["fetched_ok"] += 1
            print(f"  {code}: ok ({result.size_bytes / 1024:.0f} KB, sha={result.sha256[:8]}...)")

            # Upload do Supabase Storage
            pdf_path = None
            if storage_module:
                storage_key = f"{code}/{date_str}.pdf"
                try:
                    pdf_path = storage_module.upload_pdf(storage_key, result.content, upsert=True)
                    stats["uploaded"] += 1
                except Exception as e:
                    print(f"    WARN: upload failed: {e}")

            upsert_snapshot({
                "parasol_code": code,
                "report_date": date_str,
                "scrape_status": "ok",
                "pdf_path": pdf_path,
                "pdf_hash": result.sha256,
                "error_message": None,
            })
            stats["snapshots_upserted"] += 1

            # Throttle
            time.sleep(args.throttle)

    print(f"\n=== PODSUMOWANIE ===")
    for k, v in stats.items():
        print(f"  {k:25} {v}")


if __name__ == "__main__":
    main()
