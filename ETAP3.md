# ETAP 3 — Scraper + Parser + Backfill

Pobieranie PDFów ze składami portfeli z analizy.pl, parsowanie do `holdings`,
agregacja do `exposures` i `portfolio_metrics` (te ostatnie w Etapie 5).

## Co Etap 3 dostarcza

| Plik | Co robi |
|---|---|
| `scripts/lib/storage.py` | REST wrapper na Supabase Storage (upload/download/exists) |
| `scripts/lib/analizy_scraper.py` | Download PDF z `dokumenty.analizy.pl/pobierz/fi/{KOD}/SP/{YYYY-MM-DD}`, retry, SHA256 |
| `scripts/lib/pdf_parser.py` | Parser KNF: 18-kolumnowy layout, fund_id filtering, exposures aggregation |
| `scripts/scrape_pdfs.py` | Orchestrator pobierania: backfill / incremental / refetch |
| `scripts/parse_pdfs.py` | Orchestrator parsowania: fuzzy match fund_id, INSERT holdings+exposures, UPDATE snapshot |
| `schemas/storage_bucket.sql` | Setup bucketu `raw-pdfs` (private, PDF-only, 50MB limit) |

Również migracja `portfolio_snapshots`: UNIQUE constraint zmieniony z
`(parasol_code, fund_id, report_date)` na `(parasol_code, report_date)` — stabilny
klucz upsert dla scrape (gdzie fund_id=NULL przed parsowaniem).

## Krok 1 — Wykonaj migrację w Supabase

Otwórz **SQL Editor** w Supabase, wklej całą zawartość `schemas/_setup_all.sql` i
**Run**. Wykonanie pominie istniejące tabele (idempotentne `CREATE IF NOT EXISTS`),
ale:
- Doda nowe bucket `raw-pdfs` + 2 polityki RLS dla storage
- Wykona migrację UNIQUE constraint (NOTICE w outpucie: "Dropped old constraint" + "Added new constraint")

Weryfikacja:
```sql
-- Bucket istnieje?
SELECT id, public, file_size_limit FROM storage.buckets WHERE id = 'raw-pdfs';
-- Powinno: 1 wiersz, public=false, file_size_limit=52428800

-- Constraint zmieniony?
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'portfolio_snapshots'::regclass AND contype = 'u';
-- Powinno: UNIQUE (parasol_code, report_date) - bez fund_id
```

## Krok 2 — Test e2e na 1 fundusz

Załaduj env vars (te same co w Etapie 2):

**PowerShell:**
```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^([^=]+)=(.+)$') { Set-Item -Path "env:$($matches[1])" -Value $matches[2] }
}
```

Scrape jeden parasol na jedną datę:
```
python scripts/scrape_pdfs.py --funds FOR39 --date 2026-04-30
```

Oczekiwany output:
```
Tryb:    incremental
Funds:   1 (['FOR39']...)
Daty:    1 (2026-04-30 ... 2026-04-30)
Razem:   1 prob pobrania
=== Data 2026-04-30 ===
  FOR39: ok (XXX KB, sha=XXXXXXXX...)

=== PODSUMOWANIE ===
  fetched_ok                1
  uploaded                  1
  snapshots_upserted        1
```

Sparsuj:
```
python scripts/parse_pdfs.py --parasol FOR39 --date 2026-04-30
```

Oczekiwany output:
```
Snapshots do sparsowania: 1
  FOR39 2026-04-30: pobieram z storage raw-pdfs/FOR39/2026-04-30.pdf
    parsed: XXX wierszy, subfunds=N, parasol_name_pdf='BNPParibasFIO'
    matched fund_id=BPSXX (reason=fuzzy_best, score=0.XX)

=== PODSUMOWANIE ===
  processed                 1
  holdings_inserted         XX
  exposures_inserted        XX
```

Weryfikacja w Supabase SQL Editor:
```sql
-- Snapshoty
SELECT parasol_code, fund_id, report_date, holdings_count, aum_pln, unchanged_flag
FROM portfolio_snapshots WHERE parasol_code = 'FOR39';

-- Top holdings
SELECT isin, issuer_name, instrument_type, weight_nav_pct, currency
FROM holdings WHERE snapshot_id IN (
    SELECT snapshot_id FROM portfolio_snapshots WHERE parasol_code = 'FOR39'
) ORDER BY weight_nav_pct DESC NULLS LAST LIMIT 10;

-- Exposures
SELECT dimension, bucket, weight_nav_pct
FROM exposures WHERE snapshot_id IN (
    SELECT snapshot_id FROM portfolio_snapshots WHERE parasol_code = 'FOR39'
) ORDER BY dimension, weight_nav_pct DESC;

-- BBG queue (lista do uzupełnienia przez Bloomberg fill w Etapie 4)
SELECT * FROM bbg_queue WHERE as_of_date = '2026-04-30' LIMIT 20;
```

