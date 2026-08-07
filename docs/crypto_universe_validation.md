# Crypto candidate-universe edge validation (2026-08-07)

## Why

The dynamic crypto screener (`trader/universe/crypto_screener.py`) ranks
`CRYPTO_CANDIDATE_UNIVERSE` by 24h dollar volume and trades the top N — but the
original 25-pair pool had **zero edge filter**, only liquidity. Any Alpaca-listed
USD pair with enough volume could enter live rotation regardless of whether
DonchianBreakout (the sole live crypto strategy, see `scheduler.py
_build_crypto_strategies_for`) actually works on it.

The 2026-08-04 incident (7 same-day stop-loss/quick-exits: BAT -29%, BCH -13%,
XRP -7%, LTC -6%, AAVE -5%, LINK -4%, CRV -6%) traced to alt pairs that had never
been backtested — they were added to the candidate pool for coverage, not
validated for strategy fit.

## Method

Backtested DonchianBreakout on all 25 original candidates individually, 2yr
window, 10bps slippage + 25bps Alpaca taker fee (same cost model as
`crypto_backtest_25bps.md`). Reproduce: adapt
`scripts/backtest_crypto_candidates.py` or run `DonchianBreakout` per-symbol via
`trader.backtest.engine.run_backtest`.

## Result

| Edge (kept) | return | vs B&H | | No edge (dropped) | return | vs B&H |
|---|---:|---:|---|---|---:|---:|
| XRP/USD | +236.5% | +160.8% | | ADA/USD | -5.3% | +26.4% |
| DOGE/USD | +102.0% | +134.9% | | ETH/USD | -17.9% | +8.4% |
| MKR/USD | +63.4% | +77.0% | | FIL/USD | -18.7% | +10.1% |
| SOL/USD | +57.1% | +106.9% | | AVAX/USD | -19.0% | +51.4% |
| GRT/USD | +43.4% | +133.3% | | LINK/USD | -28.4% | -6.0% |
| BTC/USD | +31.0% | +24.5% | | DOT/USD | -30.4% | +52.6% |
| SHIB/USD | +24.7% | +91.8% | | BAT/USD | -51.9% | +7.8% |
| CRV/USD | +19.0% | +34.9% | | LTC/USD | -53.1% | -27.9% |
| SUSHI/USD | +11.0% | +83.0% | | AAVE/USD | -55.4% | -48.9% |
| | | | | UNI/USD | -56.1% | -20.9% |
| | | | | BCH/USD | -69.9% | -31.7% |
| | | | | YFI/USD | -3.1% | +56.5% |

No data on Alpaca (dropped): MATIC/USD, ALGO/USD, ATOM/USD, COMP/USD — likely
delisted/unsupported at this account tier.

Also tested: widening `crypto_stop_loss_pct` from 5% to 8% (to match equity).
Rejected — worse return AND worse max drawdown on both the edge set and the
loser set. The stop level wasn't the problem; trading symbols with no edge was.

## Action

`CRYPTO_CANDIDATE_UNIVERSE` cut to the 9 edge-positive symbols. The screener's
volume-ranking/rotation mechanism is unchanged — this only restricts which
symbols are eligible to enter that rotation.

## Caveat

Point-in-time cut on a 2yr trailing window ending 2026-08-07. Edge composition
drifts with market regime — a symbol here isn't guaranteed to keep working, and
one dropped isn't permanently disqualified. Revalidate periodically (suggest:
quarterly, or after any live incident cluster like this one).
