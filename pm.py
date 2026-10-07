"""Polymarket 公共数据访问层（带磁盘缓存，所有智能体共享）。

用法:
    import sys; sys.path.insert(0, "<sandbox>/lib")
    import pm
    mkts = pm.markets(closed=True, end_date_min="2025-01-01", limit=500)   # gamma markets 列表
    hist = pm.price_history(token_id, fidelity=60)                          # [(ts, price), ...]
    trs  = pm.trades(condition_id, limit=500)                               # data-api 成交
    out  = pm.resolution(market_dict)                                       # [1.0, 0.0] 等结算价

API:
    gamma:  https://gamma-api.polymarket.com  (/markets, /events)
    clob:   https://clob.polymarket.com/prices-history?market=<token>&interval=max&fidelity=<min>
    data:   https://data-api.polymarket.com/trades?market=<conditionId>
"""
import hashlib, json, os, time, warnings
warnings.filterwarnings("ignore")
import requests

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
os.makedirs(CACHE, exist_ok=True)
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
_S = requests.Session()


def _cached_get(url, params=None, ttl=None, sub="http"):
    key = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()
    d = os.path.join(CACHE, sub, key[:2])
    os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, key + ".json")
    if os.path.exists(fp) and (ttl is None or time.time() - os.path.getmtime(fp) < ttl):
        with open(fp) as f:
            return json.load(f)
    for attempt in range(5):
        try:
            r = _S.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1)); continue
            r.raise_for_status()
            data = r.json()
            break
        except Exception:
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))
    if _disk_ok():
        tmp = fp + ".%d.tmp" % os.getpid()
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, fp)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return data


MIN_FREE_BYTES = 1 * 1024 ** 3  # 云端：剩余 <1GB 停写缓存
_disk_chk = [0.0, True]
def _disk_ok():
    import shutil
    now = time.time()
    if now - _disk_chk[0] > 30:
        _disk_chk[0] = now
        _disk_chk[1] = shutil.disk_usage(CACHE).free > MIN_FREE_BYTES
    return _disk_chk[1]


def markets(limit=500, offset=0, ttl=86400, **filters):
    """gamma /markets，一页。常用 filters: closed=True, end_date_min, end_date_max, order='volumeNum', ascending=False, tag_id"""
    p = {"limit": limit, "offset": offset}
    for k, v in filters.items():
        p[k] = str(v).lower() if isinstance(v, bool) else v
    return _cached_get(GAMMA + "/markets", p, ttl=ttl, sub="gamma")


def all_markets(max_pages=40, page=500, **filters):
    out = []
    for i in range(max_pages):
        batch = markets(limit=page, offset=i * page, **filters)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page:
            break
    return out


def events(limit=500, offset=0, ttl=86400, **filters):
    p = {"limit": limit, "offset": offset}
    for k, v in filters.items():
        p[k] = str(v).lower() if isinstance(v, bool) else v
    return _cached_get(GAMMA + "/events", p, ttl=ttl, sub="gamma")


def token_ids(m):
    t = m.get("clobTokenIds")
    return json.loads(t) if isinstance(t, str) else (t or [])


def outcomes(m):
    t = m.get("outcomes")
    return json.loads(t) if isinstance(t, str) else (t or [])


def resolution(m):
    """已结算市场的结算价列表（与 token_ids 顺序一致），未结算返回 None。"""
    if not m.get("closed"):
        return None
    p = m.get("outcomePrices")
    p = json.loads(p) if isinstance(p, str) else p
    if not p:
        return None
    p = [float(x) for x in p]
    if max(p) < 0.99:  # 未干净结算
        return None
    return p


def price_history(token_id, fidelity=60, ttl=None):
    """返回 [(ts, price)]，按时间升序。fidelity 为分钟粒度（60=小时，1440=日）。已结算市场永久缓存。"""
    d = _cached_get(CLOB + "/prices-history",
                    {"market": token_id, "interval": "max", "fidelity": fidelity}, ttl=ttl, sub="hist")
    h = [(int(x["t"]), float(x["p"])) for x in d.get("history", [])]
    if h or fidelity >= 720:
        return h
    # 已结算市场: interval=max 只支持 fidelity>=720；用 <=14 天的 startTs/endTs 分块拼接细粒度数据
    coarse = price_history(token_id, fidelity=720, ttl=ttl)
    if not coarse:
        return []
    t0, t1 = coarse[0][0] - 43200, coarse[-1][0] + 43200
    out, CH = {}, 14 * 86400
    s = t0
    while s < t1:
        e = min(s + CH, t1)
        d = _cached_get(CLOB + "/prices-history", {"market": token_id, "startTs": s, "endTs": e, "fidelity": fidelity},
                        ttl=ttl, sub="hist")
        for x in d.get("history", []):
            out[int(x["t"])] = float(x["p"])
        s = e
    return sorted(out.items())


def trades(condition_id, limit=500, offset=0, ttl=None, **kw):
    p = {"market": condition_id, "limit": limit, "offset": offset}
    p.update(kw)
    return _cached_get(DATA + "/trades", p, ttl=ttl, sub="trades")


def price_at(hist, ts):
    """ts 时刻（含）之前最近一个价格；没有则 None。严格无前视。"""
    lo, hi, ans = 0, len(hist) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if hist[mid][0] <= ts:
            ans = hist[mid][1]; lo = mid + 1
        else:
            hi = mid - 1
    return ans
