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


def _process_snapshot_holdings(
    snap: dict,
    fund_rows: list[dict],
    fid: str,
    pdf_parasol_name: str | None,
    fund_cfg: dict,
    previous_hash_fn,
    stats: dict,
    compute_holdings_hash_fn,
    aggregate_aum_fn,
    aggregate_exposures_fn,
    upsert_fn,
) -> None:
    """Wstaw holdings + exposures dla snapshotu, update snapshot z metadanymi.

    Idempotentne: DELETE existing przed INSERT.
    """
    snap_id = snap["snapshot_id"]
    code = snap["parasol_code"]
    report_date = snap["report_date"]

    holdings_hash = compute_holdings_hash_fn(fund_rows)
    aum = aggregate_aum_fn(fund_rows)
    prev_hash = previous_hash_fn(fid, report_date)
    is_unchanged = prev_hash is not None and prev_hash == holdings_hash
    if is_unchanged:
        stats["unchanged"] += 1

    # DELETE existing holdings + exposures (idempotencja)
    from lib.supabase import _HEADERS, SUPABASE_URL
    import requests as _req
    _req.delete(f"{SUPABASE_URL}/rest/v1/holdings?snapshot_id=eq.{snap_id}", headers=_HEADERS, timeout=30)
    _req.delete(f"{SUPABASE_URL}/rest/v1/exposures?snapshot_id=eq.{snap_id}", headers=_HEADERS, timeout=30)

    # INSERT holdings
    holdings_rows = [{
        "snapshot_id": snap_id,
        "isin": r.get("isin"),
        "issuer_name": r.get("issuer_name"),
        "security_name": r.get("subfund_name"),
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
    } for r in fund_rows]
    if holdings_rows:
        r = _req.post(
            f"{SUPABASE_URL}/rest/v1/holdings",
            headers={**_HEADERS, "Prefer": "return=minimal"},
            json=holdings_rows, timeout=60,
        )
        r.raise_for_status()
        stats["holdings_inserted"] += len(holdings_rows)

    # Exposures
    exposures_rows = []
    for dim in ("currency", "issuer_country", "risk_country", "instrument_type"):
        for bucket, weight in aggregate_exposures_fn(fund_rows, dim).items():
            exposures_rows.append({
                "snapshot_id": snap_id,
                "dimension": dim,
                "bucket": bucket,
                "weight_nav_pct": round(weight, 4),
            })
    if exposures_rows:
        upsert_fn("exposures", exposures_rows, on_conflict="snapshot_id,dimension,bucket")
        stats["exposures_inserted"] += len(exposures_rows)

    # UPDATE snapshot
    upsert_fn("portfolio_snapshots", [{
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
    if not fund_cfg.get("fund_id") or not fund_cfg.get("parasol_name"):
        upsert_fn("funds", [{
            "parasol_code": code,
            "fund_id": fid,
            "parasol_name": pdf_parasol_name,
            "subfund_name": fund_cfg["subfund_name"],
            "tfi_name": fund_cfg.get("tfi_name"),
            "analizy_slug": fund_cfg.get("analizy_slug", ""),
            "refresh_freq": fund_cfg.get("refresh_freq", "monthly"),
        }], on_conflict="parasol_code")

    stats["processed"] += 1


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
    from lib.llm_pdf_parser import parse_pdf_with_claude, parse_text_with_claude
    from lib.ocr_pdf import ocr_pdf_to_text, is_tesseract_available

    # Threshold: jesli pdfplumber zwroci <N wierszy, sprobujemy Claude fallback.
    # Dla typowych portfeli mamy 30-100+ holdings per subfundusz; <10 oznacza
    # ze pdfplumber broken (UNIQA, Caspar, Investor, Goldman, Pekao layouty).
    LLM_FALLBACK_MIN_ROWS = 10
    # Throttle po non-cache LLM call zeby uniknac rate limit.
    LLM_THROTTLE_SECONDS = int(os.environ.get("LLM_THROTTLE_SECONDS", "25"))
    # Skip LLM dla PDFow >N MB - vision tokens przekraczaja praktyczny limit
    # (Sonnet 4.6 max input 200K tokens; 5MB PDF = ~420K tokens, hung/error).
    # Goldman (5MB) + Pekao (18MB) potrzebuja innego podejscia (OCR / split).
    LLM_MAX_PDF_MB = int(os.environ.get("LLM_MAX_PDF_MB", "3"))
    import time as _time

    # SELECT pending snapshots.
    # --force: bierze wszystko (rowniez 'partial' i 'error' z poprzednich failed
    #          parsowan / rate limit crashy)
    # bez --force: tylko scrape_status='ok' AND holdings_count IS NULL
    filters = []
    if args.force:
        filters.append("scrape_status=in.(ok,partial,error)")
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
        "llm_calls": 0,
        "llm_cache_hits": 0,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "unique_pdfs_processed": 0,
        "snapshots_with_same_pdf": 0,
    }

    # GRUPUJ snapshoty po pdf_hash zeby parsowac PDF tylko RAZ per unikalny plik.
    # Wiele parasol_code czesto wskazuje na ten sam parasol PDF (np. Goldman Sachs
    # ING03+ING04+ING54+ING90 to ten sam pdf_hash). Dedup oszczedza:
    #   - download z storage (raz vs N razy)
    #   - parse pdfplumber (raz vs N razy)
    #   - Claude call (raz vs N razy, ogromna oszczednosc $$)
    from collections import defaultdict
    snapshots_by_hash: dict[str, list[dict]] = defaultdict(list)
    snapshots_no_hash: list[dict] = []
    for s in snapshots:
        h = s.get("pdf_hash")
        if h:
            snapshots_by_hash[h].append(s)
        else:
            snapshots_no_hash.append(s)
    if snapshots_no_hash:
        print(f"\n  WARN: {len(snapshots_no_hash)} snapshotow bez pdf_hash - skip")
        for s in snapshots_no_hash:
            print(f"    {s['parasol_code']} {s['report_date']}")

    print(f"\nUnique PDFy do przetworzenia: {len(snapshots_by_hash)} "
          f"(z {len(snapshots)} snapshotow, dedup po pdf_hash)")
    for h, snap_list in snapshots_by_hash.items():
        if len(snap_list) > 1:
            codes = sorted({s["parasol_code"] for s in snap_list})
            print(f"  shared PDF hash={h[:8]} -> {len(snap_list)} snapshotow ({codes})")

    for pdf_hash, snap_list in snapshots_by_hash.items():
        codes_in_group = sorted({s["parasol_code"] for s in snap_list})
        pdf_path = next((s.get("pdf_path") for s in snap_list if s.get("pdf_path")), None)
        if not pdf_path:
            print(f"\n  hash={pdf_hash[:8]}... ({codes_in_group}): SKIP - brak pdf_path")
            for s in snap_list:
                stats["errors"] += 1
            continue

        print(f"\n  hash={pdf_hash[:8]}... ({len(snap_list)} snapshots: {codes_in_group}): pobieram {pdf_path}")
        stats["unique_pdfs_processed"] += 1
        if len(snap_list) > 1:
            stats["snapshots_with_same_pdf"] += len(snap_list) - 1

        try:
            pdf_bytes = storage.download_pdf(pdf_path)
        except FileNotFoundError:
            print(f"    ERR: PDF nie istnieje w storage")
            for s in snap_list:
                stats["errors"] += 1
            continue
        except Exception as e:
            print(f"    ERR: download failed: {e}")
            for s in snap_list:
                stats["errors"] += 1
            continue

        try:
            parsed = parse_pdf(pdf_bytes)
        except Exception as e:
            print(f"    ERR: parse failed: {e}")
            for s in snap_list:
                stats["errors"] += 1
            continue

        all_rows = parsed.rows
        pdf_parasol_name = parsed.parasol_name
        print(f"    pdfplumber: {len(all_rows)} wierszy, subfunds={len(unique_fund_ids(all_rows))}")

        # Claude fallback raz na PDF (nie per snapshot - massive savings).
        # Dla duzych PDFow (>3MB) auto-dispatch do OCR -> Claude text prompt
        # (text tokens ~35x tansze niz vision tokens).
        llm_did_real_call = False
        if len(all_rows) < LLM_FALLBACK_MIN_ROWS:
            pdf_mb = len(pdf_bytes) / (1024 * 1024)
            if pdf_mb > LLM_MAX_PDF_MB:
                if not is_tesseract_available():
                    print(f"    PDF {pdf_mb:.1f}MB > {LLM_MAX_PDF_MB}MB ale Tesseract brak w PATH - skip")
                    for s in snap_list:
                        stats["no_fund_match"] += 1
                        upsert("portfolio_snapshots", [{
                            "parasol_code": s["parasol_code"],
                            "report_date": s["report_date"],
                            "scrape_status": "partial",
                            "error_message": f"PDF too large ({pdf_mb:.1f}MB), Tesseract OCR not available",
                        }], on_conflict="parasol_code,report_date")
                    continue
                print(f"    PDF {pdf_mb:.1f}MB - OCR -> Claude text mode")
                try:
                    ocr_text = ocr_pdf_to_text(pdf_bytes, lang="pol+eng", dpi=200)
                except Exception as e:
                    print(f"    OCR failed: {e}")
                    for s in snap_list:
                        stats["errors"] += 1
                        upsert("portfolio_snapshots", [{
                            "parasol_code": s["parasol_code"],
                            "report_date": s["report_date"],
                            "scrape_status": "error",
                            "error_message": f"OCR: {e}",
                        }], on_conflict="parasol_code,report_date")
                    continue
                print(f"    OCR text: {len(ocr_text)} chars (~{len(ocr_text)//4} tokens)")
                llm_result = parse_text_with_claude(ocr_text, pdf_hash)
            else:
                print(f"    pdfplumber={len(all_rows)} wierszy - Claude fallback ({pdf_mb:.1f}MB)")
                llm_result = parse_pdf_with_claude(pdf_bytes, pdf_hash)
            stats["llm_calls"] += 1
            if llm_result.cache_hit:
                stats["llm_cache_hits"] += 1
            stats["llm_input_tokens"] += llm_result.input_tokens
            stats["llm_output_tokens"] += llm_result.output_tokens

            if llm_result.error:
                print(f"    LLM error: {llm_result.error}")
                for s in snap_list:
                    stats["errors"] += 1
                    upsert("portfolio_snapshots", [{
                        "parasol_code": s["parasol_code"],
                        "report_date": s["report_date"],
                        "scrape_status": "error",
                        "error_message": f"LLM fallback: {llm_result.error}",
                    }], on_conflict="parasol_code,report_date")
                continue
            if not llm_result.rows:
                print(f"    LLM zwrocil 0 wierszy")
                for s in snap_list:
                    stats["no_fund_match"] += 1
                    upsert("portfolio_snapshots", [{
                        "parasol_code": s["parasol_code"],
                        "report_date": s["report_date"],
                        "scrape_status": "partial",
                        "error_message": f"LLM: no rows in PDF",
                    }], on_conflict="parasol_code,report_date")
                continue

            all_rows = llm_result.rows
            tag = "[cache]" if llm_result.cache_hit else f"[{llm_result.input_tokens}in/{llm_result.output_tokens}out]"
            print(f"    LLM extracted {len(all_rows)} wierszy, subfunds={len(unique_fund_ids(all_rows))} {tag}")
            llm_did_real_call = not llm_result.cache_hit

        # Dla kazdego snapshotu w grupie - filter rows do jego fund_id, INSERT
        for snap in snap_list:
            snap_id = snap["snapshot_id"]
            code = snap["parasol_code"]
            report_date = snap["report_date"]
            fund_cfg = funds_by_code.get(code)
            if not fund_cfg:
                print(f"    [{code}] WARN funds.yaml nie zawiera - skip")
                continue

            target_name = fund_cfg["subfund_name"]
            hint = snap.get("fund_id") or fund_cfg.get("fund_id")
            fid, score, reason = find_fund_id_in_pdf(all_rows, target_name, hint_fund_id=hint)
            if not fid:
                print(f"    [{code}] NO MATCH: target='{target_name}' best_score={score:.2f}")
                stats["no_fund_match"] += 1
                upsert("portfolio_snapshots", [{
                    "parasol_code": code,
                    "report_date": report_date,
                    "scrape_status": "partial",
                    "error_message": f"No fund_id match for '{target_name}' (best score {score:.2f})",
                }], on_conflict="parasol_code,report_date")
                continue
            fund_rows = filter_rows_by_fund_id(all_rows, fid)
            print(f"    [{code}] matched fund_id={fid} (reason={reason}, score={score:.2f}), {len(fund_rows)} holdings")
            _process_snapshot_holdings(
                snap, fund_rows, fid, pdf_parasol_name, fund_cfg,
                previous_hash, stats,
                compute_holdings_hash, aggregate_aum, aggregate_exposures, upsert,
            )

        # Throttle Claude PO ZAKONCZENIU procesowania grupy (jak byl real call)
        if llm_did_real_call:
            print(f"  throttle {LLM_THROTTLE_SECONDS}s (Anthropic rate limit)...")
            _time.sleep(LLM_THROTTLE_SECONDS)

    print("\n=== PODSUMOWANIE ===")
    for k, v in stats.items():
        print(f"  {k:25} {v}")


if __name__ == "__main__":
    main()
