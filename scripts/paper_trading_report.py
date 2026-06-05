"""Paper trading report: summarise auto-mode run history from the portfolio DB and
the Alpaca equity curve, then emit a PASS/FAIL verdict for the go-live gate.

Usage:
    python scripts/paper_trading_report.py

Exit codes:
    0 — PASS: ≥1 auto-mode pipeline run AND ≥1 filled order recorded
    1 — FAIL: no paper data yet, or no fills found
    2 — configuration error
"""
from __future__ import annotations

import os
import sys

# Ensure project root is on sys.path so `trader` is importable when the script is
# invoked from any working directory (e.g. `python scripts/paper_trading_report.py`).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def main() -> int:
    try:
        from trader.config import load_config
        config = load_config()
    except Exception as exc:
        print(f"ERROR: could not load config — {exc}")
        return 2

    db_path = config.portfolio_db_path

    # Fail gracefully if no DB exists yet.
    if not os.path.exists(db_path):
        print(
            f"No paper trading data found at {db_path!r}.\n"
            "Run the scheduler in AUTONOMY=auto on paper first, then re-run this report."
        )
        return 1

    from trader.portfolio.sqlite_repo import SQLiteRepository
    repo = SQLiteRepository(db_path)

    # ---- runs ----
    with repo._connect() as conn:
        runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id").fetchall()]
        signals = [dict(r) for r in conn.execute("SELECT * FROM signals ORDER BY id").fetchall()]

    auto_runs = [r for r in runs if r.get("mode") == "auto"]
    manual_runs = [r for r in runs if r.get("mode") != "auto"]

    print("=" * 60)
    print("Paper Trading Report")
    print("=" * 60)
    print(f"Total pipeline runs : {len(runs)}")
    print(f"  auto-mode runs    : {len(auto_runs)}")
    print(f"  manual-mode runs  : {len(manual_runs)}")
    if auto_runs:
        print(f"  first auto run    : {auto_runs[0]['started_at']}")
        print(f"  last auto run     : {auto_runs[-1]['started_at']}")
    print(f"Signals generated   : {len(signals)}")

    # ---- proposals ----
    proposals = repo.list_pending_proposals()
    with repo._connect() as conn:
        all_proposals = [
            dict(r) for r in conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
        ]
    approved = [p for p in all_proposals if p.get("status") == "approved"]
    rejected = [p for p in all_proposals if p.get("status") == "rejected"]

    print(f"\nProposals")
    print(f"  total             : {len(all_proposals)}")
    print(f"  approved          : {len(approved)}")
    print(f"  rejected          : {len(rejected)}")
    print(f"  pending           : {len(proposals)}")

    # ---- orders ----
    orders = repo.get_orders()
    filled = [o for o in orders if o.get("status") == "filled"]
    pending_orders = [o for o in orders if o.get("status") not in ("filled", "cancelled")]

    print(f"\nOrders")
    print(f"  total             : {len(orders)}")
    print(f"  filled            : {len(filled)}")
    print(f"  pending/other     : {len(pending_orders)}")

    if filled:
        symbols_traded = sorted({o["symbol"] for o in filled})
        print(f"  symbols traded    : {', '.join(symbols_traded)}")

    # ---- trades ----
    with repo._connect() as conn:
        trades = [dict(r) for r in conn.execute("SELECT * FROM trades ORDER BY id").fetchall()]

    print(f"\nTrades recorded     : {len(trades)}")

    # ---- Alpaca equity curve (optional) ----
    print("\nAlpaca portfolio history :", end=" ")
    try:
        from trader.execution.broker import AlpacaBroker
        broker = AlpacaBroker(config)
        history = broker.get_portfolio_history()
        if history and history.get("equity"):
            equity_series = history["equity"]
            non_null = [v for v in equity_series if v is not None]
            if non_null:
                start_eq = non_null[0]
                end_eq = non_null[-1]
                pnl_pct = (end_eq / start_eq - 1.0) if start_eq else 0.0
                print(f"${start_eq:,.0f} → ${end_eq:,.0f}  ({pnl_pct:+.1%})")
            else:
                print("no data points")
        else:
            print("empty response")
    except Exception as exc:
        print(f"unavailable ({exc})")

    # ---- PDT informational check ----
    from datetime import date
    today_str = date.today().isoformat()
    today_fills = [
        o for o in filled
        if o.get("ts", "").startswith(today_str)
    ]
    day_trades_approx = len(today_fills) // 2
    print(f"\nPDT check (today)   : ~{day_trades_approx} day-trade(s) "
          f"({len(today_fills)} fills today)")
    if day_trades_approx >= 3:
        print("  WARNING: approaching PDT limit (3 round-trips) — PDT guard will block buys")

    # ---- verdict ----
    print("\n" + "=" * 60)
    has_auto_run = len(auto_runs) >= 1
    has_fill = len(filled) >= 1

    if has_auto_run and has_fill:
        print("PAPER REPORT: PASS")
        print("  (≥1 auto-mode run recorded, ≥1 filled order confirmed)")
        return 0
    else:
        reasons = []
        if not has_auto_run:
            reasons.append("no auto-mode pipeline runs in DB")
        if not has_fill:
            reasons.append("no filled orders recorded")
        print("PAPER REPORT: FAIL")
        for r in reasons:
            print(f"  • {r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
