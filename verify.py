"""独立裁判：用真实 Polymarket 历史价格重放智能体提交的交易日志，计算可信回报。

智能体只提交“意图”(何时、买哪个 token、仓位比例、何时卖)，成交价由裁判按真实历史价格 + 滑点决定，
结算由 gamma 官方结算价决定。智能体自报的收益一律不采信。

trades.csv 列:
  token_id      (必填) CLOB token id，买入该 outcome
  entry_ts      (必填) 下单的 unix 秒；裁判用 <= entry_ts 的最近历史价成交（无前视）
  size_frac     (必填) 下单时占当前总权益的比例 (0,1]
  exit_ts       (可选) 卖出 unix 秒；空 = 持有至结算
  note          (可选)

规则 (v1):
  - 初始资金 $10,000；评分窗口 WINDOW (默认 2025-10-01 → 2026-10-01)，只计 entry_ts 在窗口内的交易
  - 成交价 = 历史价 + 滑点；滑点 = 0.01 (0.05<=p<=0.95) / 0.003 (极端价)，买价上限 0.999；卖出对称减滑点
  - 历史价距离 entry_ts 超过 STALE 秒(默认 6h) 视为无可成交价 → 交易作废
  - 容量：单笔名义金额 <= 该市场总成交量的 1%，且 <= $250,000；超出部分不成交
  - 费用：feesEnabled=True 的市场按名义金额收 2% taker 费（保守）
  - 现金不足时按剩余现金缩量；未结算且无 exit 的持仓按最后历史价减滑点计值
输出 JSON: multiple(期末/期初), n_trades_valid, rejected 原因统计, 最大回撤, 最大单笔贡献占比 等
"""
import argparse, calendar, csv, json, math, os, sys, time
from collections import Counter
import pm

START_CASH = 10_000.0
DEF_WIN = ("2025-10-01", "2026-10-01")
STALE = 6 * 3600
CAP_VOL_FRAC = 0.01
CAP_ABS = 250_000.0
FEE = 0.02
POST_WIN = 1800  # v3: 信号后 30 分钟内真实成交
MIN_PRE_NOTIONAL = 0.0  # 容量上限已控制规模；只要求存在真实成交


def ts(d):
    return int(calendar.timegm(time.strptime(d, "%Y-%m-%d")))


def slip(p):
    return 0.01 if 0.05 <= p <= 0.95 else 0.003


_mcache = {}
def market_for_token(tok):
    if tok in _mcache:
        return _mcache[tok]
    m = None
    for closed in ("true", "false"):
        r = pm._cached_get(pm.GAMMA + "/markets", {"clob_token_ids": tok, "closed": closed}, ttl=None if closed == "true" else 3600, sub="gamma_tok")
        if r:
            m = r[0]; break
    _mcache[tok] = m
    return m


def last_point(hist, t):
    lo, hi, ans = 0, len(hist) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if hist[mid][0] <= t:
            ans = hist[mid]; lo = mid + 1
        else:
            hi = mid - 1
    return ans


def window_volume(cid, t0, t1):
    """data-api 在 [t0,t1] 内的成交额(USDC)，最多翻 6 页(3000 笔)，截断则为下界(保守)。"""
    tot = 0.0
    for i in range(6):
        j = pm._cached_get(pm.DATA + "/trades", {"market": cid, "start": t0, "end": t1, "limit": 500, "offset": i * 500}, ttl=None, sub="trades_win")
        if not isinstance(j, list) or not j:
            break
        tot += sum(float(x["size"]) * float(x["price"]) for x in j)
        if len(j) < 500:
            break
    return tot


def real_price(cid, tok, toks, t0, t1, with_notional=False):
    """[t0,t1] 内该 token 的真实成交价(成交量加权中位数)；互补 token 的成交按 1-p 折算。无成交返回 None。"""
    pts = []
    for i in range(4):
        j = pm._cached_get(pm.DATA + "/trades", {"market": cid, "start": t0, "end": t1, "limit": 500, "offset": i * 500}, ttl=None, sub="trades_win")
        if not isinstance(j, list) or not j:
            break
        for x in j:
            pr, sz = float(x["price"]), float(x["size"])
            if x.get("asset") == tok:
                pts.append((pr, sz))
            elif x.get("asset") in toks:
                pts.append((1 - pr, sz))
        if len(j) < 500:
            break
    if not pts:
        return (None, 0.0) if with_notional else None
    notional = sum(pr * z for pr, z in pts)
    pts.sort(); half = sum(z for _, z in pts) / 2; acc = 0
    for pr, z in pts:
        acc += z
        if acc >= half:
            return (pr, notional) if with_notional else pr


