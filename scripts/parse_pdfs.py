"""Orchestrator parsowania PDFow ze skladami portfeli.

SELECT pending snapshots (scrape_status='ok' AND holdings_count IS NULL),
pobiera PDF z Supabase Storage, parsuje, znajduje fund_id po fuzzy match z
subfund_name z funds.yaml, INSERT holdings + exposures, UPDATE snapshot.

Idempotentne: jesli re-parsowanie tego samego snapshotu, najpierw DELETE
existing holdings + exposures dla tego snapshot_id.

Usage:
    python scripts/parse_pdfs.py                    # parsuje wszystkie pending
    python scripts/parse_pdfs.py --parasol PCS05    # tylko ten parasol
    python scripts/parse_pdfs.py --date 2026-04-30  # tylko ta data
    python scripts/parse_pdfs.py --force            # re-parsuje juz sparsowane
    python scripts/parse_pdfs.py --limit 10         # tylko N snapshotow (test)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

CONFIG_PATH = REPO_ROOT / "config" / "funds.yaml"

# Prefixy do strip z subfund_name przed fuzzy match. Pierwsze sa typu generycznego
# (Subfundusz, Fundusz, FIO/SFIO) - sluza do nazwy bez TFI prefix. Reszta to
# konkretne TFI prefixy (Allianz, Rockbridge, PKO, etc.) - usuwamy je zeby fuzzy
# match dzialal po rebrandingu (np. Erste→Santander) i dla porownania nazw
# w PDFie ('SubfunduszX') vs analizy.pl ('TFIName X').
#
# Kolejnosc ma znaczenie - dluzsze keyword'y najpierw zeby "credit agricole" nie
# byl strippowany jako "credit" + "agricole" osobno. Po _normalize_name (lower +
# remove [\s_]) wszystkie nazwy sa concat-ami.
NAME_PREFIXES_TO_STRIP = [
    # Generic
    "parasolowy",       # Rockbridge: 'ROCKBRIDGE FIO Parasolowy Rockbridge Subfundusz X'
    "parasol",
    "subfundusz",
    "fundusz",
    # TFI prefixy (dluzsze pierwsze)
    "creditagricole",
    "goldmansachs",
    "bnpparibas",
    "rockbridge",
    "investors",
    "investor",
    "skarbiec",
    "generali",
    "santander",
    "allianz",
    "quercus",
    "esaliens",
    "ipopema",
    "pocztowy",
    "velofund",
    "velobank",
    "caspar",
    "alior",
    "uniqa",
    "noble",
    "erste",
    "prestiz",
    "prestiż",
    "pekao",
    "bnpp",
    "inpzu",
    "pzu",
    "pko",
    "bnp",
    "ing",
    "axa",
    "sgb",
    # Common qualifiers po prefixie TFI
    "fio",
    "sfio",
]


def _normalize_name(s: str) -> str:
    """Lowercased, usuniete biale znaki + underscores + iterative strip prefixow.

    Underscores wystepuja w BNP layout (fund_id = 'BNP_Paribas_Obligacji_Skarbowych').
    Whitespace usuwamy bo pdfplumber czasem rozdziela slowa spacjami losowo.

    Iterative strip - usuwamy WIELE prefiksow z rzedu (np. 'AllianzSubfunduszAkcji'
    → 'Akcji' po strip 'allianz' + 'subfundusz').
    """
    if not s:
        return ""
    s = re.sub(r"[\s_/.,&-]+", "", s).lower()
    changed = True
    while changed:
        changed = False
        for prefix in NAME_PREFIXES_TO_STRIP:
            if s.startswith(prefix):
                s = s[len(prefix):]
                changed = True
                break
    return s


def find_fund_id_in_pdf(parsed_rows: list[dict], target_subfund_name: str,
                        hint_fund_id: str | None = None) -> tuple[str | None, float, str]:
    """Znajdz fund_id w PDFie ktory odpowiada target_subfund_name z funds.yaml.

    Args:
        parsed_rows: wynik parse_pdf().rows
        target_subfund_name: z funds.yaml.subfund_name (np. "PKO Konserwatywny")
        hint_fund_id: jesli funds.fund_id juz ustawiony, sprawdz najpierw jego.

    Returns:
        (fund_id, score, reason) gdzie:
            fund_id - matched lub None
            score - SequenceMatcher ratio (0..1)
            reason - 'hint_match', 'exact_match', 'fuzzy_best', 'no_match'
    """
    # Lista (fund_id, pdf_subfund_name) per pierwszy wiersz dla kazdego fund_id
    candidates: dict[str, str] = {}
    for r in parsed_rows:
        fid = r.get("fund_id")
        if fid and fid not in candidates:
            candidates[fid] = r.get("subfund_name") or ""

    if not candidates:
        return None, 0.0, "no_match"

    # 0. Hint - jesli funds.fund_id juz ustawiony, uzyj go bez fuzzy
    if hint_fund_id and hint_fund_id in candidates:
        return hint_fund_id, 1.0, "hint_match"

    target_norm = _normalize_name(target_subfund_name)

    # 1. Exact match po normalized name
    for fid, pdf_name in candidates.items():
        if _normalize_name(pdf_name) == target_norm:
            return fid, 1.0, "exact_match"

    # 2. Fuzzy match - najwyzszy SequenceMatcher.ratio
    best_fid = None
    best_score = 0.0
    for fid, pdf_name in candidates.items():
        pdf_norm = _normalize_name(pdf_name)
        if not pdf_norm:
            continue
        score = SequenceMatcher(None, target_norm, pdf_norm).ratio()
        if score > best_score:
            best_score = score
            best_fid = fid

    # Wymagamy min 0.75 zeby uniknac false matchy typu "Konserwatywny" vs "Konserwatywny Plus"
    if best_score >= 0.75:
        return best_fid, best_score, "fuzzy_best"
    return None, best_score, "no_match"


def load_funds_yaml() -> dict[str, dict]:
    """Wczytaj funds.yaml i zindexuj po parasol_code."""
    with CONFIG_PATH.open(encoding="utf-8") as fp:
        data = yaml.safe_load(fp) or {}
    return {f["parasol_code"]: f for f in (data.get("funds", []) or []) if "parasol_code" in f}


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse PDFy ze skladami portfeli")
    parser.add_argument("--parasol", type=str, help="Filtr: tylko ten parasol_code")
    parser.add_argument("--date", type=_parse_date, help="Filtr: tylko ten report_date")
    parser.add_argument("--force", action="store_true", help="Re-parsuj juz sparsowane snapshots")
    parser.add_argument("--limit", type=int, help="Maksymalna liczba snapshotow do sparsowania")
    parser.add_argument("--dry-run", action="store_true", help="Tylko pokaz co bylo by sparsowane")
    args = parser.parse_args()

    # Env check
    for var in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        if not os.environ.get(var):
            print(f"ERR: brak env var {var}", file=sys.stderr)
            sys.exit(2)

    funds_by_code = load_funds_yaml()

    from lib.supabase import select_all, upsert
    from lib import storage
    from lib.pdf_parser import (
        parse_pdf,
        compute_holdings_hash,
        aggregate_aum,
        aggregate_exposures,
        filter_rows_by_fund_id,
        unique_fund_ids,
    )

    # SELECT pending snapshots.
    # --force: bierze wszystko (rowniez 'partial' z poprzednich failed parsowan)
    # bez --force: tylko scrape_status='ok' AND holdings_count IS NULL
    filters = []
    if args.force:
        filters.append("scrape_status=in.(ok,partial)")
    else:
        filters.append("scrape_status=eq.ok")
        filters.append("holdings_count=is.null")
    if args.parasol:
        filters.append(f"parasol_code=eq.{args.parasol}")
    if args.date:
        filters.append(f"report_date=eq.{args.date.isoformat()}")

    query = "?select=snapshot_id,parasol_code,report_date,pdf_path,pdf_hash,fund_id&" + "&".join(filters)
    query += "&order=report_date.desc,parasol_code.asc"
    if args.limit:
        query += f"&limit={args.limit}"

    snapshots = select_all("portfolio_snapshots", query)
    print(f"Snapshots do sparsowania: {len(snapshots)}")
    if not snapshots:
        return

    if args.dry_run:
        for s in snapshots[:20]:
            print(f"  {s['parasol_code']:8} {s['report_date']:12} snap_id={s['snapshot_id']}")
        if len(snapshots) > 20:
            print(f"  ... ({len(snapshots) - 20} more)")
        return

    # Pre-fetch previous snapshots per fund_id (do unchanged_flag detection)
    # Mapping (fund_id, report_date) -> previous holdings_hash. Pobieramy raz na start.
    all_with_hashes = select_all(
        "portfolio_snapshots",
        "?select=fund_id,report_date,holdings_hash&fund_id=not.is.null&holdings_hash=not.is.null"
        "&order=fund_id.asc,report_date.asc",
    )
    # Sortuj per fund_id, znajduj poprzedni hash dla danej daty
    by_fund: dict[str, list[tuple[str, str]]] = {}
    for r in all_with_hashes:
        by_fund.setdefault(r["fund_id"], []).append((r["report_date"], r["holdings_hash"]))

    def previous_hash(fund_id: str | None, current_date: str) -> str | None:
        if not fund_id or fund_id not in by_fund:
            return None
        prev = None
        for d, h in by_fund[fund_id]:
            if d < current_date:
                prev = h
            else:
                break
        return prev

    stats = {
        "processed": 0,
        "holdings_inserted": 0,
        "exposures_inserted": 0,
        "no_fund_match": 0,
        "unchanged": 0,
        "errors": 0,
    }

    for snap in snapshots:
        snap_id = snap["snapshot_id"]
        code = snap["parasol_code"]
        report_date = snap["report_date"]
        pdf_path = snap.get("pdf_path")

        fund_cfg = funds_by_code.get(code)
        if not fund_cfg:
            print(f"  {code} {report_date}: WARN funds.yaml nie zawiera tego parasola - skip")
            continue

        print(f"\n  {code} {report_date}: pobieram z storage {pdf_path or '(brak ścieżki)'}")
        if not pdf_path:
            print(f"    SKIP: brak pdf_path w snapshocie")
            stats["errors"] += 1
            continue

        try:
            pdf_bytes = storage.download_pdf(pdf_path)
        except FileNotFoundError:
            print(f"    ERR: PDF nie istnieje w storage")
            stats["errors"] += 1
            continue
        except Exception as e:
            print(f"    ERR: download failed: {e}")
            stats["errors"] += 1
            continue

        try:
            parsed = parse_pdf(pdf_bytes)
        except Exception as e:
            print(f"    ERR: parse failed: {e}")
            stats["errors"] += 1
            continue

        print(f"    parsed: {len(parsed.rows)} wierszy, subfunds={len(unique_fund_ids(parsed.rows))}, "
              f"parasol_name_pdf={parsed.parasol_name!r}")

        # Znajdz fund_id w PDFie ktory odpowiada subfund_name z funds.yaml
        target_name = fund_cfg["subfund_name"]
        hint = snap.get("fund_id") or fund_cfg.get("fund_id")
        fid, score, reason = find_fund_id_in_pdf(parsed.rows, target_name, hint_fund_id=hint)
        if not fid:
            print(f"    NO MATCH: target='{target_name}' best_score={score:.2f}")
            stats["no_fund_match"] += 1
            # Update snapshot status do 'partial'
            upsert("portfolio_snapshots", [{
                "parasol_code": code,
                "report_date": report_date,
                "scrape_status": "partial",
                "error_message": f"No fund_id match for '{target_name}' (best score {score:.2f})",
            }], on_conflict="parasol_code,report_date")
            continue

        print(f"    matched fund_id={fid} (reason={reason}, score={score:.2f})")
        fund_rows = filter_rows_by_fund_id(parsed.rows, fid)
        if not fund_rows:
            stats["no_fund_match"] += 1
            continue

        # Compute aggregates
        holdings_hash = compute_holdings_hash(fund_rows)
        aum = aggregate_aum(fund_rows)
        prev_hash = previous_hash(fid, report_date)
        is_unchanged = prev_hash is not None and prev_hash == holdings_hash
        if is_unchanged:
            stats["unchanged"] += 1
            print(f"    unchanged from previous snapshot (hash match)")

        # DELETE existing holdings + exposures dla tego snapshotu (idempotencja)
        if args.force or True:  # zawsze - new INSERT zawsze fresh
            from lib.supabase import _HEADERS, SUPABASE_URL  # type: ignore
            import requests
            del_url = f"{SUPABASE_URL}/rest/v1/holdings?snapshot_id=eq.{snap_id}"
            requests.delete(del_url, headers=_HEADERS, timeout=30)
            del_url2 = f"{SUPABASE_URL}/rest/v1/exposures?snapshot_id=eq.{snap_id}"
            requests.delete(del_url2, headers=_HEADERS, timeout=30)

        # INSERT holdings
        holdings_rows = []
        for r in fund_rows:
            holdings_rows.append({
                "snapshot_id": snap_id,
                "isin": r.get("isin"),
                "issuer_name": r.get("issuer_name"),
                "security_name": r.get("subfund_name"),  # w PDFie pole jest takie samo
                "instrument_type": r.get("instrument_type"),
                "instrument_category": r.get("instrument_category"),
                "issuer_country": r.get("issuer_country"),
                "risk_country": r.get("risk_country"),
                "currency": r.get("currency"),
                "quantity": r.get("quantity"),
                "value_pln": r.get("value_pln"),
                "weight_assets_pct": r.get("weight_assets_pct"),
                "weight_nav_pct": r.get("weight_nav_pct"),
                "info": r.get("info"),
            })
        if holdings_rows:
            from lib.supabase import _HEADERS, SUPABASE_URL  # noqa: F811
            import requests
            url = f"{SUPABASE_URL}/rest/v1/holdings"
            r = requests.post(url, headers={**_HEADERS, "Prefer": "return=minimal"}, json=holdings_rows, timeout=60)
            r.raise_for_status()
            stats["holdings_inserted"] += len(holdings_rows)

        # Exposures
        exposures_rows = []
        for dim in ("currency", "issuer_country", "risk_country", "instrument_type"):
            for bucket, weight in aggregate_exposures(fund_rows, dim).items():
                exposures_rows.append({
                    "snapshot_id": snap_id,
                    "dimension": dim,
                    "bucket": bucket,
                    "weight_nav_pct": round(weight, 4),
                })
        if exposures_rows:
            upsert("exposures", exposures_rows, on_conflict="snapshot_id,dimension,bucket")
            stats["exposures_inserted"] += len(exposures_rows)

        # UPDATE snapshot: fund_id, holdings_hash, unchanged_flag, aum_pln, holdings_count
        upsert("portfolio_snapshots", [{
            "parasol_code": code,
            "report_date": report_date,
            "fund_id": fid,
            "holdings_hash": holdings_hash,
            "unchanged_flag": is_unchanged,
            "aum_pln": round(aum, 2) if aum else None,
            "holdings_count": len(fund_rows),
            "scrape_status": "ok",
            "error_message": None,
        }], on_conflict="parasol_code,report_date")

        # UPDATE funds: fund_id (jednorazowo) i parasol_name (z PDFa)
        if (not fund_cfg.get("fund_id") or not fund_cfg.get("parasol_name")):
            upsert("funds", [{
                "parasol_code": code,
                "fund_id": fid,
                "parasol_name": parsed.parasol_name,
                "subfund_name": fund_cfg["subfund_name"],   # zachowaj z YAML (autoryteatywne)
                "tfi_name": fund_cfg.get("tfi_name"),
                "analizy_slug": fund_cfg.get("analizy_slug", ""),
                "refresh_freq": fund_cfg.get("refresh_freq", "monthly"),
            }], on_conflict="parasol_code")

        stats["processed"] += 1

    print(f"\n=== PODSUMOWANIE ===")
    for k, v in stats.items():
        print(f"  {k:25} {v}")


if __name__ == "__main__":
    main()
