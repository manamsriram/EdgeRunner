# Crypto backtest at realistic cost (P1.4 deliverable)

Rerun **2026-07-12** with Alpaca live keys. Cost model: **10bps slippage + 25bps Alpaca
crypto taker fee**, intra-bar stop. Universe: BTC, ETH, SOL, LINK, XRP, DOGE, AVAX (7 pairs).
Reproduce: `venv/bin/python scripts/backtest_crypto_candidates.py [--years 4]`.

Purpose: confirm the pure-Donchian crypto stack (`_build_crypto_strategies_for`) still wins
once real taker fees are charged, not the 5–10bps idealization the original choice was made at.

## 2-year (averages across all symbols)

| label                    | return | sharpe | max_dd | win% | trades | vs B&H |
|--------------------------|-------:|-------:|-------:|-----:|-------:|-------:|
| **DonchianBreakout**     | **45.6%** | **0.39** | **-36.3%** | 45.4% | 136 | +69.1% ✓ |
| SmashDayB (prod)         | 21.4% | 0.19 | -53.5% | 39.1% | 229 | +44.9% ✓ |
| CryptoEMACrossover (prod)| 10.4% | 0.14 | -53.5% | 30.3% | 58  | +33.9% ✓ |
| PROD + Dip 30/10         | -1.6% | 0.08 | -54.2% | 37.0% | 229 | +21.9% ✓ |
| PROD + DonchianBreakout  | -2.3% | 0.06 | -53.3% | 35.9% | 245 | +21.2% ✓ |
| avg buy & hold           | -23.5% |      |        |      |        |        |

## 4-year (averages across all symbols)

| label                    | return | sharpe | max_dd | win% | trades | vs B&H |
|--------------------------|-------:|-------:|-------:|-----:|-------:|-------:|
| **DonchianBreakout**     | **170.5%** | **0.40** | **-43.3%** | 44.3% | 274 | +119.6% ✓ |
| SmashDayB (prod)         | 143.7% | 0.20 | -58.5% | 39.8% | 445 | +92.8% ✓ |
| PROD + Dip 30/10         | 87.0% | 0.25 | -59.8% | 39.6% | 446 | +36.0% ✓ |
| PROD + DonchianBreakout  | 62.8% | 0.18 | -59.6% | 38.8% | 480 | +11.9% ✓ |
| CryptoEMACrossover (prod)| 47.9% | 0.31 | -58.2% | 29.8% | 116 | -3.0% ✗ |
| avg buy & hold           | 50.9% |      |        |      |        |        |

## Conclusion

Pure DonchianBreakout dominates on both windows at 25bps — highest return AND shallowest
drawdown, and best vs buy-and-hold. Every strategy added to it dilutes return and worsens
drawdown (the composite's sell-priority lets weaker strategies truncate Donchian's big
winners). The production choice survives realistic cost. `trader/scheduler.py`
`_build_crypto_strategies_for` docstring numbers match this table (±rounding / end-date drift).
