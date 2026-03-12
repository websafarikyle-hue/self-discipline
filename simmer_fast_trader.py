from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol


@dataclass(frozen=True)
class FastEvent:
    event_id: str
    asset: str
    question: str
    opens_at: datetime
    resolves_at: datetime
    yes_price: float
    spread_cents: float
    tradable: bool
    liquidity_score: float | None = None

    @property
    def time_remaining_s(self) -> float:
        return max((self.resolves_at - now_utc()).total_seconds(), 0.0)


@dataclass(frozen=True)
class Signal:
    model_prob_up: float
    reason: str


@dataclass(frozen=True)
class TradeDecision:
    should_trade: bool
    side: str | None
    edge: float
    net_edge: float
    reason: str


@dataclass
class TraderConfig:
    min_time_remaining_s: int = 60
    max_spread_cents: float = 8.0
    min_liquidity_score: float = 0.3
    fee_rate: float = 0.10
    slippage_buffer: float = 0.01
    min_net_edge: float = 0.01
    max_position_usd: float = 100.0
    max_total_exposure_usd: float = 300.0


class SimmerClient(Protocol):
    def get_fast_markets(self, asset: str, window: str) -> list[dict[str, Any]]: ...

    def import_market(self, event_id: str) -> dict[str, Any]: ...

    def place_order(self, event_id: str, side: str, size_usd: float) -> dict[str, Any]: ...


class FastTrader:
    def __init__(self, client: SimmerClient, config: TraderConfig | None = None, dedup_file: str = ".trade_dedup.json") -> None:
        self.client = client
        self.config = config or TraderConfig()
        self.dedup_file = Path(dedup_file)
        self._traded_event_ids = self._load_traded_event_ids()

    def discover_current_event(self, asset: str, window: str = "5m") -> tuple[FastEvent | None, FastEvent | None]:
        raw_markets = self.client.get_fast_markets(asset=asset, window=window)
        events = sorted((self._parse_event(asset, m) for m in raw_markets), key=lambda e: e.opens_at)
        current: FastEvent | None = None
        future: FastEvent | None = None
        ts = now_utc()
        for event in events:
            if event.opens_at <= ts < event.resolves_at:
                current = event
                break
            if event.opens_at > ts and future is None:
                future = event
        return current, future

    def is_event_tradable(self, event: FastEvent) -> tuple[bool, str]:
        if not event.tradable:
            return False, "event not tradable"
        if event.time_remaining_s < self.config.min_time_remaining_s:
            return False, "insufficient time remaining"
        if event.spread_cents > self.config.max_spread_cents:
            return False, "spread too wide"
        if event.liquidity_score is not None and event.liquidity_score < self.config.min_liquidity_score:
            return False, "liquidity too low"
        if event.event_id in self._traded_event_ids:
            return False, "event already traded"
        return True, "tradable"

    def evaluate_edge(self, event: FastEvent, signal: Signal) -> TradeDecision:
        market_prob = clip01(event.yes_price)
        model_prob = clip01(signal.model_prob_up)
        raw_edge_yes = model_prob - market_prob
        raw_edge_no = (1 - model_prob) - (1 - market_prob)
        side = "YES" if raw_edge_yes >= raw_edge_no else "NO"
        raw_edge = max(raw_edge_yes, raw_edge_no)

        spread_cost = event.spread_cents / 100.0
        net_edge = raw_edge - self.config.fee_rate - spread_cost - self.config.slippage_buffer
        if net_edge <= self.config.min_net_edge:
            return TradeDecision(False, None, raw_edge, net_edge, "insufficient net edge")
        return TradeDecision(True, side, raw_edge, net_edge, signal.reason)

    def execute_trade(self, event: FastEvent, decision: TradeDecision) -> dict[str, Any]:
        if not decision.should_trade or not decision.side:
            return {"ok": False, "reason": decision.reason}

        import_res = self.client.import_market(event.event_id)
        if not import_res.get("ok"):
            return {"ok": False, "reason": "import failed", "detail": import_res}

        refreshed_current, _ = self.discover_current_event(asset=event.asset)
        if not refreshed_current or refreshed_current.event_id != event.event_id:
            return {"ok": False, "reason": "event changed during import"}

        tradable, reason = self.is_event_tradable(refreshed_current)
        if not tradable:
            return {"ok": False, "reason": f"post-import check failed: {reason}"}

        size = self._size_position(decision.net_edge)
        order_res = self.client.place_order(event_id=event.event_id, side=decision.side, size_usd=size)
        if order_res.get("ok"):
            self._traded_event_ids.add(event.event_id)
            self._save_traded_event_ids()
        return order_res

    def _size_position(self, net_edge: float) -> float:
        scaled = self.config.max_position_usd * min(max(net_edge / 0.05, 0.1), 1.0)
        return round(min(scaled, self.config.max_total_exposure_usd), 2)

    def _parse_event(self, asset: str, row: dict[str, Any]) -> FastEvent:
        return FastEvent(
            event_id=str(row.get("event_id") or row.get("id") or ""),
            asset=asset,
            question=str(row.get("question", "")),
            opens_at=parse_dt(row.get("opens_at")),
            resolves_at=parse_dt(row.get("resolves_at")),
            yes_price=float(row.get("current_probability") or row.get("yes_price") or 0.5),
            spread_cents=float(row.get("spread_cents") or 0.0),
            tradable=bool(row.get("tradable", row.get("is_live_now", False))),
            liquidity_score=(float(row["liquidity_score"]) if row.get("liquidity_score") is not None else None),
        )

    def _load_traded_event_ids(self) -> set[str]:
        if not self.dedup_file.exists():
            return set()
        data = json.loads(self.dedup_file.read_text(encoding="utf-8"))
        return set(data.get("traded_event_ids", []))

    def _save_traded_event_ids(self) -> None:
        payload = {"traded_event_ids": sorted(self._traded_event_ids), "updated_at": now_utc().isoformat()}
        self.dedup_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc)
    if not raw:
        return now_utc()
    text = str(raw).replace("Z", "+00:00")
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def clip01(v: float) -> float:
    return min(max(v, 0.0), 1.0)


def run_once(client: SimmerClient, assets: Iterable[str], signal_fn, window: str = "5m") -> list[dict[str, Any]]:
    trader = FastTrader(client)
    outputs: list[dict[str, Any]] = []
    for asset in assets:
        current, nxt = trader.discover_current_event(asset=asset, window=window)
        if not current:
            outputs.append({"asset": asset, "status": "no_current_event", "next_event": nxt.event_id if nxt else None})
            continue

        tradable, reason = trader.is_event_tradable(current)
        if not tradable:
            outputs.append({"asset": asset, "event_id": current.event_id, "status": "skip", "reason": reason})
            continue

        signal = signal_fn(asset=asset, event=current)
        decision = trader.evaluate_edge(current, signal)
        if not decision.should_trade:
            outputs.append({"asset": asset, "event_id": current.event_id, "status": "skip", "reason": decision.reason})
            continue

        res = trader.execute_trade(current, decision)
        outputs.append({"asset": asset, "event_id": current.event_id, "status": "traded" if res.get("ok") else "error", "detail": res})
    return outputs


def run_loop(client: SimmerClient, assets: Iterable[str], signal_fn, every_seconds: int = 60) -> None:
    while True:
        result = run_once(client, assets, signal_fn)
        print(json.dumps({"at": now_utc().isoformat(), "result": result}, ensure_ascii=False))
        time.sleep(every_seconds)
