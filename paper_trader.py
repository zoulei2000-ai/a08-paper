"""A08 胜出策略 —— 纸面交易（前向测试）。不下任何真实订单，只读公开数据并记录“本应下的单”。

每小时 HH:01 UTC 运行一次（--loop 常驻，或 --once 单次）：
  1. 拉 Binance 实时 1h K 线（只用已收盘 K 线），计算 z72 / z720 / σ —— 与回测完全相同（复用 A08 core.py）。
  2. 拉 Polymarket 当前开放的加密行权价市场（触价类 + multi-strikes），按 A08 规则生成本小时信号。
  3. 对每个新信号读取 CLOB 实时盘口，模拟吃单：最多吃到 最优卖价+2¢，金额 = min(5% 权益, 近24h成交额10%, 盘口深度, 敞口上限)。
  4. 信号 40 分钟后补记“裁判 v3 口径”成交价（信号后 30 分钟真实成交中位价），用于和回测假设对账。
  5. 检查持仓市场是否已结算，按官方结算价回款。
输出（sandbox/paper/）：state.json、signals.csv、trades.csv、equity.csv、log.txt；汇总见 report.py。
"""
import argparse, csv, json, math, os, sys, time, traceback
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BINANCE = os.environ.get("BINANCE_BASE", "https://data-api.binance.vision")  # 公共行情镜像，美国机房可访问
import pm
import verify
import core  # A08 第5轮信号代码（原样复用）

# ---- 策略参数（与 A08 第5轮回测一致）----
Z, GATE, SIZE, MAX_EV = 2.5, 0.25, 0.05, 3
# ---- 实盘风控参数（回测中没有，纸面交易新增）----
START_CASH = 10_000.0
MAX_SLIP = 0.02          # 最多吃到 最优卖价 + 2¢
VOL_CAP = 0.10           # 单笔 ≤ 近 24h 成交额 10%
ASSET_CAP = 0.30         # 单币种总敞口 ≤ 30% 权益
TOTAL_CAP = 0.70         # 总敞口 ≤ 70% 权益
MIN_ORDER = 5.0          # 小于 $5 不下单
DD_HALT = 0.35           # 回撤超过 35% 暂停开新仓（需人工复核）

STATE = os.path.join(HERE, "state.json")
SIG_CSV = os.path.join(HERE, "signals.csv")
TRD_CSV = os.path.join(HERE, "trades.csv")
EQ_CSV = os.path.join(HERE, "equity.csv")
LOG = os.path.join(HERE, "log.txt")
S = requests.Session()


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S UTC ", time.gmtime()) + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


TG_ENV = os.path.join(HERE, "telegram.env")


def tg(msg):
    """发送 Telegram 通知。令牌与 chat_id 从 telegram.env 读取（用户自行填写，不入日志）。"""
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    try:
        cfg = {}
        for line in (open(TG_ENV) if not (tok and chat) else []):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                cfg[k.strip()] = v.strip().strip('"')
        tok, chat = tok or cfg.get("TELEGRAM_BOT_TOKEN"), chat or cfg.get("TELEGRAM_CHAT_ID")
    except FileNotFoundError:
        return None
    if not tok or not chat:
        return None
    err = ""
    for i in range(4):  # 代理偶发超时：最多重试 4 次
        try:
            r = S.post(f"https://api.telegram.org/bot{tok}/sendMessage", data=dict(chat_id=chat, text=msg), timeout=20)
            if r.ok:
                return True
            err = f"HTTP {r.status_code}"
            if r.status_code in (400, 401, 403, 404):
                break  # 令牌/chat_id 错误，重试无用
        except Exception as e:
            err = type(e).__name__
        time.sleep(3 * (i + 1))
    log(f"Telegram 发送失败：{err}")
    return False


def get(url, params=None, tries=5, timeout=40):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(3 * (i + 1)); continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return dict(cash=START_CASH, peak=START_CASH, positions={}, seen=[], ev_entries={}, pending_v3=[],
                halted=False, started=int(time.time()))


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


# ---------- 数据 ----------
def live_klines(asset, start=None, end=None):
    """Binance 最近约 2000 根 1h K 线 [open_ms, high, low, close]，含正在形成的那根（core.idx 会自动排除）。"""
    out, end_ms = [], None
    for _ in range(2):
        p = dict(symbol=core.SYM[asset], interval="1h", limit=1000)
        if end_ms:
            p["endTime"] = end_ms
        b = get(BINANCE + "/api/v3/klines", p)
        out = [[x[0], float(x[2]), float(x[3]), float(x[4])] for x in b] + out
        end_ms = b[0][0] - 1
    out.sort(key=lambda x: x[0])
    return [[x[0] / 1000.0, x[1], x[2], x[3]] for x in out]


def live_universe():
    evs = []
    for a in core.ASSETS:
        for ser, kind in core.series_for(a):
            for i in range(5):
                b = get(pm.GAMMA + "/events", dict(series_slug=ser, closed="false", limit=100, offset=i * 100)) or []
                evs += [(a, kind, e) for e in b]
                if len(b) < 100:
                    break
    return evs


