"""Clear position_owners rows left behind by out-of-band sells (e.g. a liquidation
script that hit the broker directly instead of going through pipeline.py, which is
the only place that calls repo.clear_position_owner on a confirmed sell).

A stale row wrongly blocks other strategies from buying a symbol the DB says is
still "owned" even though the broker shows zero shares.

    python scripts/clean_stale_owners.py            # dry run, prints what's stale
    python scripts/clean_stale_owners.py --apply     # actually clears them

Requires ALPACA_API_KEY/ALPACA_SECRET_KEY and DATABASE_URL in .env.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trader.config import load_config
from trader.execution.broker import AlpacaBroker
from trader.portfolio.postgres_repo import PostgresRepository


def main() -> None:
    apply = "--apply" in sys.argv

    config = load_config()
    config.require_alpaca()
    if not config.database_url:
        raise RuntimeError("DATABASE_URL required")

    broker = AlpacaBroker(config)
    repo = PostgresRepository(config.database_url)

    state = broker.reconcile()
    if state.stale:
        raise RuntimeError("broker state is stale — refusing to clean against unknown positions")

    # DB owner symbols carry a slash for crypto ("BTC/USD"); broker positions don't
    # ("BTCUSD"). Normalize both sides before comparing, or every held crypto pair
    # looks orphaned.
    held = {s.replace("/", "") for s in state.positions}
    owners = repo.get_position_owners()
    stale = [(symbol, pool, strategy) for (symbol, pool), strategy in owners.items()
              if symbol.replace("/", "") not in held]

    print(f"{len(owners)} owner rows, {len(held)} live positions, {len(stale)} stale")
    for symbol, pool, strategy in stale:
        print(f"  {'clearing' if apply else 'would clear'}: {symbol}/{pool} (was {strategy})")
        if apply:
            repo.clear_position_owner(symbol, pool)

    if not apply and stale:
        print("\nDry run — rerun with --apply to clear these rows.")


if __name__ == "__main__":
    main()
