# A08 纸面交易（GitHub Actions 版）

Polymarket 加密行权价阶梯市场的现货趋势跟随策略（沙盒淘汰赛第 5 轮胜出策略 A08）的**前向纸面测试**。
**只读公开数据，不下任何真实订单。**

- 每小时由 GitHub Actions 运行一次 `python paper_trader.py --once`，结果（`state.json`、`signals.csv`、`trades.csv`、`equity.csv`、`v3_check.csv`、`log.txt`）自动提交回仓库。
- 出现信号 / 持仓结算 / 回撤熔断时发 Telegram 通知（令牌放在仓库 Secrets：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`）。
- 查看汇总：克隆仓库后 `python report.py`，或直接在网页上看 `log.txt` / `equity.csv`。
- 行情：Binance 公共镜像 `data-api.binance.vision`（美国机房可访问）；Polymarket gamma / CLOB / data-api 只读接口。

策略参数：z72 ≥ 2.5、30 日趋势同向（gate 0.25）、公允价 0.10–0.90、每笔 5% 权益、持有到结算。
风控：最多吃到最优卖价 +2¢；单笔 ≤ 近 24h 成交额 10%；单币种敞口 ≤ 30%、总敞口 ≤ 70%；回撤 > 35% 暂停开新仓。
