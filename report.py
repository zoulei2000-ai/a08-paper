"""纸面交易汇总报告：python3 report.py"""
import csv, json, os, time
HERE = os.path.dirname(os.path.abspath(__file__))


def rows(name):
    p = os.path.join(HERE, name)
    return list(csv.DictReader(open(p))) if os.path.exists(p) else []


st = json.load(open(os.path.join(HERE, "state.json"))) if os.path.exists(os.path.join(HERE, "state.json")) else None
if not st:
    print("还没有运行记录。先运行：python3 paper_trader.py --loop"); raise SystemExit
eq = st["cash"] + sum(p["cost"] for p in st["positions"].values())
days = (time.time() - st["started"]) / 86400
sig, trd, v3 = rows("signals.csv"), rows("trades.csv"), rows("v3_check.csv")
buys = [r for r in trd if r["type"] == "BUY"]; sets = [r for r in trd if r["type"] == "SETTLE"]
wins = [r for r in sets if float(r["pnl"]) > 0]
print(f"== A08 纸面交易报告（运行 {days:.1f} 天）==")
print(f"权益 ${eq:,.2f}（起始 $10,000，{eq / 10000:.3f}x）| 峰值 ${st['peak']:,.2f} | 熔断：{'是' if st['halted'] else '否'}")
print(f"信号 {len(sig)} 个 | 纸面成交 {len(buys)} 笔 | 已结算 {len(sets)} 笔（胜 {len(wins)}）| 持仓 {len(st['positions'])} 个")
reasons = {}
for r in sig:
    reasons[r["result"]] = reasons.get(r["result"], 0) + 1
print("信号去向：" + "，".join(f"{k or '其它'} {v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
if sets:
    print(f"已实现盈亏 ${sum(float(r['pnl']) for r in sets):+,.2f}")
d = [float(r["diff"]) for r in v3 if r["diff"] not in ("", None)]
if d:
    d.sort()
    print(f"实盘盘口成交价 − 裁判v3假设成交价：中位 {d[len(d)//2]:+.4f}，样本 {len(d)}（>+0.02 说明实盘成交明显差于回测假设）")
nov3 = sum(1 for r in v3 if r["v3_fill"] in ("", None))
if v3:
    print(f"信号后30分钟内无真实成交（回测会作废）：{nov3}/{len(v3)}")
for tok, p in list(st["positions"].items())[:15]:
    print(f"  持仓 {p['side']:3s} {p['q']:28s} 成本 ${p['cost']:8.2f}  入场 {time.strftime('%m-%d %H:%M', time.gmtime(p['t']))}")
