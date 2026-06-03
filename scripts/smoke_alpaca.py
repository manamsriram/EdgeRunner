"""Phase 0 smoke test: connect to Alpaca PAPER and prove data access.

Prints the paper account number + buying power, then fetches one recent daily bar for
AAPL. Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (see .env.example).

    python scripts/smoke_alpaca.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow running as `python scripts/smoke_alpaca.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import load_config
from trader.data.alpaca_bars import get_daily_bars


def main() -> None:
    from alpaca.trading.client import TradingClient

    config = load_config()
    config.require_alpaca()

    client = TradingClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
        paper=config.alpaca_paper,
    )
    account = client.get_account()
    print(f"Connected to Alpaca {'PAPER' if config.alpaca_paper else 'LIVE'}")
    print(f"  account number: {account.account_number}")
    print(f"  buying power:   ${float(account.buying_power):,.2f}")

    end = datetime.utcnow()
    start = end - timedelta(days=10)
    bars = get_daily_bars("AAPL", start=start, end=end, config=config)
    if bars.empty:
        print("  no AAPL bars returned (market data entitlement?)")
    else:
        last = bars.iloc[-1]
        print(f"  AAPL last daily bar ({bars.index[-1].date()}): "
              f"O={last['open']:.2f} H={last['high']:.2f} "
              f"L={last['low']:.2f} C={last['close']:.2f} V={int(last['volume'])}")


if __name__ == "__main__":
    main()
