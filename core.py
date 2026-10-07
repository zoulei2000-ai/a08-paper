"""A08 round 5 core: rule-based universe + spot-only momentum candidates (no PM prices used for selection)."""
import sys, os, re, math, time, calendar, json
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
import pm
from scipy.stats import norm
import requests

ASSETS = ("bitcoin", "ethereum", "solana", "xrp")
SHORT = dict(bitcoin="btc", ethereum="eth", solana="sol", xrp="xrp")
SYM = dict(bitcoin="BTCUSDT", ethereum="ETHUSDT", solana="SOLUSDT", xrp="XRPUSDT")

def series_for(a):
    s = SHORT[a]
    barrier = [f"{a}-hit-price-weekly", f"{a}-hit-price-monthly", f"{a}-hit-price-daily", f"{s}-monthly-prices"]
    terminal = [("btc" if a == "bitcoin" else a) + "-multi-strikes-weekly"]   # daily "<asset> above K on <date>?"
    return [(x, "barrier") for x in barrier] + [(x, "terminal") for x in terminal]

def safe(fn, *a, **kw):
    for i in range(12):
        try:
            return fn(*a, **kw)
        except Exception:
            time.sleep(min(30, 3 * (i + 1)))
    return None

def ts(d):
    return int(calendar.timegm(time.strptime(d, "%Y-%m-%d")))

def iso(s):
    return calendar.timegm(time.strptime(s.replace("Z", "")[:19], "%Y-%m-%dT%H:%M:%S"))

def universe(start, end):
    """All events of the listed series whose endDate is in [start, end+40d]. No outcome/volume info used."""
    lo = ts(start); hi = ts(end) + 40 * 86400
    evs = {}
    for a in ASSETS:
        for ser, kind in series_for(a):
            for closed in (True, False):
                for i in range(50):
                    b = safe(pm.events, limit=100, offset=i * 100, series_slug=ser, closed=closed, ttl=None if closed else 3600) or []
                    for e in b:
                        ed = e.get("endDate")
                        if ed and lo <= ts(ed[:10]) <= hi:
                            evs[e["id"]] = (a, kind, e)
                    if len(b) < 100:
                        break
    return list(evs.values())

def parse(m):
    q = m.get("question", ""); g = m.get("groupItemTitle") or ""
    mm = re.search(r"\$\s*([\d,]+(?:\.\d+)?)\s*([kK])?", q)
    if not mm:
        return 0, None
    K = float(mm.group(1).replace(",", "")) * (1000 if mm.group(2) else 1)
    ql = q.lower()
    if "↑" in g or re.search(r"\b(reach|above|hit)\b", ql) and not re.search(r"\b(dip|below|fall|drop)\b", ql):
        d = 1
    elif "↓" in g or re.search(r"\b(dip|below|fall|drop)\b", ql):
        d = -1
    else:
        d = 0
    return d, K

KD = os.path.join(HERE, "spot_cache")
def klines(asset, start="2024-06-01", end="2026-10-02"):
    """Binance public 1h klines: [open_ts, high, low, close]; a bar is usable at open_ts+3600."""
    os.makedirs(KD, exist_ok=True)
    fp = os.path.join(KD, f"{asset}_{start}_{end}.json")
    r4 = os.path.join(HERE, "..", "round_4", "spot_cache", f"{asset}_{start}_{end}.json")
    for f in (fp, r4):
        if os.path.exists(f):
            return json.load(open(f))
    s = ts(start) * 1000; e = ts(end) * 1000; out = []
    while s < e:
        b = None
        for i in range(8):
            try:
                b = requests.get("https://api.binance.com/api/v3/klines", params=dict(symbol=SYM[asset], interval="1h", startTime=s, limit=1000), timeout=20).json(); break
            except Exception:
                time.sleep(2 * (i + 1))
        if not b: break
        out += [[int(k[0]) // 1000, float(k[2]), float(k[3]), float(k[4])] for k in b]
        s = b[-1][0] + 3600_000
    json.dump(out, open(fp, "w"))
    return out

class Spot:
    def __init__(self, asset, L=72):
        k = np.array(klines(asset)); self.ot, self.hi, self.lo, self.cl = k[:, 0], k[:, 1], k[:, 2], k[:, 3]
        lr = np.diff(np.log(self.cl), prepend=np.nan)
        n = len(self.cl); self.z = np.full(n, np.nan); self.z720 = np.full(n, np.nan); self.sig = np.full(n, np.nan)
        for j in range(800, n):
            w = lr[j - 719:j + 1]; s7 = lr[j - 167:j + 1].std()
            self.sig[j] = s7
            self.z[j] = math.log(self.cl[j] / self.cl[j - L]) / (s7 * math.sqrt(L))
            self.z720[j] = math.log(self.cl[j] / self.cl[j - 720]) / (w.std() * math.sqrt(720))
    def idx(self, t):
        """last bar fully closed by t"""
        return int(np.searchsorted(self.ot, t - 3600, side="right") - 1)

def fair_yes(kind, d, K, S, sig, Th):
    x = math.log(K / S) / (sig * math.sqrt(max(Th, 1e-6)))
    if kind == "terminal":
        return float(1 - norm.cdf(x)) if d == 1 else float(norm.cdf(x))
    return float(min(1.0, 2 * (1 - norm.cdf(abs(x)))))

def candidates(a, kind, e, m, SP, w0, w1, Z, GATE, BAND=(0.10, 0.90), TMIN=6 * 3600, TMAX=40 * 86400):
    """Hourly scan (t = top of hour + 60s). Uses only Binance bars closed by t + market metadata."""
    toks = pm.token_ids(m)
    if len(toks) < 2 or [o.lower() for o in pm.outcomes(m)][:2] != ["yes", "no"]:
        return []
    d, K = parse(m)
    if not d or not K:
        return []
    try:
        end_ = iso(m.get("endDate") or e["endDate"]); st = iso(m.get("startDate") or e.get("startDate") or e["endDate"])
    except Exception:
        return []
    sp = SP[a]; out = []
    t0 = max(w0, st, end_ - TMAX); t0 = (t0 // 3600 + 1) * 3600 + 60
    i0 = int(np.searchsorted(sp.ot, st))
    for t in range(t0, min(w1, end_ - TMIN), 3600):
        j = sp.idx(t)
        if j < 800 or not np.isfinite(sp.z[j]):
            continue
        z = d * sp.z[j]
        if abs(z) < Z:
            continue
        g = np.sign(z) * d * sp.z720[j]
        if g <= GATE:
            continue
        if kind == "barrier" and j >= i0 and ((d == 1 and sp.hi[i0:j + 1].max() >= K) or (d == -1 and sp.lo[i0:j + 1].min() <= K)):
            continue
        fy = fair_yes(kind, d, K, sp.cl[j], sp.sig[j], (end_ - t) / 3600)
        fs = fy if z > 0 else 1 - fy
        if not (BAND[0] <= fs <= BAND[1]):
            continue
        out.append(dict(ev=e["id"], asset=a, kind=kind, dir=d, K=K, yes=toks[0], no=toks[1], cid=m["conditionId"],
                        t=t, z=z, z720=sp.z720[j], fair=fs, end=end_, fees=bool(m.get("feesEnabled"))))
    return out