def book(tok):
    b = get("https://clob.polymarket.com/book", dict(token_id=tok))
    asks = sorted(((float(x["price"]), float(x["size"])) for x in b.get("asks", [])), key=lambda x: x[0])
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), key=lambda x: -x[0])
    return asks, bids


def market_by_token(tok):
    for closed in ("true", "false"):
        r = get(pm.GAMMA + "/markets", dict(clob_token_ids=tok, closed=closed))
        if r:
            return r[0]
    return None


# ---------- 核心步骤 ----------
def equity(st):
    return st["cash"] + sum(p["cost"] for p in st["positions"].values())


def exposure(st, asset=None):
    return sum(p["cost"] for p in st["positions"].values() if asset is None or p["asset"] == asset)


def settle(st):
    for tok, p in list(st["positions"].items()):
        try:
            m = market_by_token(tok)
        except Exception:
            continue
        res = pm.resolution(m) if m else None
        if res is None:
            continue
        toks = pm.token_ids(m)
        pay = res[toks.index(tok)] * p["shares"]
        st["cash"] += pay
        pnl = pay - p["cost"]
        log(f"结算 {p['q'][:60]} | {p['side']} | 成本 ${p['cost']:.2f} → 回款 ${pay:.2f} | 盈亏 {pnl:+.2f}")
        tg(f"【A08纸面】结算 {p['side']} {p['q']}\n成本 ${p['cost']:.2f} → 回款 ${pay:.2f}（盈亏 {pnl:+.2f}）")
        append_csv(TRD_CSV, ["time", "type", "asset", "side", "question", "token", "price", "shares", "amount", "pnl", "note"],
                   [int(time.time()), "SETTLE", p["asset"], p["side"], p["q"], tok, res[toks.index(tok)], p["shares"], round(pay, 2), round(pnl, 2), ""])
        del st["positions"][tok]


def backfill_v3(st):
    """信号 ≥40 分钟后，补记裁判 v3 口径成交价（信号后30分钟真实成交中位价）。"""
    keep = []
    for x in st["pending_v3"]:
        if time.time() < x["t"] + 2400:
            keep.append(x); continue
        try:
            m = market_by_token(x["tok"]); toks = pm.token_ids(m)
            rp = verify.real_price(m["conditionId"], x["tok"], toks, x["t"], x["t"] + 1800)
        except Exception:
            keep.append(x); continue
        v3 = None if rp is None else round(min(0.999, rp + verify.slip(rp)), 4)
        append_csv(os.path.join(HERE, "v3_check.csv"), ["signal_t", "token", "question", "paper_fill", "v3_fill", "diff"],
                   [x["t"], x["tok"], x["q"], x["fill"], v3, "" if v3 is None or x["fill"] is None else round(x["fill"] - v3, 4)])
    st["pending_v3"] = keep


