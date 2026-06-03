"""Personal trading-agent package (paper-first, Alpaca).

This package houses the disciplined trading core. Phase 0-2 (this iteration) ships:
- config:   environment + paper/live + autonomy flag + risk-limit placeholders
- data:     historical bars from Alpaca (the strategy/trading data source)
- strategy: the Strategy contract + concrete quant signals + indicators
- backtest: the bar-replay harness that proves edge before any real money

Phases 3-7 (risk gate, execution, portfolio DB, decision pipeline, LLM overlay,
autonomy toggle) are documented in the plan but not implemented here.
"""
