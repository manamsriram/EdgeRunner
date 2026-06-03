"""Personal trading-agent package (paper-first, Alpaca).

This package houses the disciplined trading core. Shipped so far:
- config:    environment + paper/live + autonomy flag + risk limits
- data:      historical bars from Alpaca (the strategy/trading data source)
- strategy:  the Strategy contract + concrete quant signals + indicators
- backtest:  the bar-replay harness that proves edge before any real money
- risk:      the fail-closed risk gate every order must pass + kill switch  (Phase 3)
- execution: Alpaca broker adapter — reconcile + idempotent orders          (Phase 3)
- portfolio: PortfolioRepository interface + SQLite store for the audit log (Phase 3)

Phases 4-7 (decision pipeline + approval queue, scheduler/dashboard, Claude overlay,
autonomy toggle) are documented in the plan but not implemented here.
"""
