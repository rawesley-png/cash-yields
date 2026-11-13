# Cash Yields

A self-updating page that shows, every weekday morning, where cash should sit: three columns
(**Same day**, **Next day, no date**, and **By your date** with a date picker), each
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

## How it ranks

- Yields are compared at the two decimals they are published to. On a tie a fund ranks ahead of a bill, a
  deposit, then an ETF; then the safer tier, then the later cutoff.
- **Same day**: once a fund's cutoff has passed, the page moves the recommendation to the best fund still open.
- **By your date**: pick the date the cash is needed. Only bills maturing on or before it qualify. Ranking is
  on net dollars per $1M from the next business day to that date; after a bill matures the cash is assumed to
  sit in the Same-day pick. The card also shows the reinvestment rate at which rolling a shorter bill would
  match the recommended one.
- **Bill yields are indicative** (Treasury's closing bid-side rates), not purchase quotes.
- **Cutoffs are the funds' own deadlines.** Add `"merrill_cutoff": "14:00"` (and optionally
  `"merrill_cutoff_verified": "11/20/2026"`) to an instrument in `config/instruments.json` once Merrill's own
  deadline is confirmed; the page then uses and labels that instead.
- SC-taxable shares for the BlackRock and Fidelity funds come from the sponsors' 2025 tax letters (a prior-year
  proxy). Federated funds, ETFs and deposits use unverified estimates; each row says which.
- The change log compares each day with the previous day's run, so a second run on the same day rebuilds the
  day's entry rather than clearing it. It tracks the Same-day and Next-day picks and the dated pick 13 and 52
  weeks out.

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