def run(path, win=DEF_WIN, fidelity=60, cap_mode="final", slip_extra=0.0, delay=0, cap_frac_win=0.10, version=1):
    """version=2: 入场/出场需要 [t-1h, t+2h] 内真实成交确认，成交价取 max(历史价+滑点, 真实成交中位价)；容量=±24h 真实成交额的 10%。"""
    if version >= 2:
        cap_mode = "window"  # v2/v2.1/v3 共用真实成交额容量
    """审计参数: cap_mode='window' 用入场前后24h真实成交额*cap_frac_win 做容量; slip_extra 额外滑点; delay 入场/出场延迟秒。"""
    w0, w1 = ts(win[0]), ts(win[1])
    rows = list(csv.DictReader(open(path)))
    rej = Counter()
    events = []  # (time, kind, idx)
    trades = []
    for r in rows:
        try:
            tok = r["token_id"].strip(); et = int(float(r["entry_ts"])) + delay; sf = float(r["size_frac"])
            xt = r.get("exit_ts", "").strip(); xt = int(float(xt)) + delay if xt else None
        except Exception:
            rej["bad_row"] += 1; continue
        if not (w0 <= et < w1):
            rej["outside_window"] += 1; continue
        if not (0 < sf <= 1):
            rej["bad_size"] += 1; continue
        if xt is not None and xt <= et:
            rej["exit_before_entry"] += 1; continue
        m = market_for_token(tok)
        if not m:
            rej["unknown_token"] += 1; continue
        toks = pm.token_ids(m)
        if tok not in toks:
            rej["token_mismatch"] += 1; continue
        hist = pm.price_history(tok, fidelity=fidelity)
        pt = last_point(hist, et)
        if not pt or et - pt[0] > STALE:
            rej["no_price_at_entry"] += 1; continue
        p = pt[1]
        if p <= 0.0 or p >= 0.999:
            rej["unfillable_price"] += 1; continue
        fill = min(0.999, p + slip(p) + slip_extra)
        if version >= 4:
            rp = real_price(m["conditionId"], tok, toks, et, et + POST_WIN)
            if rp is None:
                rej["no_real_trade_after_signal"] += 1; continue
            fill = min(0.999, rp + slip(rp) + slip_extra)
        elif version >= 3:
            rp, nt = real_price(m["conditionId"], tok, toks, et - 3600, et, with_notional=True)
            if rp is None or nt < MIN_PRE_NOTIONAL:
                rej["no_real_trade_before_entry"] += 1; continue
            if real_price(m["conditionId"], tok, toks, et, et + 7200) is None:
                rej["no_liquidity_after_entry"] += 1; continue
            fill = min(0.999, rp + slip(rp) + slip_extra)
        elif version >= 2:
            rp = real_price(m["conditionId"], tok, toks, et - 3600, et + 7200)
            if rp is None:
                rej["no_real_trade_at_entry"] += 1; continue
            fill = min(0.999, max(fill, rp + slip_extra))
        res = pm.resolution(m)
        payout, exit_t = None, None
        if xt is not None and (res is None or xt < ts_end(m)):
            xp = last_point(hist, xt)
            if not xp or xt - xp[0] > STALE:
                rej["no_price_at_exit"] += 1; continue
            payout = max(0.0, xp[1] - slip(xp[1]) - slip_extra); exit_t = xt
            if version >= 4:
                rp = real_price(m["conditionId"], tok, toks, xt, xt + POST_WIN)
                if rp is None:
                    rej["no_real_trade_at_exit"] += 1; continue
                payout = max(0.0, rp - slip(rp) - slip_extra)
            elif version >= 3:
                rp, nt = real_price(m["conditionId"], tok, toks, xt - 3600, xt, with_notional=True)
                if rp is None or nt < MIN_PRE_NOTIONAL or real_price(m["conditionId"], tok, toks, xt, xt + 7200) is None:
                    rej["no_real_trade_at_exit"] += 1; continue
                payout = max(0.0, rp - slip(rp) - slip_extra)
            elif version >= 2:
                rp = real_price(m["conditionId"], tok, toks, xt - 3600, xt + 7200)
                if rp is None:
                    rej["no_real_trade_at_exit"] += 1; continue
                payout = max(0.0, min(payout, rp - slip_extra))
        elif res is not None:
            payout = res[toks.index(tok)]; exit_t = max(et + 1, ts_end(m))
        else:
            lp = hist[-1][1] if hist else 0.0
            payout = max(0.0, lp - slip(lp)); exit_t = w1
        vol = float(m.get("volumeNum") or m.get("volume") or 0)
        cap = min(CAP_ABS, CAP_VOL_FRAC * vol)
        if cap_mode == "window":
            cap = min(CAP_ABS, cap_frac_win * window_volume(m["conditionId"], et - 86400, et + 86400))
        trades.append(dict(tok=tok, q=m.get("question", "")[:80], et=et, xt=exit_t, sf=sf, fill=fill,
                           payout=payout, cap=cap, fee=FEE if m.get("feesEnabled") else 0.0, note=r.get("note", "")))
    for i, t in enumerate(trades):
        events.append((t["et"], 1, i)); events.append((t["xt"], 0, i))
    events.sort()
    cash, open_cost = START_CASH, {}
    pnl = [0.0] * len(trades)
    curve = [(w0, START_CASH)]
    capped = 0; peak = START_CASH; mdd = 0.0
    for t_, kind, i in events:
        tr = trades[i]
        if kind == 1:
            equity = cash + sum(open_cost.values())
            want = tr["sf"] * equity
            notional = min(want, tr["cap"], cash / (1 + tr["fee"]))
            if notional < want - 1e-6:
                capped += 1
            if notional < 1.0:
                tr["skip"] = True; continue
            shares = notional / tr["fill"]
            cash -= notional * (1 + tr["fee"])
            tr["shares"], tr["cost"] = shares, notional * (1 + tr["fee"])
            open_cost[i] = tr["cost"]
        else:
            if tr.get("skip") or "shares" not in tr:
                continue
            proceeds = tr["shares"] * tr["payout"]
            cash += proceeds
            pnl[i] = proceeds - tr["cost"]
            open_cost.pop(i, None)
            eq = cash + sum(open_cost.values())
            curve.append((t_, eq)); peak = max(peak, eq); mdd = max(mdd, 1 - eq / peak)
    final = cash + sum(open_cost.values())
    done = [i for i, t in enumerate(trades) if "shares" in t]
    wins = sum(1 for i in done if pnl[i] > 0)
    tot_gain = sum(p for p in pnl if p > 0)
    top = max(pnl) if pnl else 0
    by_mkt = Counter()
    for i in done:
        by_mkt[trades[i]["q"]] += pnl[i]
    return dict(
        file=path, window=list(win), start=START_CASH, final=round(final, 2), multiple=round(final / START_CASH, 4),
        n_rows=len(rows), n_valid=len(trades), n_executed=len(done), n_capped=capped, win_rate=round(wins / max(1, len(done)), 3),
        max_drawdown=round(mdd, 4), top_trade_share_of_gains=round(top / tot_gain, 3) if tot_gain > 0 else None,
        n_markets=len(by_mkt), top_markets=[(q, round(v, 2)) for q, v in by_mkt.most_common(5)],
        rejected=dict(rej))


def ts_end(m):
    for k in ("closedTime", "umaEndDate", "endDate"):
        v = m.get(k)
        if v:
            v = v.replace("Z", "").replace("+00", "").split(".")[0].replace("T", " ")
            try:
                return int(calendar.timegm(time.strptime(v[:19], "%Y-%m-%d %H:%M:%S")))
            except Exception:
                try:
                    return int(calendar.timegm(time.strptime(v[:10], "%Y-%m-%d")))
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("trades_csv")
    ap.add_argument("--start", default=DEF_WIN[0]); ap.add_argument("--end", default=DEF_WIN[1])
    ap.add_argument("--fidelity", type=int, default=60)
    ap.add_argument("--cap-mode", default="final"); ap.add_argument("--slip-extra", type=float, default=0.0); ap.add_argument("--delay", type=int, default=0)
    ap.add_argument("--version", type=int, default=4, help="裁判版本：2=v2(第3-4轮)；3=v2.1(废弃,含信号前价格泄漏)；4=v3(第5轮起默认)")
    a = ap.parse_args()
    print(json.dumps(run(a.trades_csv, (a.start, a.end), a.fidelity, a.cap_mode, a.slip_extra, a.delay, version=a.version), ensure_ascii=False, indent=2))
