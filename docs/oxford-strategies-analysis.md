# Oxford Strategy Analysis: Top 5 B-Rated Performers

Source: https://oxfordstrat.com/category/trading-strategies/
Screened: 101 strategies | Only 5 received rating B (highest awarded — no A ratings exist in the dataset)
Test conditions: 42 US futures markets, 32 years (1980–2011), fixed fractional 1% position sizing, $100 round-trip commission

---

## Rating Scale

| Rating | Meaning |
|--------|---------|
| A | Best — never awarded to any strategy in this dataset |
| B | Top performer — robust across parameter sensitivity |
| C | Marginal — works but not robust or barely beats costs |
| D | Avoid — fails after realistic trading costs |

---

## Strategy Comparison: All 5 B-Rated

### 1. Gap Pattern — Type A (Filter & Exit)

**URL:** https://oxfordstrat.com/trading-strategies/gap-pattern/

| Attribute | Detail |
|-----------|--------|
| Signal type | Breakaway gap |
| Direction | Long + Short |
| Entry | Market at open after gap + trend filter confirms |
| Exit | Time exit (N bars) or pattern exit (gap closes) |
| Filter | Price channel: price must be above/below N-bar high/low |
| Asset fit | Stocks ✅ ETFs ✅ Crypto ✅ |
| Complexity | Low |
| Timeframe | Daily bars |

**Rules:**
```
Long:  Low[i] > High[i-1]                      # gap up
       AND price > highest_high(Filter_N)       # in uptrend
       Entry: market buy at open

Short: High[i] < Low[i-1]                      # gap down
       AND price < lowest_low(Filter_N)         # in downtrend
       Entry: market sell at open

Exit:  Hold N days (time exit) OR exit when gap closes (pattern exit)
```

**Edge source:** Gap + trend alignment. Trend filter is critical — removes noise gaps against the trend.
**Key finding:** Longer hold period preferred. Without trend filter → drops to C.
**Equity adaptation:** Long-only works. Skip short leg if no margin enabled.

---

### 2. Ross Hook with Filter — Exit 1 (Time Exit)

**URL:** https://oxfordstrat.com/trading-strategies/ross-hook-filter-2/

| Attribute | Detail |
|-----------|--------|
| Signal type | Trend continuation pattern |
| Direction | Long + Short |
| Entry | Buy/sell stop beyond hook extreme |
| Exit | Time exit (hold N bars) + ATR stop loss |
| Filter | Hook size ≤ prior drawdown × Max_Hook (keeps pullbacks proportional) |
| Asset fit | Stocks ⚠️ ETFs ⚠️ Crypto ✅ |
| Complexity | High (swing point detection needed) |
| Timeframe | Daily bars |

**Rules:**
```
Setup:
  Bull 1-2-3: swing low → swing high → higher swing low (3 points)
  Ross Hook:  first pullback after breakout above point 2

Filter:
  Hook size (high - low) ≤ (prior drawdown) × Max_Hook
  Max_Hook in [0.25, 2.0] — keeps hook small relative to move

Entry:
  Long:  buy stop 1 tick above hook's highest high
  Short: sell stop 1 tick below hook's lowest low

Exit:
  Time exit: close position after Time_Index bars
  ATR stop:  Entry ± ATR(20) × 6
```

**Edge source:** Entering on first pullback in a confirmed trend. Filter removes deep corrections (likely trend failures).
**Key finding:** Smaller hooks (Max_Hook < 0.75) significantly outperform. ATR stop prevents catastrophic loss.
**Equity adaptation:** Swing detection adds code complexity. Best applied to trending names (NVDA in bull runs).

---

### 3. Ross Hook with Filter — Exit 2 (Target Exit)

**URL:** https://oxfordstrat.com/trading-strategies/ross-hook-filter-3/

Same setup and filter as Exit 1. Exit differs:

```
Exit:
  Target: Close ≥ Entry + (Initial_Risk × Target_Index)   # profit target
  Quick:  Close below hook's low (momentum failed)
  ATR stop: Entry - ATR(20) × 6
  Target_Index in [1.0, 10.0] — risk-reward multiple
```

**vs Exit 1:** Target exit locks in profits faster. Time exit holds through noise. Both rate B — target exit has higher % winning trades; time exit has larger average wins.

---

### 4. Smash Day — Type B (Filter & Exit)

**URL:** https://oxfordstrat.com/trading-strategies/smash-day-pattern-b1/

| Attribute | Detail |
|-----------|--------|
| Signal type | Volatility expansion after range breach |
| Direction | Long + Short |
| Entry | Buy/sell stop beyond prior bar's extreme |
| Exit | Time exit + quick exit + ATR stop |
| Filter | Trend direction (close vs N-bar-ago close) |
| Asset fit | Stocks ✅ ETFs ✅ Crypto ✅ |
| Complexity | Very low — 2 lines of math |
| Timeframe | Daily bars |