def scan(st, t_now):
    msgs = []
    core.klines = lambda asset, start=None, end=None: [[int(x[0]), x[1], x[2], x[3]] for x in live_klines(asset)]
    SP = {a: core.Spot(a) for a in core.ASSETS}
    U = live_universe()
    w0 = max(st.get("last_t", t_now - 3600), t_now - 6 * 3600)  # 上次已扫描的整点之后
    if w0 < t_now - 3600:
        log(f"补扫 {(t_now - w0) // 3600 - 1} 个漏掉的整点")
    cands = []
    for a, kind, e in U:
        for m in e.get("markets", []):
            if m.get("closed"):
                continue
            cands += core.candidates(a, kind, e, m, SP, w0, t_now + 1, Z, GATE)
    cands.sort(key=lambda r: r["yes"])
    log(f"扫描：{len(U)} 个开放事件，本小时候选信号 {len(cands)} 个")
    eq = equity(st)
    st["peak"] = max(st["peak"], eq)
    if eq < (1 - DD_HALT) * st["peak"] and not st["halted"]:
        st["halted"] = True
        log(f"⚠️ 回撤 {1 - eq / st['peak']:.1%} 超过 {DD_HALT:.0%}，暂停开新仓，需人工复核（删除 state.json 中 halted 或改为 false 恢复）")
        tg(f"【A08纸面】⚠️ 回撤 {1 - eq / st['peak']:.1%}，已暂停开新仓，请人工复核")
    for r in cands:
        y = r["z"] > 0
        tok = r["yes"] if y else r["no"]
        side = "YES" if y else "NO"
        kw = ("reach" if r["dir"] == 1 else "dip") if r["kind"] == "barrier" else ("above" if r["dir"] == 1 else "below")
        q = f"{r['asset']} {kw} {r['K']:g}"
        reason, fill, spent, shares, best = "", None, 0.0, 0.0, None
        recent = [x for x in st["ev_entries"].get(r["ev"], []) if t_now - x < 86400]
        if r["yes"] in st["seen"]:
            continue  # 每个市场只做一次（与回测一致），不重复记录
        if len(recent) >= MAX_EV:
            reason = "同事件24h已达3笔"
        elif st["halted"]:
            reason = "回撤熔断中"
        if not reason:
            target = SIZE * eq
            room = min(ASSET_CAP * eq - exposure(st, r["asset"]), TOTAL_CAP * eq - exposure(st), st["cash"])
            try:
                vol24 = verify.window_volume(r["cid"], t_now - 86400, t_now)
            except Exception:
                vol24 = 0.0
            cap = min(target, max(0.0, room), VOL_CAP * vol24)
            try:
                asks, _ = book(tok)
            except Exception:
                asks = None
            if asks is None:
                reason = "盘口读取失败"
            elif not asks:
                reason = "盘口无卖单"
            else:
                best = asks[0][0]
                lim = min(0.99, best + MAX_SLIP)
                for px, sz in asks:
                    if px > lim or spent >= cap - 1e-9:
                        break
                    take = min(sz * px, cap - spent)
                    spent += take; shares += take / px
                fee = 0.02 * spent if r.get("fees") else 0.0
                if spent < MIN_ORDER:
                    reason = f"可成交金额不足(cap=${cap:.0f}, 24h量=${vol24:.0f})"
                    spent = shares = 0.0
                else:
                    fill = spent / shares
                    st["cash"] -= spent + fee
                    st["positions"][tok] = dict(asset=r["asset"], side=side, q=q, shares=shares, cost=spent + fee,
                                                t=t_now, fair=round(r["fair"], 4), end=r["end"])
                    reason = "成交"
                    append_csv(TRD_CSV, ["time", "type", "asset", "side", "question", "token", "price", "shares", "amount", "pnl", "note"],
                               [t_now, "BUY", r["asset"], side, q, tok, round(fill, 4), round(shares, 2), round(spent + fee, 2), "",
                                f"best_ask={best} fair={r['fair']:.3f} z72={r['z']:.2f} z720={r['z720']:.2f} vol24=${vol24:.0f}"])
                    log(f"纸面买入 {side} {q} | 均价 {fill:.3f}（最优卖 {best}）| ${spent:.2f} | 公允 {r['fair']:.3f}")
        st["seen"].append(r["yes"])
        st["ev_entries"][r["ev"]] = recent + [t_now]
        st["pending_v3"].append(dict(t=t_now, tok=tok, q=q, fill=None if fill is None else round(fill, 4)))
        msgs.append(f"{side} {q} | z72={r['z']:+.2f} 公允={r['fair']:.3f} 最优卖={best} → {reason}"
                    + (f"（均价 {fill:.3f}，${spent:.2f}）" if fill else ""))
        append_csv(SIG_CSV, ["time", "asset", "kind", "side", "question", "token", "z72", "z720", "fair", "best_ask", "result"],
                   [t_now, r["asset"], r["kind"], side, q, tok, round(r["z"], 3), round(r["z720"], 3), round(r["fair"], 4), best, reason])
    notify_signals(msgs, st)


def notify_signals(msgs, st):
    if not msgs:
        return
    head = f"【A08纸面】{time.strftime('%m-%d %H:%M', time.gmtime())} UTC 出现 {len(msgs)} 个信号"
    body = "\n".join(msgs[:25]) + (f"\n…另有 {len(msgs) - 25} 个" if len(msgs) > 25 else "")
    tg(f"{head}\n{body}\n权益 ${equity(st):,.2f}，持仓 {len(st['positions'])} 个（纸面，不下真单）")


def run_once():
    st = load_state()
    t_now = (int(time.time()) // 3600) * 3600 + 60
    try:
        settle(st)
        backfill_v3(st)
        scan(st, t_now)
        st["last_t"] = t_now
    except Exception:
        log("运行出错：" + traceback.format_exc().splitlines()[-1])
    eq = equity(st)
    append_csv(EQ_CSV, ["time", "equity", "cash", "open_positions", "exposure"],
               [t_now, round(eq, 2), round(st["cash"], 2), len(st["positions"]), round(exposure(st), 2)])
    save_state(st)
    log(f"权益 ${eq:,.2f}（现金 ${st['cash']:,.2f}，持仓 {len(st['positions'])} 个，敞口 ${exposure(st):,.2f}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="常驻：每小时 HH:01:30 UTC 运行")
    ap.add_argument("--once", action="store_true", help="单次运行（GitHub Actions 每小时调用）")
    a = ap.parse_args()
    if a.once:
        run_once(); return
    log("纸面交易启动（不下任何真实订单）")
    ok = tg("【A08纸面】纸面交易已启动，出现信号时会通知你。")
    log("Telegram 通知：" + {True: "已配置，启动消息已发送", False: "已配置但发送失败（见上一行原因）", None: "未配置（填写 telegram.env 后重启即可）"}[ok])
    run_once()
    while a.loop:
        now = time.time()
        nxt = (int(now) // 3600 + 1) * 3600 + 90
        time.sleep(max(5, nxt - now))
        run_once()


if __name__ == "__main__":
    main()
