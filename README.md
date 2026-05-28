# PEERS_COMPARISON

Miesięczna analiza konkurencji TFI/funduszy dłużnych — scrapowanie składów portfeli z
[analizy.pl](https://www.analizy.pl), śledzenie zmian, wzbogacanie danymi z Bloomberga
(yield/duration/CS01/rating), generowanie wniosków przez Claude Opus i propozycji inwestycyjnych.

## Architektura

```
┌─────────────────────────────────────────────────────────────────┐
│ ☁️  GITHUB ACTIONS (chmura, cron miesięczny + workflow_dispatch) │
│                                                                  │
│   scrape analizy.pl → parse PDF → INSERT holdings do Supabase   │
│         ↓                                                        │
│   flaguje brakujące (ISIN, as_of_date) w widoku bbg_queue       │
│         ↓                                                        │
│   📧 email do kamilkonat@gmail.com: "X ISIN-ów do BBG"          │
└─────────────────────────────────────────────────────────────────┘
                          │
                  Supabase Postgres (single source of truth)
                          │
┌─────────────────────────────────────────────────────────────────┐
│ 🖥️  LOKALNA STACJA Z BLOOMBERGIEM                                │
│                                                                  │
│   python scripts/bbg_fill.py                                     │
│     - SELECT brakujące z widoku bbg_queue                       │
│     - BLPAPI → yield/duration/CS01/rating                       │
│     - UPSERT do isin_metrics                                    │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ ☁️  GITHUB ACTIONS (drugi workflow, workflow_dispatch po BBG)    │
│                                                                  │
│   compute_metrics → generate_llm_reports → export_excel_recos    │
│         ↓                                                        │
│   render_dashboard.ipynb → public/index.html → GitHub Pages      │
│         ↓                                                        │
│   📧 raport gotowy: link do dashboardu                          │
└─────────────────────────────────────────────────────────────────┘
```

## Struktura repo

```
PEERS_COMPARISON/
├── .github/workflows/      # cron'y i workflow_dispatch dla GH Actions
├── scripts/
│   ├── lib/                # supabase wrapper, parser PDF, BBG helper, LLM client
│   ├── discover_funds.py   # ONE-OFF: skrobie grupy porównawcze z analizy.pl
│   ├── scrape_pdfs.py      # pobiera nowe PDFy (deduplikacja po parasolu+dacie)
│   ├── parse_pdfs.py       # ekstrakcja holdingów z PDFa do tabel Supabase
│   ├── bbg_fill.py         # LOCAL ONLY: uzupełnia isin_metrics przez BLPAPI
│   ├── compute_metrics.py  # weighted yield/duration/CS01 + delty + exposures
│   ├── generate_llm_reports.py  # Claude Opus per fund + cross-fund report
│   └── export_excel_recos.py    # Excel "co kupować/sprzedawać"
├── notebooks/
│   └── peers_dashboard.ipynb    # główny dashboard (per-fund + overview)
├── schemas/                # *_schema.sql per tabela, wykonywane w Supabase SQL editor
├── config/
│   └── funds.yaml          # lista funduszy w grupach FOD i FSK
├── requirements.txt        # cloud (GH Actions)
├── requirements-local.txt  # lokalnie (bbg_fill.py)
└── requirements-notebook.txt  # dashboard
```

## Grupy porównawcze

Śledzimy dwie grupy porównawcze:

- **FSK** — fundusze dłużne polskie skarbowe krótkoterminowe.
  Fundusz referencyjny: [PKO Obligacji Skarbowych Krótkoterminowy (PCS05)](https://www.analizy.pl/fundusze-inwestycyjne-otwarte/PCS05/pko-obligacji-skarbowych-krotkoterminowy#competition).

- **FOD** — fundusze dłużne polskie skarbowe średnio/długoterminowe.
  Fundusz referencyjny: [PKO Obligacji Skarbowych Średnioterminowy (PCS91)](https://www.analizy.pl/fundusze-inwestycyjne-otwarte/PCS91/pko-obligacji-skarbowych-srednioterminowy#competition).

## Setup

1. Klonuj repo, zainstaluj zależności:
   ```
   pip install -r requirements.txt
   playwright install chromium
   ```
2. Skonfiguruj GH Secrets: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `ANTHROPIC_API_KEY`,
   `SMTP_*` dla powiadomień email.
3. Wykonaj `schemas/*.sql` w Supabase SQL editor.
4. Uruchom `python scripts/discover_funds.py` — wypełnia `config/funds.yaml`.
5. Trigger workflow `scrape_analizy.yml` z `workflow_dispatch` z `mode=backfill` żeby
   pobrać 4 lata historii.
6. Na stacji z BBG: `pip install -r requirements-local.txt` i okresowo
   `python scripts/bbg_fill.py`.
