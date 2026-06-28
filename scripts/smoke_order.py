"""Phase 3 smoke test: drive one order through the real paper order path.

Reconciles live paper state, runs one allowlisted intent through the risk gate, submits a
small notional paper order with a deterministic client_order_id, records it, then re-runs
the submit to prove idempotency (still one order). Honours the kill switch.

    python scripts/smoke_order.py [SYMBOL] [NOTIONAL]

Requires ALPACA_API_KEY / ALPACA_SECRET_KEY (paper) in .env. Places a REAL paper order.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import load_config
from trader.data.alpaca_bars import get_daily_bars
from trader.execution.broker import AlpacaBroker, client_order_id_for
from trader.portfolio.postgres_repo import PostgresRepository
from trader.portfolio.repository import OrderRow
from trader.risk.gate import KillSwitch, OrderIntent, RiskGate


def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "AAPL").upper()
    notional = float(sys.argv[2]) if len(sys.argv) > 2 else 50.0

    config = load_config()
    config.require_alpaca()

    broker = AlpacaBroker(config)
    gate = RiskGate(config.risk)
    kill_switch = KillSwitch(config.kill_switch_path)
    if not config.database_url:
        raise RuntimeError("DATABASE_URL required")
    repo = PostgresRepository(config.database_url)

    print(f"Order path smoke ({'PAPER' if config.alpaca_paper else 'LIVE'}): "
          f"{symbol} ${notional:.2f}")

    state = broker.reconcile()
    print(f"  reconciled: equity=${state.equity:,.2f} "
          f"positions={state.positions} stale={state.stale}")

    end = datetime.now(timezone.utc)
    from datetime import timedelta
    bars = get_daily_bars(symbol, start=end - timedelta(days=365), end=end, config=config)
    if bars.empty:
        print("  no bars; cannot price the intent")
        return
    ref_price = float(bars.iloc[-1]["close"])

    intent = OrderIntent(symbol=symbol, side="buy", notional=notional,
                         ref_price=ref_price, reason="smoke test")
    decision = gate.evaluate(intent, state, kill_switch)
    print(f"  risk gate: approved={decision.approved} "
          f"notional=${decision.approved_notional:.2f} ({decision.reason})")
    if not decision.approved:
        return

    coid = client_order_id_for(end.date(), symbol, "buy", "smoke")
    order = broker.submit(symbol=symbol, side="buy",
                          notional=decision.approved_notional, client_order_id=coid)
    repo.record_order(OrderRow(coid, symbol, "buy", decision.approved_notional,
                               str(getattr(order, "status", "submitted")),
                               str(getattr(order, "id", None))))
    print(f"  submitted: client_order_id={coid} status={getattr(order, 'status', '?')}")

    # Idempotency: same id again must not create a second order.
    again = broker.submit(symbol=symbol, side="buy",
                          notional=decision.approved_notional, client_order_id=coid)
    same = getattr(order, "id", None) == getattr(again, "id", None)
    print(f"  idempotent re-submit: same order={same} "
          f"(orders in DB: {len(repo.get_orders())})")


if __name__ == "__main__":
    main()
