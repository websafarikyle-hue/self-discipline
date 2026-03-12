# Simmer Fast Market Trader Skill

本 skill 提供一套**可执行且可审计**的 fast market 交易闭环，修复常见的“只监控不交易”与“用 50¢ 偏离代替真实 edge”问题。

## Scope

代码文件：
- `multi_monitor.py`：只做 market presence / current event 监控
- `simmer_fast_trader.py`：完整交易流程（发现 → 筛选 → 信号 → edge → 执行 → 去重）

## 设计原则

1. **先定位 current event，再判断可交易性**，不依赖单一 `is_live_now`。
2. **edge 使用 model_prob - market_prob**，不是 `abs(prob - 0.5)`。
3. **执行前必须过净 edge 门槛**：`fee + spread + slippage`。
4. **事件级去重**：同一 `event_id` 不重复下单。
5. **import 后二次校验**：防止窗口切换造成过期下单。

## 交易流程（run_once）

1. 发现当前 event：`discover_current_event(asset, window)`
2. 可交易性检查：
   - 剩余时间 > `min_time_remaining_s`
   - spread <= `max_spread_cents`
   - liquidity >= `min_liquidity_score`（如有）
   - event 未交易过
3. 生成信号：外部注入 `signal_fn(asset, event) -> Signal(model_prob_up, reason)`
4. 计算净 edge：
   - raw edge = `max(model_prob - market_prob, (1-model_prob)-(1-market_prob))`
   - net edge = `raw edge - fee - spread - slippage`
5. 下单执行：
   - import market
   - 再次发现并确认 event 未切换
   - 再次通过可交易检查
   - 下单并写入 `.trade_dedup.json`

## 配置项（TraderConfig）

- `min_time_remaining_s`（默认 60）
- `max_spread_cents`（默认 8）
- `min_liquidity_score`（默认 0.3）
- `fee_rate`（默认 0.10）
- `slippage_buffer`（默认 0.01）
- `min_net_edge`（默认 0.01）
- `max_position_usd`（默认 100）
- `max_total_exposure_usd`（默认 300）

## 使用说明

### 1) 先做监控（不交易）

调用 `monitor_markets(client, assets)`，查看：
- current event id
- 窗口起止时间
- 剩余时间 / spread
- 是否可交易以及 skip 原因

### 2) 再启用交易

调用 `run_once(client, assets, signal_fn)` 或 `run_loop(...)`。

> `signal_fn` 需要返回 `Signal`，例如结合 Binance 动量模型输出 `model_prob_up`。

## 最低接入要求（对接 Simmer）

`client` 需实现：
- `get_fast_markets(asset, window)`
- `import_market(event_id)`
- `place_order(event_id, side, size_usd)`

## 风控说明

fast 5m/15m 市场的风险控制应前置在进场前：
- 单笔仓位上限
- 组合敞口上限
- 事件级去重
- 净 edge 门槛

不要依赖慢速的事后止损轮询来补救 fast market 风险。
