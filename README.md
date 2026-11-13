# Cash Yields

A self-updating page that shows, every weekday morning, where cash should sit: four columns
(**Same day**, **Next day, no date**, **Dated inside 13 weeks**, **Dated inside 52 weeks**), each
ranked by net yield after the selected entity's taxes (SC partners, SC trust, FL trust, Foundation),
with today's order cutoff and the date the cash is available on every row.

Same shape as the tripwires repo: `cash_yields.py` (engine) + `template.html` → `docs/index.html`,
`docs/data.json`, `history.json`, `changelog.json`, `state.json`.

## Run it

    pip install requests          # plus pdftotext (poppler-utils)
    python3 cash_yields.py
    open docs/index.html

GitHub Actions (`.github/workflows/cash-yields.yml`) runs it on weekdays at 7:30 a.m. Eastern and
commits the output; GitHub Pages serves `docs/`.

## Where the numbers come from

| Data | Source |
|---|---|
| Money-fund 7-day yields, Preferred Deposit tiers, sweep rates | Merrill "Yields at a Glance" PDF |
| Daily sponsor yields | BlackRock cash product screener (TTTXX, TSTXX, TFDXX, TMCXX, MCSXX); Federated Hermes product API (UTIXX, FFTXX, UTPXX, FTFXX) |
| ETF yields | iShares SGOV page; State Street BIL page (30-day SEC yield, yield to maturity) |
| Bill yields | Treasury daily bill rates XML (coupon-equivalent) |
| Bill auctions | TreasuryDirect upcoming-auctions JSON |
| Holidays, early closes | SIFMA holiday schedule, cached weekly in `config/calendar.json` |
| Fund cutoffs | Federated Hermes fund pages (daily); BlackRock fact sheets (monthly); Fidelity from config |

A fund is ranked on whichever of the Merrill-sheet yield and the sponsor yield is newer (Merrill on
a tie). Fidelity has no public daily feed, so Fidelity funds use the Merrill sheet only. USFR has no
feed and shows "no data". **No yield is ever estimated**: an instrument with no fetched yield is
greyed and left out of the ranking, and when a feed fails the last good values are kept and a banner
says so.

## Config

- `config/instruments.json` — instruments, risk tiers, taxable shares, cutoffs, settlement notes.
- `config/tax.json` — rates, `max_risk_tier_recommended`, `iap_accounts`. Shown in the page footer.
- `config/sources.json` — feed URLs.
- `manual/ICCRateSheet.pdf` — optional hand-saved Merrill sheet, used if the download fails.

## Alerts

Same module and secret names as tripwires (`SMTP_USER`, `SMTP_PASS`, `ALERT_EMAIL`, `PUSHOVER_USER`,
`PUSHOVER_TOKEN`, `SITE_URL`). An alert goes out when a Recommended pick changes or the muni test
flips. With no secrets set, nothing is sent.

## Testing switches

    CY_BREAK=merrill,federated python3 cash_yields.py     # force feeds to fail
    CY_NOW=2026-11-13T09:00 python3 cash_yields.py        # pretend it is this Eastern time

`CY_BREAK` accepts: merrill, treasury, auctions, sifma, blackrock, federated, ishares, ssga.
