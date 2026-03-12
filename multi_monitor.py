from __future__ import annotations

import json
from typing import Any, Iterable, Protocol

from simmer_fast_trader import FastTrader


class SimmerClient(Protocol):
    def get_fast_markets(self, asset: str, window: str) -> list[dict[str, Any]]: ...


def monitor_markets(client: SimmerClient, assets: Iterable[str], window: str = "5m") -> list[dict[str, Any]]:
    trader = FastTrader(client)
    summaries: list[dict[str, Any]] = []

    for asset in assets:
        current, nxt = trader.discover_current_event(asset=asset, window=window)
        if not current:
            summaries.append({
                "asset": asset,
                "status": "no_current_event",
                "next_event": {
                    "event_id": nxt.event_id if nxt else None,
                    "opens_at": nxt.opens_at.isoformat() if nxt else None,
                },
            })
            continue

        tradable, reason = trader.is_event_tradable(current)
        summaries.append({
            "asset": asset,
            "status": "current_event_found",
            "event": {
                "event_id": current.event_id,
                "question": current.question,
                "opens_at": current.opens_at.isoformat(),
                "resolves_at": current.resolves_at.isoformat(),
                "time_remaining_s": int(current.time_remaining_s),
                "yes_price": current.yes_price,
                "spread_cents": current.spread_cents,
            },
            "tradable": tradable,
            "skip_reason": None if tradable else reason,
        })

    return summaries


def pretty_print(summaries: list[dict[str, Any]]) -> None:
    print(json.dumps({"markets": summaries}, ensure_ascii=False, indent=2))
