# SETUP — krok po kroku dla Supabase i seeda funduszy

Po Etapie 1 masz `config/funds.yaml` z 39 funduszami. Teraz przygotujemy bazę.

## Etap 2a — Konto Supabase i projekt

1. Wejdź na [supabase.com](https://supabase.com) i zaloguj się (GitHub OAuth jest najszybszy).
2. Utwórz nowy projekt. **Region: `eu-central-1` (Frankfurt)** — najmniejsza latencja z PL.
   - Pricing tier: **Free** wystarczy na początek (500MB DB, 50K MAU). Cały projekt zmieści się w <100MB.
   - Hasło DB ustaw silne — zapisz w bezpiecznym miejscu, przyda się raz przy SQL z psql.
3. Czekaj ~2min aż projekt się zbuduje.

## Etap 2b — Wyciągnij credentials

W lewym menu Supabase: **Settings → API**.

Zapisz dwie wartości:
- `Project URL` (np. `https://xxxxxxxxxxxx.supabase.co`) — to **SUPABASE_URL**
- W sekcji "Project API keys" → **`service_role` secret** (NIE `anon`!) — to **SUPABASE_SERVICE_ROLE_KEY**

⚠️ `service_role` ma **pełen dostęp do bazy**, omija RLS. Trzymaj go jak hasło — nie commituj, nie wklejaj na publicznych chatach.

## Etap 2c — Wykonaj schema setup w SQL editorze

1. Lewe menu → **SQL Editor → New query**.
2. Otwórz lokalnie `schemas/_setup_all.sql` i **skopiuj całą zawartość** do edytora w Supabase.
3. Kliknij **Run** (lub Ctrl+Enter). Wykonanie trwa ~2-5s. Powinieneś zobaczyć "Success. No rows returned" — zostały utworzone wszystkie tabele, indeksy, widoki, funkcje.
4. Weryfikacja: nowa zakładka **Table Editor** w lewym menu — powinieneś widzieć: `funds`, `fund_peer_groups`, `portfolio_snapshots`, `holdings`, `isin_metrics`, `portfolio_metrics`, `exposures`, `holdings_delta`, `llm_reports`.

Jeśli skrypt rzuca błąd "schema already exists" — to znaczy że masz już jakieś tabele z poprzedniej iteracji. Wszystkie statementy są idempotentne (CREATE IF NOT EXISTS / CREATE OR REPLACE), więc bezpiecznie odpalić ponownie.

## Etap 2d — Skonfiguruj lokalne env vars

W roocie projektu (`PEERS_COMPARISON/`) stwórz plik `.env`:

```
SUPABASE_URL=https://xxxxxxxxxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.xxxxxxxxxxxx
```

`.env` jest w `.gitignore`, więc nie zostanie commitnięty.

Załaduj zmienne do shellu (jeden z poniższych — zależnie od preferencji):

**PowerShell:**
```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^([^=]+)=(.+)$') {
    Set-Item -Path "env:$($matches[1])" -Value $matches[2]
  }
}
```

**Bash/WSL:**
```bash
export $(grep -v '^#' .env | xargs)
```

## Etap 2e — Załaduj fundusze do Supabase

```
python scripts/seed_funds.py --dry-run    # sprawdz co bedzie wyslane
python scripts/seed_funds.py              # faktyczny upsert
```

Powinno wypisać:
```
Wczytano 39 funduszy z config\funds.yaml
Do upsert: funds=39, fund_peer_groups=39
  funds: 39 wierszy upserted
  fund_peer_groups: 39 wierszy upserted (ignore-duplicates)
Gotowe.
```

## Etap 2f — Weryfikacja w Supabase

W SQL Editor uruchom:

```sql
SELECT parasol_code, subfund_name, tfi_name, peer_groups
FROM v_funds_with_groups
ORDER BY peer_groups DESC, parasol_code;
```

Oczekiwany wynik: **39 wierszy** (17 z `{fsk}`, 22 z `{fod}`).

```sql
SELECT peer_group, COUNT(*) FROM fund_peer_groups GROUP BY peer_group;
```

Oczekiwany wynik:
```
fod | 22
fsk | 17
```

## Etap 2g — Dodaj GH Secrets (przed Etapem 3)

W GitHub repo (jak już utworzysz): **Settings → Secrets and variables → Actions → New repository secret**.

Dodaj 3 sekrety:
- `SUPABASE_URL` — to samo co lokalnie
- `SUPABASE_SERVICE_ROLE_KEY` — to samo co lokalnie
- `ANTHROPIC_API_KEY` — Twój klucz Claude

To wszystko dla Etapu 2. Po weryfikacji jedziemy z **Etapem 3 (scraper + parser PDFów + backfill 4 lata wstecz)**.

---

## Co jest w bazie po tym etapie

| Tabela | Wpisy | Po co |
|---|---|---|
| `funds` | 39 | Master lista subfunduszy (parasol_code PK, subfund_name, tfi_name, refresh_freq) |
| `fund_peer_groups` | 39 | Junction: fund × grupa (fsk/fod) |
| `portfolio_snapshots` | 0 | Wypełni scraper w Etapie 3 |
| `holdings` | 0 | Wypełni parser w Etapie 3 |
| `isin_metrics` | 0 | Wypełni Bloomberg fill lokalnie w Etapie 4 |
| `portfolio_metrics`, `exposures`, `holdings_delta` | 0 | Wypełni compute_metrics.py w Etapie 5 |
| `llm_reports` | 0 | Wypełni generate_llm_reports.py w Etapie 5 |

Widoki/RPCs dostępne od razu (na pustych tabelach zwrócą 0 wierszy):
- `v_funds_with_groups`, `v_fund_metrics_history`, `v_fund_exposures_history`,
  `v_fund_top_holdings`, `v_consensus_holdings`, `v_delta_summary`
- `bbg_queue` (widok — co BBG ma uzupełnić; jeszcze pusty)
- RPC: `latest_snapshot_per_fund()`, `bbg_coverage_summary()`, `bbg_queue_summary()`,
  `llm_report_history(fund_id, limit)`, `llm_token_usage_summary()`,
  `yield_duration_scatter(date, peer_group)`