Jeśli wszystko OK → idź do Kroku 3.

## Krok 3 — Backfill 1 funduszu (4 lata)

```
python scripts/scrape_pdfs.py --mode backfill --start 2022-04-30 --end 2026-04-30 --funds FOR39
python scripts/parse_pdfs.py --parasol FOR39
```

Czas: ~1-2 min dla scrape (49 dat × 1s throttle), ~30s dla parse.

Sprawdź ile snapshotów udało się pobrać:
```sql
SELECT scrape_status, COUNT(*)
FROM portfolio_snapshots WHERE parasol_code = 'FOR39'
GROUP BY scrape_status;
```

Dla kwartalnego raportowania (większość funduszy raportuje miesięcznie, ale niektóre
kwartalne) — będziesz miał ~17-49 snapshotów ze statusem `ok` zamiast 49.

## Krok 4 — Pełny backfill 39 funduszy

⚠️ **Czas: ~45-60 minut**. Polecam odpalić na noc lub w tle (`&` w bashu).

```
python scripts/scrape_pdfs.py --mode backfill --start 2022-04-30 --end 2026-04-30
```

To są **1911 prób pobrania** (39 × 49). Większość zwróci 200 OK, niektóre 404
(kwartalne fundusze, fundusze nieistniejące jeszcze 4 lata temu).

Po scrape:
```
python scripts/parse_pdfs.py
```

Sparsuje wszystkie pending snapshots (gdzie `holdings_count IS NULL`).

## Krok 5 — Co dalej (Etap 4 i wyżej)

Po pomyślnym backfillu:

1. **Etap 4 (Bloomberg fill)** — lokalnie na stacji z BBG odpalisz
   `python scripts/bbg_fill.py`. Skrypt zselectuje z widoku `bbg_queue` brakujące
   ISIN-y, zapyta BBG o yield/duration/CS01/rating, zapisze do `isin_metrics`.
   (Etap 4 jeszcze niezaimplementowany — czeka na potwierdzenie że Etap 3
   działa, by uniknąć rebuildowania designu jeśli coś trzeba zmienić.)

2. **Etap 5 (Analytics + LLM)** — `compute_metrics.py` policzy weighted
   yield/duration/CS01 i wpisze do `portfolio_metrics`, `holdings_delta` przejdzie
   przez kolejne pary snapshotów, `generate_llm_reports.py` poprosi Claude Opus
   o komentarz per snapshot i cross-fund report.

3. **Etap 6 (GH Actions workflows)** — automatyzacja miesięczna +
   `render_dashboard.yml` deploy do GitHub Pages.

## Troubleshooting

### "ERR: brak env var SUPABASE_URL"
Załaduj `.env` ponownie (PowerShell się nie inheritsuje między sessjami).

### Storage upload failed 403
Sprawdź czy bucket `raw-pdfs` istnieje (Krok 1). Czasem RLS policy się nie utworzyła —
możesz wykonać `schemas/storage_bucket.sql` osobno.

### "No fund_id match for 'X' (best score 0.XX)"
Parser nie zmatchował subfund_name z funds.yaml z żadnym fund_id w PDFie.
Najczęstsze przyczyny:
- Fundusz został przemianowany (np. ING → Goldman Sachs po przejęciu) i nazwa
  w PDFie różni się znacząco od nazwy w analizy.pl
- Fundusz został zlikwidowany — PDF dla tej daty ma inną strukturę

Sprawdź ręcznie:
```sql
-- Co jest w PDFie dla tego snapshotu?
SELECT DISTINCT issuer_name FROM holdings WHERE snapshot_id = X LIMIT 5;
```

Update `funds.yaml` ręcznie z poprawnym `fund_id` jeśli wiesz który matchuje:
```yaml
- parasol_code: ARK23
  fund_id: ARK0XX  # ← wpisz ręcznie
```

Potem ponownie:
```
python scripts/seed_funds.py
python scripts/parse_pdfs.py --parasol ARK23 --force
```

### Timeout / 5xx z dokumenty.analizy.pl
Backend Symfony analizy.pl ma sporadyczne timeouty. Scraper ma 3 retry z backoff.
Jeśli za dużo errorów → zwiększ throttle:
```
python scripts/scrape_pdfs.py --throttle 2.0 ...
```