**Rules:**
```
Long Setup:  Close[i-1] > High[i-2]    # yesterday closed above 2-day-ago high
Trend Filter: Close[i-1] > Close[i-1-Trend_N]   # in uptrend

Entry: Buy stop 1 tick above High[i-1]

Short Setup: Close[i-1] < Low[i-2]
Trend Filter: Close[i-1] < Close[i-1-Trend_N]

Entry: Sell stop 1 tick below Low[i-1]

Exit:
  Time:  hold N bars then close
  Quick: price returns below entry bar's low (momentum failed fast)
  ATR:   Entry - ATR(20) × ATR_Stop
```

**Edge source:** Strong close breaking prior range signals momentum continuation. Trend filter removes counter-trend traps.
**Key finding:** Type B setup is the profitable variant. Type A (weaker breach) rates C. Type C rates D. The specific close-above-high condition matters.

---

### 5. Smash Day — Type B (Exits Only)

**URL:** https://oxfordstrat.com/trading-strategies/smash-day-pattern-b2/

Same entry setup as #4. Tests additional exit combinations:

```
Exit variants tested:
  Time Exit:   close after N bars
  Target Exit: close if profit ≥ Risk × Target_Index
  Both:        take whichever triggers first
```

**vs Strategy 4:** This variant adds the target exit on top of time exit. Rating is also B — the combined exit outperforms either alone when Target_Index is in the 2–4× range.

---

## Head-to-Head Comparison

| Criteria | Gap Pattern A | Ross Hook Exit 1 | Ross Hook Exit 2 | Smash Day B-1 | Smash Day B-2 |
|----------|--------------|-----------------|-----------------|---------------|---------------|
| Complexity | Low | High | High | Very Low | Very Low |
| Data needed | OHLC daily | OHLC + swing detection | OHLC + swing detection | OHLC daily | OHLC daily |
| Signal frequency | Low (gaps rare) | Low-Medium | Low-Medium | Medium | Medium |
| Works long-only | Yes | Yes | Yes | Yes | Yes |
| Crypto fit | Strong | Strong | Strong | Strong | Strong |
| Stock fit | Strong | Moderate | Moderate | Strong | Strong |
| ETF fit | Strong | Moderate | Moderate | Strong | Strong |
| Best for | Gap-prone stocks, gap at open | Trending markets, NVDA/BTC | Trending markets | Any liquid daily-bar asset | Any liquid daily-bar asset |
| Implement first | 2nd | 4th | 5th | **1st** | 3rd |

---

## Income Target Reality Check

**Target stated:** 10% gain on 10% of capital, daily

| Metric | Reality |
|--------|---------|
| Implied daily return | 1% of portfolio/day |
| Annualized (compounded) | ~1,100% per year |
| Oxford B-rated strategies (best case) | 15–20% CAGR |
| Best hedge funds globally | 20–40% CAGR |
| Daily 1% consistently | Does not exist as a systematic strategy |

**What these strategies actually deliver (futures portfolio, before costs):**
- CAGR: ~15–20%
- Sharpe: 0.7–1.2
- Max drawdown: 30–50%
- % winning trades: 30–45% (large wins offset many small losses)

**Realistic daily income framing:**

If you invest $10,000 total and size 10% per trade ($1,000):
- Realistic target per trade: 1–3% gain = $10–$30 per trade
- Frequency: 2–5 signals/week across a watchlist of 10–15 stocks
- Monthly realistic P&L: $80–$400 (0.8–4% of capital/month)

To generate $X/day reliably you need:
- Large enough capital (the strategy, not position size, drives daily income)
- Diversification across many uncorrelated assets
- Accept that daily income smooths out over weeks, not literally every day

**Better framing:** Target monthly return of 2–5%, accept daily variance. These strategies are not scalping — they hold positions days to weeks.

---

## Recommended Implementation Order

1. **Smash Day Type B** — implement first (2 lines of logic, works everywhere)
2. **Gap Pattern Type A** — implement second (slightly more complex, excellent for gap stocks)
3. **Smash Day B with Target Exit** — add target exit variant to #1
4. **Ross Hook Filter** — implement last (requires swing point detection library)

---

## Data Sources by Strategy Component

| Component | Source |
|-----------|--------|
| Daily OHLC | Alpaca API (`/v2/stocks/{symbol}/bars`) or `yfinance` |
| Gap detection | First bar of day vs prior day's close |
| ATR calculation | Rolling `pandas` computation on daily bars |
| Trend filter | `rolling(N).max()` / `rolling(N).min()` |
| Swing detection (Ross Hook) | `scipy.signal.argrelextrema` or custom N-bar high/low |
| Trade execution | Alpaca API (paper) → Robinhood MCP (live) |

---

*All Oxford strategy results are hypothetical backtests on futures. Equity/crypto forward performance requires independent validation. CFTC Rule 4.41 applies.*
