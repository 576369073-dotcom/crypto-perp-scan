#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gate USDT 永续 · 多周期 MACD / EMA 共振筛选器
=================================================
严格按照需求文档《新建 DOCX 文档.docx》实现:
  指标一 MACD(12,26,9): 大周期 1D+4H 顺 / 中周期 1H 逆 / 小周期 15M 顺, 且处于底部结构
  指标二 EMA(7,25,99): 4H 不破 EMA25(或回踩收回) / 1H 不破 EMA25(或回踩收回) / 15M 站稳 EMA25
数据源: Gate.io USDT 永续合约公共 API
输出:   控制台摘要 + report/latest.html + report/latest.json + 企业微信群机器人推送

用法:
  python scanner.py                # 全市场扫描 + 推送(若配置了 webhook)
  python scanner.py --limit 40     # 只扫前 40 个合约(调试)
  python scanner.py --dry-run      # 不推送, 只生成本地报告
  python scanner.py --loop 900     # 常驻进程, 每 900 秒(15分钟)扫一次
"""

import argparse
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape

TZ = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(HERE, "report")

INTERVAL_SEC = {
    "10s": 10, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400,
}
TF_LABEL = {"1d": "1D", "4h": "4H", "1h": "1H", "15m": "15M"}


# ----------------------------------------------------------------------------- utils
def now_bj():
    return datetime.now(TZ)


def fmt_ts(ts, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ts, TZ).strftime(fmt)


def human_vol(v):
    """成交额(USDT) 人类可读"""
    if v is None:
        return "-"
    if v >= 1e8:
        return "%.2f亿" % (v / 1e8)
    if v >= 1e4:
        return "%.0f万" % (v / 1e4)
    return "%.0f" % v


def fmt_price(p):
    if p is None:
        return "-"
    p = float(p)
    if p >= 1000:
        return "{:,.1f}".format(p)
    if p >= 1:
        return "{:.4g}".format(p)
    return "{:.6g}".format(p)


class RateLimiter:
    """简易令牌桶: 控制全局请求速率, 避免触发交易所限频"""

    def __init__(self, per_sec):
        self.per_sec = max(1.0, float(per_sec))
        self.lock = threading.Lock()
        self.next_at = time.time()

    def wait(self):
        with self.lock:
            now = time.time()
            if now < self.next_at:
                delay = self.next_at - now
                self.next_at += 1.0 / self.per_sec
            else:
                delay = 0.0
                self.next_at = now + 1.0 / self.per_sec
        if delay > 0:
            time.sleep(delay)


def http_json(url, timeout=20, retries=3, limiter=None):
    last_err = None
    for attempt in range(retries):
        if limiter:
            limiter.wait()
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (crypto-scanner)", "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code in (429, 418, 500, 502, 503):
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = "%s: %s" % (type(e).__name__, e)
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("request failed after %d tries (%s): %s" % (retries, last_err, url))


# ----------------------------------------------------------------------------- indicators
def ema_valid(vals, n):
    """EMA, 前 n-1 个位置为 None, 第 n 个用 SMA 播种"""
    out = [None] * len(vals)
    if len(vals) < n or n <= 0:
        return out
    k = 2.0 / (n + 1.0)
    prev = sum(vals[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def macd_series(closes, fast=12, slow=26, signal=9):
    ef = ema_valid(closes, fast)
    es = ema_valid(closes, slow)
    dif = [None if (ef[i] is None or es[i] is None) else ef[i] - es[i] for i in range(len(closes))]
    idx = [i for i, v in enumerate(dif) if v is not None]
    dea = [None] * len(closes)
    if idx:
        dea_v = ema_valid([dif[i] for i in idx], signal)
        for j, i in enumerate(idx):
            dea[i] = dea_v[j]
    hist = [
        None if (dif[i] is None or dea[i] is None) else 2.0 * (dif[i] - dea[i])
        for i in range(len(closes))
    ]
    return dif, dea, hist


def macd_state(closes, fast=12, slow=26, signal=9):
    """返回最新一根已收盘K线的 MACD 结构状态"""
    if len(closes) < slow + signal + 10:
        return None
    dif, dea, hist = macd_series(closes, fast, slow, signal)
    vd, ve, vh = [], [], []
    for i in range(len(closes)):
        if dif[i] is not None and dea[i] is not None:
            vd.append(dif[i])
            ve.append(dea[i])
            vh.append(hist[i])
    if len(vd) < 12:
        return None

    dif_now, dea_now, hist_now = vd[-1], ve[-1], vh[-1]
    bull = dif_now > dea_now

    # 最近一次金/死叉距今多少根
    golden_bars = None
    for k in range(1, min(len(vd) - 1, 60)):
        i = len(vd) - k
        if vd[i] > ve[i] and vd[i - 1] <= ve[i - 1]:
            golden_bars = k - 1
            break

    shrink3 = (
        len(vh) >= 4 and vh[-1] < 0 and vh[-2] < 0 and vh[-3] < 0 and vh[-1] > vh[-2] > vh[-3]
    )
    expand3 = len(vh) >= 4 and vh[-1] > vh[-2] > vh[-3] > vh[-4]
    dif_turn_up = len(vd) >= 3 and vd[-1] > vd[-2] and vd[-2] <= vd[-3]

    return {
        "dif": dif_now,
        "dea": dea_now,
        "hist": hist_now,
        "bull": bull,
        "hist_rising": hist_now > vh[-2] if len(vh) >= 2 else False,
        "expand3": expand3,
        "shrink3": shrink3,
        "dif_turn_up": dif_turn_up,
        "golden_bars": golden_bars,
        "above_zero": dif_now > 0,
    }


def ema_state(closes, lows, n, retest_bars):
    """收盘价与 EMA 的关系; 支持"回踩后快速收回"判定"""
    e = ema_valid(closes, n)
    if not e or e[-1] is None:
        return None
    close, ev = closes[-1], e[-1]
    above = close > ev
    retest = False
    for k in range(1, min(retest_bars, len(closes) - 1) + 1):
        i = len(closes) - k
        if e[i] is not None and lows[i] < e[i] and closes[i] > e[i]:
            retest = True
            break
    return {
        "ema": ev,
        "above": above,
        "retest": retest,
        "ok": above or retest,
        "gap_pct": (close / ev - 1.0) * 100.0 if ev else 0.0,
    }


# ----------------------------------------------------------------------------- data
class GateClient:
    def __init__(self, cfg, use_cache=True):
        self.cfg = cfg
        ds = cfg["data_source"]
        self.base = ds["base_url"].rstrip("/")
        self.settle = ds.get("settle", "usdt")
        self.limit = ds.get("candle_limit", 400)
        rt = cfg["runtime"]
        self.limiter = RateLimiter(rt["rate_per_sec"])
        self.timeout = rt["timeout"]
        self.retries = rt["retries"]
        self.use_cache = use_cache
        self.cache_dir = os.path.join(HERE, "cache")
        if use_cache:
            os.makedirs(self.cache_dir, exist_ok=True)

    def _get(self, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return http_json(url, self.timeout, self.retries, self.limiter)

    def contracts(self):
        return self._get("/futures/%s/contracts" % self.settle)

    def tickers(self):
        return self._get("/futures/%s/tickers" % self.settle)

    def candles(self, contract, interval):
        iv = INTERVAL_SEC.get(interval, 900)
        cpath = os.path.join(self.cache_dir, "%s_%s.json" % (contract, interval)) if self.use_cache else None
        if cpath and os.path.exists(cpath):
            try:
                with open(cpath, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                # 缓存有效期 = 一个K线周期(最多1小时), 保证不重复拉取盘中数据
                if time.time() - cached.get("fetched_at", 0) < min(iv, 3600):
                    return cached["bars"]
            except Exception:  # noqa: BLE001
                pass

        raw = self._get(
            "/futures/%s/candlesticks" % self.settle,
            {"contract": contract, "interval": interval, "limit": self.limit},
        )
        now = time.time()
        bars = []
        for b in raw:
            t = int(b["t"])
            # 只保留已收盘K线: 开+周期 必须早于当前时间
            if t + iv > now + 1:
                continue
            try:
                bars.append(
                    {
                        "t": t,
                        "o": float(b["o"]),
                        "h": float(b["h"]),
                        "l": float(b["l"]),
                        "c": float(b["c"]),
                        "v": float(b.get("sum") or 0),  # 结算币成交额
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        bars.sort(key=lambda x: x["t"])
        if cpath:
            try:
                tmp = cpath + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"fetched_at": now, "bars": bars}, f)
                os.replace(tmp, cpath)
            except Exception:  # noqa: BLE001
                pass
        return bars


# ----------------------------------------------------------------------------- evaluate
def plan_position(entry, stop, cfg):
    """按账户风险预算反推仓位: 先定止损与可亏金额, 再决定名义规模与杠杆。
    杠杆是结果而不是目标 —— 止损越近, 同样的风险预算需要越大名义。"""
    acc = cfg.get("account") or {}
    equity = float(acc.get("equity_usdt", 40))
    risk_pct = float(acc.get("risk_pct", 25))
    max_lev = float(acc.get("max_leverage", 20))
    fee = float(acc.get("fee_rate", 0.0005))
    mmr = float(acc.get("mmr", 0.01))
    buf = float(acc.get("min_liq_buffer_pct", 1.0)) / 100.0
    slip = float(acc.get("slippage_pct", 0.08)) / 100.0

    stop_dist = max((entry - stop) / entry, 1e-6)
    risk_budget = equity * risk_pct / 100.0
    notional = risk_budget / stop_dist

    # 交易所杠杆固定为 max_lev; 保证金 = 名义/杠杆, 并限制占本金比例以留缓冲
    lev_set = max_lev
    max_margin = equity * float(acc.get("max_margin_pct", 80)) / 100.0
    capped = False
    if notional / lev_set > max_margin:
        notional = max_margin * lev_set
        capped = True
    risk_actual = notional * stop_dist
    margin = notional / lev_set
    lev_eff = notional / equity if equity else 0.0
    liq_dist = (1.0 - mmr) / lev_set
    liq_price = entry * (1 - liq_dist)
    # 让止损先于爆仓被触发的最高杠杆
    lev_max_safe = max(1.0, (1.0 - mmr) / (stop_dist + buf))
    fee_cost = notional * 2 * fee
    # 往返摩擦成本: 开平各一次手续费 + 各一次滑点, 折算到 R
    cost_pct = (2 * fee + 2 * slip) * 100.0
    cost_r_ratio = cost_pct / (stop_dist * 100.0) if stop_dist else 99.0
    return {
        "entry": entry,
        "stop": stop,
        "stop_dist_pct": stop_dist * 100,
        "equity": equity,
        "risk_pct": risk_pct,
        "risk_budget": risk_budget,
        "risk_actual": risk_actual,
        "notional": notional,
        "margin": margin,
        "leverage": lev_eff,
        "lev_set": lev_set,
        "max_leverage": max_lev,
        "capped": capped,
        "liq_price": liq_price,
        "liq_dist_pct": liq_dist * 100,
        "lev_max_safe": lev_max_safe,
        "fee_cost": fee_cost,
        "fee_pct_of_equity": fee_cost / equity * 100 if equity else 0,
        "cost_pct": cost_pct,
        "cost_r_ratio": cost_r_ratio,
        "cost_r_pct": cost_r_ratio * 100,
        "viable": (liq_dist - stop_dist) >= buf,
    }


def evaluate(symbol, series, cfg, meta, keep_all=False):
    """series: {tf: [bars...]}; 返回候选记录或 None"""
    ind = cfg["indicators"]
    macd_p = ind["macd"]
    tfs = cfg["timeframes"]
    ema_req = cfg["ema_requirements"]

    need = [tfs["large"][0], tfs["large"][1], tfs["mid"], tfs["small"]]
    bars = {}
    for tf in need:
        b = series.get(tf) or []
        if len(b) < macd_p["slow"] + macd_p["signal"] + 10:
            return None
        bars[tf] = b

    closes = {tf: [x["c"] for x in bars[tf]] for tf in need}
    lows = {tf: [x["l"] for x in bars[tf]] for tf in need}
    highs = {tf: [x["h"] for x in bars[tf]] for tf in need}

    st = {tf: macd_state(closes[tf], macd_p["fast"], macd_p["slow"], macd_p["signal"]) for tf in need}
    if any(v is None for v in st.values()):
        return None

    tf_1d, tf_4h, tf_1h, tf_15m = need
    s1d, s4h, s1h, s15 = st[tf_1d], st[tf_4h], st[tf_1h], st[tf_15m]

    # ---- 指标一: MACD 大顺 / 中逆 / 小顺 + 底部结构
    f_1d_up = s1d["bull"] or (s1d["hist"] > 0 and s1d["expand3"])
    f_4h_up = s4h["bull"] or (s4h["hist"] > 0 and s4h["expand3"])
    f_1h_counter = (not s1h["bull"]) and (s1h["shrink3"] or s1h["dif_turn_up"])
    f_15m_up = s15["bull"] or (s15["dif_turn_up"] and s15["hist_rising"])

    # 底部结构: 中周期死叉缩柱/拐头, 或 4H 在零轴下方的缩柱
    bottom_1h = (not s1h["bull"]) and (s1h["shrink3"] or (s1h["dif"] < 0 and s1h["dif_turn_up"]))
    bottom_4h = s4h["dif"] < 0 and s4h["shrink3"]
    macd_bottom = bottom_1h or bottom_4h

    macd_ok = bool(f_1d_up and f_4h_up and f_1h_counter and f_15m_up and macd_bottom)
    macd_count = sum([f_1d_up, f_4h_up, f_1h_counter, f_15m_up, macd_bottom])

    # ---- 指标二: EMA 7/25/99 多周期不破线
    ema_flags = {}
    ema_details = {}
    for tf, rule in ema_req.items():
        if tf not in bars:
            continue
        n = int(rule["ema"])
        ers = ema_state(closes[tf], lows[tf], n, int(rule.get("retest_bars", 3)))
        ema_flags[tf] = bool(ers and ers["ok"])
        ema_details[tf] = ers
    ema_ok = bool(ema_flags) and all(ema_flags.values())
    ema_count = sum(1 for v in ema_flags.values() if v)

    # ---- 附加质量项(仅用于排序)
    bonus = 0
    e7_4h = ema_state(closes[tf_4h], lows[tf_4h], 7, 3)
    e7_15 = ema_state(closes[tf_15m], lows[tf_15m], 7, 3)
    e99_4h = ema_state(closes[tf_4h], lows[tf_4h], 99, 1)
    if e7_4h and e7_4h["above"]:
        bonus += 1  # 4H 站稳 7 日线(示例二形态)
    if e7_15 and e7_15["above"]:
        bonus += 1  # 15M 紧贴 7 日线
    if e99_4h and e99_4h["above"]:
        bonus += 1  # 4H 位于 99 日线上方(大趋势健康)
    if s4h["above_zero"]:
        bonus += 1

    if macd_ok and ema_ok:
        tier = "S"
    elif macd_ok:
        tier = "A"
    elif macd_count >= 4:
        # 观察池: MACD 五条中只差一条, 尚未完全符合文档条件, 用于提前跟踪
        tier = "W"
    elif ema_ok:
        tier = "B"
    elif keep_all:
        tier = "N"
    else:
        return None

    close = closes[tf_15m][-1]
    score = macd_count + ema_count + bonus + (4 if tier == "S" else 0)

    # 止损: 15M 近 N 根最低价与 15M EMA25 取更低者, 再加缓冲
    acc = cfg.get("account") or {}
    lb = int(acc.get("stop_lookback_bars", 8))
    refs = list(lows[tf_15m][-lb:])
    ema25_15 = (ema_details.get("15m") or {}).get("ema")
    if ema25_15:
        refs.append(ema25_15)
    stop = min(refs) * (1 - float(acc.get("stop_buffer_pct", 0.2)) / 100.0)
    plan = plan_position(close, stop, cfg)

    # 止盈: 优先用结构位(1H 近 48 根高点 / 4H 近 30 根高点), 结构位太近则按 R 倍数外推
    r_unit = max(close - stop, 1e-9)
    h1 = highs[tf_1h][-48:] if len(highs[tf_1h]) >= 48 else highs[tf_1h]
    h4 = highs[tf_4h][-30:] if len(highs[tf_4h]) >= 30 else highs[tf_4h]
    res_1h, res_4h = max(h1), max(h4)
    # TP1 取结构高点, 但不超过 4R(结构太远时按 R 外推, 保证可执行)
    tp1 = min(res_1h, close + 4 * r_unit) if res_1h > close * 1.005 else close + 1.5 * r_unit
    # TP2 上限 6R: 结构位再远也不追, 否则等于把止盈挂在没人到的位置
    tp2 = min(max(res_4h, tp1 * 1.02, close + 2.5 * r_unit), close + 6 * r_unit)
    plan.update(
        {
            "entry": close,
            "tp1": tp1,
            "tp2": tp2,
            "rr1": (tp1 - close) / r_unit,
            "rr2": (tp2 - close) / r_unit,
            "entry_zone": [close * 0.997, close * 1.003],
            "tight_stop": plan["stop_dist_pct"] < 0.8,
        }
    )

    return {
        "symbol": symbol,
        "base": meta.get("base", symbol.replace("_USDT", "")),
        "price": close,
        "change_24h": meta.get("change_24h"),
        "quote_volume_24h": meta.get("quote_volume_24h"),
        "tier": tier,
        "score": score,
        "macd_count": macd_count,
        "macd": {
            "1d": s1d,
            "4h": s4h,
            "1h": s1h,
            "15m": s15,
            "flags": {
                "1d_up": f_1d_up,
                "4h_up": f_4h_up,
                "1h_counter": f_1h_counter,
                "15m_up": f_15m_up,
                "bottom": macd_bottom,
            },
            "ok": macd_ok,
        },
        "ema": {"flags": ema_flags, "details": ema_details, "ok": ema_ok},
        "bonus": bonus,
        "stop": stop,
        "plan": plan,
        "multiplier": meta.get("multiplier") or 0,
        "leverage_max": meta.get("leverage_max") or 0,
        "order_min": meta.get("order_min") or 1,
        "bars": {tf: len(bars[tf]) for tf in need},
        "last_bar_time": {tf: bars[tf][-1]["t"] for tf in need},
    }


def reasons(rec):
    """生成可读的判定理由"""
    out = []
    mf = rec["macd"]["flags"]
    bits = []
    if mf["1d_up"]:
        bits.append("1D顺")
    if mf["4h_up"]:
        bits.append("4H顺")
    if mf["1h_counter"]:
        bits.append("1H逆(底部)")
    if mf["15m_up"]:
        bits.append("15M顺")
    if bits:
        out.append("MACD " + " · ".join(bits))
    ef = rec["ema"]["flags"]
    eb = []
    for tf in ("4h", "1h", "15m"):
        if tf in ef and ef[tf]:
            d = rec["ema"]["details"].get(tf) or {}
            tag = "回踩收回" if d.get("retest") and not d.get("above") else "上"
            eb.append("%s%sEMA25" % (TF_LABEL[tf], tag))
    if eb:
        out.append("EMA " + " · ".join(eb))
    if rec["tier"] == "W":
        names = {"1d_up": "1D顺", "4h_up": "4H顺", "1h_counter": "1H逆", "15m_up": "15M顺", "bottom": "底部结构"}
        missing = [v for k, v in names.items() if not mf[k]]
        out.append("尚缺: " + " / ".join(missing))
    return out


# ----------------------------------------------------------------------------- output
def build_console(res, stats):
    lines = []
    lines.append("=" * 78)
    lines.append("Gate USDT 永续 · 多周期共振筛选   %s (UTC+8)" % stats["run_at"])
    lines.append(
        "样本 %d 个合约 / 有效 %d / 取数失败 %d / S级 %d / A级 %d / 观察池W %d / B级 %d / 耗时 %.1fs"
        % (
            stats["contracts"],
            stats["valid"],
            stats.get("failed", 0),
            stats["s"],
            stats["a"],
            stats.get("w", 0),
            stats["b"],
            stats["elapsed"],
        )
    )
    lines.append("=" * 78)
    if not res:
        lines.append("本轮无符合条件的合约。")
        return "\n".join(lines)
    for r in res:
        lines.append(
            "[%s] %-14s %-11s 24h额 %-8s 评分 %d" % (r["tier"], r["symbol"], fmt_price(r["price"]), human_vol(r["quote_volume_24h"]), r["score"])
        )
        for line in reasons(r):
            lines.append("       " + line)
        pl = r.get("plan")
        if pl and r["tier"] != "B":
            lines.append(
                "       点位: 入场 %s 止损 %s(-%.2f%%) TP1 %s(%.1fR) TP2 %s(%.1fR) | 名义 $%.0f %.1f倍 保证金 $%.2f%s"
                % (
                    fmt_price(pl["entry"]),
                    fmt_price(pl["stop"]),
                    pl["stop_dist_pct"],
                    fmt_price(pl["tp1"]),
                    pl["rr1"],
                    fmt_price(pl["tp2"]),
                    pl["rr2"],
                    pl["notional"],
                    pl["leverage"],
                    pl["margin"],
                    "" if pl["viable"] else ("  [!]爆仓过近, 杠杆需<=%.1f倍" % pl["lev_max_safe"]),
                )
            )
    return "\n".join(lines)


def build_wecom(res, stats, cfg, top_n):
    """只发信号本身: 合约 + 入场区间 + 止损 + 两档止盈 + 仓位。不发统计噪音。"""
    md = []
    md.append("## 🎯 永续共振信号")
    md.append("> %s (UTC+8)｜全市场 %d 个合约中筛出 %d 个" % (stats["run_at"], stats["valid"], len(res)))

    for r in res[:top_n]:
        pl = r.get("plan") or {}
        chg = r.get("change_24h")
        md.append("")
        tag = '　<font color="warning">[A→S 升级]</font>' if r.get("_upgrade") else ""
        md.append(
            '<font color="info">**%s**</font>　现价 %s%s%s'
            % (
                r["symbol"],
                fmt_price(r["price"]),
                "" if chg is None else ("　24h %+.2f%%" % chg),
                tag,
            )
        )
        if pl:
            lo, hi = pl.get("entry_zone", [r["price"], r["price"]])
            md.append("> 入场 %s ~ %s" % (fmt_price(lo), fmt_price(hi)))
            md.append(
                "> 止损 **%s**（-%.2f%%）" % (fmt_price(pl["stop"]), pl["stop_dist_pct"])
            )
            md.append(
                "> 止盈1 **%s**（+%.2f%%，%.1fR）　止盈2 **%s**（+%.2f%%，%.1fR）"
                % (
                    fmt_price(pl["tp1"]),
                    (pl["tp1"] / pl["entry"] - 1) * 100,
                    pl["rr1"],
                    fmt_price(pl["tp2"]),
                    (pl["tp2"] / pl["entry"] - 1) * 100,
                    pl["rr2"],
                )
            )
            md.append(
                "> 仓位：名义 $%.0f · %d倍 · 保证金 $%.2f · 单笔风险 $%.2f（本金 %.0f%%）"
                % (
                    pl["notional"],
                    int(pl["lev_set"]),
                    pl["margin"],
                    pl["risk_actual"],
                    pl["risk_actual"] / pl["equity"] * 100,
                )
            )
            if pl.get("cost_r_pct") is not None:
                md.append(
                    "> 摩擦成本 往返 %.2f%%（%.0f%%R）　净盈亏比 TP1 %.1fR / TP2 %.1fR"
                    % (
                        pl["cost_pct"],
                        pl["cost_r_pct"],
                        pl.get("rr1", 0) - pl.get("cost_r_ratio", 0),
                        pl.get("rr2", 0) - pl.get("cost_r_ratio", 0),
                    )
                )
            if pl.get("tight_stop"):
                md.append(
                    '<font color="warning">> ⚠ 止损仅 %.2f%%，低于 15M 正常波动，容易被扫损；建议等回踩站稳再进</font>'
                    % pl["stop_dist_pct"]
                )
            if not pl.get("viable", True):
                md.append(
                    '<font color="warning">> ⚠ 爆仓距离 %.2f%% 短于止损，杠杆需 ≤ %.1f 倍</font>'
                    % (pl["liq_dist_pct"], pl["lev_max_safe"])
                )
        md.append("> 依据：" + " ｜ ".join(reasons(r)))

    md.append("")
    md.append(
        '<font color="comment">仅推 S/A 级 + 24h成交额≥2000万U；同一信号 12 小时内只推一次，A 级升级为 S 级会再推一次。'
        "技术面机械信号，不构成投资建议。</font>"
    )
    return "\n".join(md)


def load_push_state():
    p = os.path.join(HERE, "state", "push_state.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f), p
    except Exception:  # noqa: BLE001
        return {"symbols": {}}, p


def save_push_state(state, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _sig_record(rec):
    """兼容旧格式(纯时间戳)与新格式(层级+时间戳)"""
    if isinstance(rec, dict):
        return str(rec.get("tier") or ""), float(rec.get("ts") or 0)
    if isinstance(rec, (int, float)):
        return "", float(rec)
    return "", 0.0


def _today_stats(state):
    """按自然日重置日内计数"""
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    daily = state.get("daily") or {}
    if daily.get("date") != today:
        daily = {"date": today, "signals": 0, "loss_usdt": 0.0}
        state["daily"] = daily
    return daily


def select_push(res, cfg, state):
    """推送过滤, 四层纪律:
    1) 层级 + 流动性
    2) 确定性: 止损不可过窄 / 爆仓距离必须够 / 摩擦成本占 R 不超过上限
    3) 去重: 同一信号 12 小时只推一次; A→S 升级可再推一次; 降级不推
    4) 日内风控: 每日最多 N 条; 当日亏损达上限则停手
    """
    pc = cfg.get("push") or {}
    disc = cfg.get("discipline") or {}
    tiers = set(pc.get("only_tiers") or ["S"])
    minv = float(pc.get("min_quote_volume_24h") or 0)
    cd = float(pc.get("cooldown_hours") or 0) * 3600
    allow_upgrade = bool(pc.get("repush_on_upgrade", True))

    max_cost_r = float(disc.get("max_cost_pct_of_r", 25))
    min_stop = float(disc.get("min_stop_pct", 0.8))
    max_sig = int(disc.get("max_signals_per_day", 2))
    day_stop = float(disc.get("daily_stop_loss_pct", 20))
    equity = float((cfg.get("account") or {}).get("equity_usdt", 40))

    now = time.time()
    syms = state.get("symbols") or {}
    daily = _today_stats(state)
    out, skipped = [], {
        "tier": 0, "volume": 0, "cooldown": 0, "upgrade": 0, "downgrade": 0,
        "cost": 0, "liq": 0, "tight": 0, "daily": 0, "breaker": 0,
    }

    # 当日熔断: 已实现亏损达到上限, 当天不再出任何信号
    if day_stop > 0 and float(daily.get("loss_usdt") or 0) >= equity * day_stop / 100.0:
        skipped["breaker"] = len(res)
        return [], skipped

    # 当日剩余可推条数(跨轮累计)
    quota = max(0, max_sig - int(daily.get("signals") or 0))

    for r in res:
        if r["tier"] not in tiers:
            skipped["tier"] += 1
            continue
        if (r.get("quote_volume_24h") or 0) < minv:
            skipped["volume"] += 1
            continue

        pl = r.get("plan") or {}
        # 确定性闸门: 三项硬约束, 任一不过就不是"确定的信号"
        if not pl.get("viable", True) or pl.get("liq_dist_pct", 99) <= pl.get("stop_dist_pct", 0):
            skipped["liq"] += 1
            continue
        if min_stop and pl.get("stop_dist_pct", 0) < min_stop:
            skipped["tight"] += 1
            continue
        if max_cost_r and pl.get("cost_r_pct", 0) > max_cost_r:
            skipped["cost"] += 1
            continue

        last_tier, last_ts = _sig_record(syms.get(r["symbol"]))
        in_cd = bool(cd) and (now - last_ts) < cd
        if in_cd:
            if last_tier == "A" and r["tier"] == "S" and allow_upgrade:
                r["_upgrade"] = True
                skipped["upgrade"] += 1
            elif last_tier == "S" and r["tier"] == "A":
                skipped["downgrade"] += 1
                continue
            else:
                skipped["cooldown"] += 1
                continue
        else:
            r["_upgrade"] = False

        if len(out) >= quota:
            skipped["daily"] += 1
            continue
        out.append(r)
    return out[: int(pc.get("top_n", 5))], skipped


def write_json(res, stats, path):
    payload = {"run_at": stats["run_at"], "stats": stats, "candidates": res}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


CSS = """
:root{--bg:#f5f7fa;--card:#fff;--ink:#1b2431;--sub:#6b7787;--line:#e6ebf2;
--up:#e14b4b;--down:#12a37a;--s:#e14b4b;--a:#e08a1e;--b:#3d7de0;--chip:#f0f4fa}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 6px;font-weight:650;letter-spacing:.3px}
.sub{color:var(--sub);font-size:13px;margin-bottom:22px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:26px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi .n{font-size:24px;font-weight:700;line-height:1.2}
.kpi .l{color:var(--sub);font-size:12px;margin-top:4px}
.section{margin:26px 0 10px;font-size:15px;font-weight:650;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin-bottom:12px;
box-shadow:0 1px 2px rgba(16,32,64,.04)}
.head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.sym{font-size:17px;font-weight:700;letter-spacing:.2px}
.price{font-variant-numeric:tabular-nums;font-weight:600}
.chg{font-size:13px;font-weight:600}
.vol{margin-left:auto;color:var(--sub);font-size:12px}
.score{background:var(--chip);color:var(--sub);border-radius:20px;padding:2px 10px;font-size:12px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.tag{font-size:12px;padding:3px 9px;border-radius:6px;background:var(--chip);color:#33445c}
.tag.on{background:#eaf7f1;color:#0d7a5b}
.tag.off{background:#fdf0f0;color:#b8443f}
.mrow{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:12px}
.tf{background:#fafbfd;border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:12px;color:#42506a}
.tf b{display:block;font-size:12px;color:#8b96a6;font-weight:600;margin-bottom:3px}
.empty{color:var(--sub);font-size:13px;padding:14px 0}
.plan{margin-top:10px;padding-top:10px;border-top:1px dashed var(--line);color:#42506a;font-size:12.5px}
.foot{margin-top:30px;color:#98a2b0;font-size:12px;line-height:1.7}
"""


def write_html(res, stats, path):
    color_job = lambda v: ("var(--up)" if (v or 0) >= 0 else "var(--down)")
    parts = []
    parts.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append("<title>USDT永续 · 多周期共振扫描</title>")
    parts.append("<style>%s</style></head><body><div class='wrap'>" % CSS)
    parts.append("<h1>USDT 永续 · 多周期共振扫描</h1>")
    parts.append(
        "<div class='sub'>%s (UTC+8)　来源 Gate USDT 永续　MACD(12,26,9) + EMA(7/25/99)</div>"
        % stats["run_at"]
    )
    parts.append("<div class='kpis'>")
    for n, l in [
        (stats["contracts"], "扫描合约"),
        (stats["valid"], "数据有效"),
        (stats["s"], "S级 双指标共振"),
        (stats["a"], "A级 仅MACD"),
        (stats.get("w", 0), "观察池 MACD 4/5"),
        (stats["b"], "B级 仅EMA"),
    ]:
        parts.append("<div class='kpi'><div class='n'>%s</div><div class='l'>%s</div></div>" % (n, l))
    parts.append("</div>")

    titles = {
        "S": ("S级 · 双指标共振", "var(--s)"),
        "A": ("A级 · 仅MACD成立", "var(--a)"),
        "W": ("观察池 · MACD 五条中差一条", "var(--a)"),
        "B": ("B级 · 仅EMA成立", "var(--b)"),
    }
    any_card = False
    for tier in ("S", "A", "W", "B"):
        group = [r for r in res if r["tier"] == tier]
        title, c = titles[tier]
        parts.append(
            "<div class='section'><span class='dot' style='background:%s'></span>%s<span style='color:var(--sub);font-weight:400'>（%d）</span></div>"
            % (c, title, len(group))
        )
        if not group:
            parts.append("<div class='empty'>本轮无符合该层级的合约</div>")
            continue
        for r in group:
            any_card = True
            chg = r.get("change_24h")
            chg_txt = "-" if chg is None else "%+.2f%%" % chg
            parts.append("<div class='card'>")
            parts.append("<div class='head'>")
            parts.append("<span class='sym'>%s</span>" % escape(r["symbol"]))
            parts.append("<span class='price'>%s</span>" % fmt_price(r["price"]))
            parts.append("<span class='chg' style='color:%s'>%s</span>" % (color_job(chg), chg_txt))
            parts.append("<span class='vol'>24h 成交额 %s USDT</span>" % human_vol(r["quote_volume_24h"]))
            parts.append("<span class='score'>评分 %d</span>" % r["score"])
            parts.append("</div>")

            tags = []
            mf = r["macd"]["flags"]
            for key, label in [("1d_up", "1D 顺"), ("4h_up", "4H 顺"), ("1h_counter", "1H 逆"), ("15m_up", "15M 顺"), ("bottom", "底部结构")]:
                tags.append("<span class='tag %s'>%s</span>" % ("on" if mf[key] else "off", label))
            for tf in ("4h", "1h", "15m"):
                ok = r["ema"]["flags"].get(tf)
                d = r["ema"]["details"].get(tf) or {}
                extra = "·回踩收回" if (ok and d.get("retest")) else ""
                tags.append(
                    "<span class='tag %s'>EMA %s %s%s</span>" % ("on" if ok else "off", TF_LABEL[tf], "≥25" if ok else "<25", extra)
                )
            parts.append("<div class='tags'>%s</div>" % "".join(tags))

            parts.append("<div class='mrow'>")
            for tf in ("1d", "4h", "1h", "15m"):
                s = r["macd"][tf]
                state = "多头" if s["bull"] else "空头"
                parts.append(
                    "<div class='tf'><b>%s MACD</b>%s　DIF %s<br>柱 %s%s</div>"
                    % (
                        TF_LABEL[tf],
                        state,
                        ("%.4g" % s["dif"]),
                        ("%.4g" % s["hist"]),
                        "　缩柱" if s["shrink3"] else ("　放大" if s["expand3"] else ""),
                    )
                )
            pl = r.get("plan")
            if pl and tier != "B":
                parts.append(
                    "<div class='plan'>入场 %s ｜ 止损 %s（-%.2f%%）｜ TP1 %s（%.1fR）｜ TP2 %s（%.1fR）｜ 名义 $%.0f ｜ %.1f 倍 ｜ 保证金 $%.2f%s</div>"
                    % (
                        fmt_price(pl["entry"]),
                        fmt_price(pl["stop"]),
                        pl["stop_dist_pct"],
                        fmt_price(pl["tp1"]),
                        pl["rr1"],
                        fmt_price(pl["tp2"]),
                        pl["rr2"],
                        pl["notional"],
                        pl["leverage"],
                        pl["margin"],
                        "" if pl["viable"] else ("　⚠ 爆仓距离过近，杠杆需 ≤ %.1f 倍" % pl["lev_max_safe"]),
                    )
                )
            parts.append("</div></div>")
    if not any_card:
        parts.append("<div class='empty'>本轮全市场无符合条件合约。</div>")

    parts.append(
        "<div class='foot'>筛选逻辑严格按文档执行：MACD 1D/4H 顺周期 · 1H 逆周期且处于底部结构（缩柱或拐头）· 15M 顺周期；"
        "EMA 4H、1H、15M 均不跌破 25 日线，或回踩后快速收回（近 3 根K线内下探并收回）。<br>"
        "数据为 Gate USDT 永续已收盘K线；仅为技术面机械筛选结果，不构成投资建议。</div>"
    )
    parts.append("</div></body></html>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def push_wecom(webhook, content, max_bytes):
    data = content.encode("utf-8")
    if len(data) > max_bytes:
        cut = content.encode("utf-8")[: max_bytes - 60].decode("utf-8", "ignore")
        content = cut + "\n> …内容过长已截断"
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload, headers={"Content-Type": "application/json", "User-Agent": "crypto-scanner"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ----------------------------------------------------------------------------- main
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def scan(cfg, limit=None, min_volume=None, use_cache=True, diag=False, symbol=None):
    t0 = time.time()
    cli = GateClient(cfg, use_cache=use_cache)
    ds = cfg["data_source"]

    contracts = cli.contracts()
    tickers = {t["contract"]: t for t in cli.tickers()}

    exclude_types = set(ds.get("contract_types_exclude") or [])
    universe = []
    for c in contracts:
        name = c.get("name")
        if not name or not name.endswith("_" + ds["settle"].upper()):
            continue
        if c.get("in_delisting"):
            continue
        if (c.get("contract_type") or "") in exclude_types:
            continue
        if ds.get("exclude_pre_market") and c.get("is_pre_market"):
            continue
        universe.append(c)

    minv = ds.get("min_quote_volume_24h", 0) if min_volume is None else min_volume
    if minv:
        keep = []
        for c in universe:
            t = tickers.get(c["name"]) or {}
            try:
                v = float(t.get("volume_24h_settle") or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v >= minv:
                keep.append(c)
        universe = keep

    if symbol:
        key = symbol.upper()
        if not key.endswith("_" + ds["settle"].upper()):
            key += "_" + ds["settle"].upper()
        universe = [c for c in universe if c["name"] == key]
        minv = 0  # 指定合约时跳过流动性门槛

    by_name = {c["name"]: c for c in universe}

    universe.sort(key=lambda c: -float((tickers.get(c["name"]) or {}).get("volume_24h_settle") or 0))
    if limit:
        universe = universe[:limit]

    tfs = [cfg["timeframes"]["large"][0], cfg["timeframes"]["large"][1], cfg["timeframes"]["mid"], cfg["timeframes"]["small"]]
    workers = int(cfg["runtime"]["workers"])

    def job(c):
        name = c["name"]
        series = {}
        try:
            for tf in tfs:
                series[tf] = cli.candles(name, tf)
        except Exception:  # noqa: BLE001
            return name, None
        return name, series

    results, valid, failed = [], 0, []
    diag_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for name, series in pool.map(job, universe):
            if not series:
                failed.append(name)
                continue
            t = tickers.get(name) or {}
            c0 = by_name.get(name) or {}
            meta = {
                "base": name.replace("_USDT", ""),
                "multiplier": float(c0.get("quanto_multiplier") or 0),
                "leverage_max": int(c0.get("leverage_max") or 0),
                "order_min": float(c0.get("order_size_min") or 1),
            }
            try:
                meta["quote_volume_24h"] = float(t.get("volume_24h_settle") or 0)
                meta["change_24h"] = float(t.get("change_percentage") or 0)
            except (TypeError, ValueError):
                meta["quote_volume_24h"] = 0.0
                meta["change_24h"] = None
            rec = evaluate(name, series, cfg, meta, keep_all=diag)
            valid += 1
            if rec:
                if rec["tier"] == "N":
                    diag_rows.append(rec)
                else:
                    results.append(rec)

    order = {"S": 0, "A": 1, "W": 2, "B": 3}
    results.sort(key=lambda r: (order[r["tier"]], -r["score"], -(r.get("quote_volume_24h") or 0)))
    stats = {
        "run_at": now_bj().strftime("%Y-%m-%d %H:%M"),
        "contracts": len(universe),
        "valid": valid,
        "s": sum(1 for r in results if r["tier"] == "S"),
        "a": sum(1 for r in results if r["tier"] == "A"),
        "w": sum(1 for r in results if r["tier"] == "W"),
        "b": sum(1 for r in results if r["tier"] == "B"),
        "elapsed": round(time.time() - t0, 1),
        "failed": len(failed),
        "failed_list": failed[:10],
        "source": "gate-usdt-perp",
    }
    if diag:
        stats["diag"] = flag_distribution(diag_rows + results)
    return results, stats, diag_rows


def flag_distribution(rows):
    """统计各判定条件的通过率, 用于校准阈值"""
    keys = [
        ("1D 顺", lambda r: r["macd"]["flags"]["1d_up"]),
        ("4H 顺", lambda r: r["macd"]["flags"]["4h_up"]),
        ("1H 逆", lambda r: r["macd"]["flags"]["1h_counter"]),
        ("15M 顺", lambda r: r["macd"]["flags"]["15m_up"]),
        ("底部结构", lambda r: r["macd"]["flags"]["bottom"]),
        ("MACD 全条件", lambda r: r["macd"]["ok"]),
        ("EMA 4H", lambda r: r["ema"]["flags"].get("4h", False)),
        ("EMA 1H", lambda r: r["ema"]["flags"].get("1h", False)),
        ("EMA 15M", lambda r: r["ema"]["flags"].get("15m", False)),
        ("EMA 全条件", lambda r: r["ema"]["ok"]),
    ]
    total = len(rows) or 1
    out = []
    for name, fn in keys:
        n = sum(1 for r in rows if fn(r))
        out.append({"cond": name, "pass": n, "ratio": round(100.0 * n / total, 1)})
    # 接近命中: MACD 满足 4 项及以上
    near = sorted(
        [r for r in rows if sum(1 for v in r["macd"]["flags"].values() if v) >= 4],
        key=lambda r: -r["score"],
    )[:12]
    return {
        "total": len(rows),
        "conditions": out,
        "near_miss": [
            {"symbol": r["symbol"], "macd_pass": sum(1 for v in r["macd"]["flags"].values() if v), "score": r["score"],
             "flags": r["macd"]["flags"]}
            for r in near
        ],
    }


def run_once(cfg, args):
    os.makedirs(REPORT_DIR, exist_ok=True)
    res, stats, diag_rows = scan(
        cfg,
        limit=args.limit,
        min_volume=args.min_volume,
        use_cache=not args.no_cache,
        diag=args.diag,
    )
    print(build_console(res, stats))
    if args.diag and stats.get("diag"):
        d = stats["diag"]
        print("\n--- 条件通过率 (样本 %d) ---" % d["total"])
        for c in d["conditions"]:
            print("  %-12s %5d  %5.1f%%" % (c["cond"], c["pass"], c["ratio"]))
        print("--- MACD 接近命中(>=4/5) ---")
        for r in d["near_miss"]:
            print("  %-14s %d/5" % (r["symbol"], r["macd_pass"]))
        write_json(diag_rows, stats, os.path.join(REPORT_DIR, "diag.json"))

    write_json(res, stats, os.path.join(REPORT_DIR, "latest.json"))
    html_path = os.path.join(REPORT_DIR, "latest.html")
    write_html(res, stats, html_path)
    print("HTML -> %s" % html_path)

    webhook = (cfg.get("push", {}).get("wecom_webhook") or os.environ.get("WECOM_WEBHOOK") or "").strip()
    state, state_path = load_push_state()
    targets, skipped = select_push(res, cfg, state)
    mode = (cfg.get("push") or {}).get("mode", "always")

    cd_h = float((cfg.get("push") or {}).get("cooldown_hours") or 0)
    if not targets:
        print(
            "[quiet] 本轮无可推送信号（层级 %d / 成交额 %d / 成本超限 %d / 爆仓过近 %d / 止损过窄 %d "
            "/ %.0f小时内已推 %d / 降级 %d / 日内限额 %d / 熔断 %d），不发送消息"
            % (
                skipped["tier"], skipped["volume"], skipped["cost"], skipped["liq"], skipped["tight"],
                cd_h, skipped["cooldown"], skipped["downgrade"], skipped["daily"], skipped["breaker"],
            )
        )
        if args.dry_run:
            preview = build_wecom(res[:3], stats, cfg, 3) if res else "(空)"
            print("--- 预览（不会发送）---\n" + preview)
        return res, stats, diag_rows

    if skipped.get("upgrade"):
        print("[upgrade] %d 条 A→S 升级信号，已在冷却期内仍推送" % skipped["upgrade"])

    if args.dry_run or args.no_push:
        print("[skip] 未推送（dry-run），将发送 %d 条信号" % len(targets))
        print("--- 预览 ---\n" + build_wecom(targets, stats, cfg, len(targets)))
        return res, stats, diag_rows
    if not webhook:
        print("[warn] 未配置企业微信 webhook，本次仅生成本地报告")
        return res, stats, diag_rows

    md = build_wecom(targets, stats, cfg, len(targets))
    try:
        resp = push_wecom(webhook, md, int(cfg["push"].get("max_bytes", 3800)))
        print("[push] 发送 %d 条信号，企业微信返回: %s" % (len(targets), json.dumps(resp, ensure_ascii=False)))
        if resp.get("errcode") == 0:
            now_ts = time.time()
            state.setdefault("symbols", {})
            for r in targets:
                prev = state["symbols"].get(r["symbol"])
                prev_tier, prev_ts = _sig_record(prev)
                state["symbols"][r["symbol"]] = {
                    "tier": r["tier"],
                    "ts": now_ts,
                    "prev_tier": prev_tier,
                    "prev_ts": prev_ts,
                    "push_count": int((prev or {}).get("push_count") or 0) + 1 if isinstance(prev, dict) else 1,
                    "at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
                }
            daily = _today_stats(state)
            daily["signals"] = int(daily.get("signals") or 0) + len(targets)
            disc = cfg.get("discipline") or {}
            print(
                "[risk] 今日已推 %d/%d 条 · 当日亏损 %.2f USDT（熔断线 %.0f%% = %.2f USDT）"
                % (
                    daily["signals"],
                    int(disc.get("max_signals_per_day", 2)),
                    float(daily.get("loss_usdt") or 0),
                    float(disc.get("daily_stop_loss_pct", 20)),
                    float((cfg.get("account") or {}).get("equity_usdt", 40)) * float(disc.get("daily_stop_loss_pct", 20)) / 100.0,
                )
            )
            state["last_push_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
            save_push_state(state, state_path)
    except Exception as e:  # noqa: BLE001
        print("[error] 推送失败: %s" % e)
    return res, stats, diag_rows


def risk_matrix(cfg):
    acc = cfg.get("account") or {}
    eq = float(acc.get("equity_usdt", 40))
    fee = float(acc.get("fee_rate", 0.0005))
    mmr = float(acc.get("mmr", 0.01))
    print("=" * 72)
    print("账户 %.0f USDT · 20 倍全仓(保证金 %.0f) → 名义 %.0f USDT · 维持保证金率 %.1f%%"
          % (eq, eq, eq * 20, mmr * 100))
    print("理论爆仓价 ≈ 入场价 × (1 - 1/20 + %.2f) = 反向 %.2f%%" % (mmr, (0.05 - mmr) * 100))
    print("-" * 72)
    print("价格反向   亏损(USDT)   占本金")
    for mv in (0.5, 1, 1.5, 2, 3, 4):
        loss = eq * 20 * mv / 100
        print("  -%.1f%%      %7.2f     %5.0f%%%s" % (mv, loss, loss / eq * 100, "   ← 爆仓" if mv >= 4 else ""))
    print("开平各 %.2f%% 手续费: %.2f USDT = 本金 %.1f%%" % (fee * 100, eq * 20 * fee * 2, 20 * fee * 2 * 100))
    print("=" * 72)
    print("按止损距离反推杠杆（风险预算 %.0f%% = %.2f USDT，上限 20 倍）：" % (acc.get("risk_pct", 25), eq * acc.get("risk_pct", 25) / 100))
    print("止损距离   名义(USDT)   杠杆     实际风险(USDT)  爆仓距离")
    for sd in (0.8, 1.2, 1.5, 2, 3, 4):
        p = plan_position(100.0, 100.0 * (1 - sd / 100), cfg)
        print("  %.1f%%      %8.0f     %5.1f倍    %7.2f         %.2f%%"
              % (sd, p["notional"], p["leverage"], p["risk_actual"], p["liq_dist_pct"]))


def order_sheet(cfg, symbol):
    """输出可直接照着挂单的指令清单, 并逐条列出是否通过确定性闸门"""
    res, stats, diag = scan(cfg, use_cache=True, diag=True, symbol=symbol)
    recs = res + diag
    if not recs:
        print("未取到 %s 的K线（合约名可能不对，或该周期数据不足）" % symbol)
        return
    r = recs[0]
    pl = r["plan"]
    price = r["price"]
    mult = float(r.get("multiplier") or 0)
    qty = pl["notional"] / (price * mult) if mult else 0.0
    disc = cfg.get("discipline") or {}
    pc = cfg.get("push") or {}

    gates = [
        ("层级 S/A", r["tier"] in set(pc.get("only_tiers") or ["S"]), "当前 %s 级" % r["tier"]),
        ("成交额≥2000万U", (r.get("quote_volume_24h") or 0) >= float(pc.get("min_quote_volume_24h") or 0),
         "%.0f 万" % ((r.get("quote_volume_24h") or 0) / 1e4)),
        ("爆仓>止损", bool(pl.get("viable")) and pl["liq_dist_pct"] > pl["stop_dist_pct"],
         "爆仓 %.2f%% / 止损 %.2f%%" % (pl["liq_dist_pct"], pl["stop_dist_pct"])),
        ("止损≥%.1f%%" % float(disc.get("min_stop_pct", 0.8)), pl["stop_dist_pct"] >= float(disc.get("min_stop_pct", 0.8)),
         "%.2f%%" % pl["stop_dist_pct"]),
        ("成本≤%d%%R" % int(disc.get("max_cost_pct_of_r", 25)), pl["cost_r_pct"] <= float(disc.get("max_cost_pct_of_r", 25)),
         "%.0f%%R" % pl["cost_r_pct"]),
    ]
    ok_all = all(g[1] for g in gates)

    W = 62
    print("=" * W)
    print("下单指令　%s　现价 %s　24h %s" % (
        r["symbol"], fmt_price(price),
        "-" if r.get("change_24h") is None else "%+.2f%%" % r["change_24h"]))
    print("=" * W)
    print("方向　　做多（USDT 永续）")
    print("杠杆　　%d 倍（在交易所设置，用全仓或逐仓自选）" % int(pl["lev_set"]))
    print("保证金　%.2f USDT（占本金 %.0f%%）" % (pl["margin"], pl["margin"] / pl["equity"] * 100))
    print("名义　　%.0f USDT" % pl["notional"])
    if mult:
        print("张数　　%d 张（合约乘数 %g，1 张 = %g 个币）" % (round(qty), mult, mult))
    else:
        print("张数　　按名义 %.0f USDT 折算（未取到乘数）" % pl["notional"])
    print("入场　　限价 %s（区间 %s ~ %s）" % (
        fmt_price(price), fmt_price(pl["entry_zone"][0]), fmt_price(pl["entry_zone"][1])))
    print("止损　　%s（-%.2f%%）　触发后按市价平仓" % (fmt_price(pl["stop"]), pl["stop_dist_pct"]))
    print("TP1　　%s（+%.2f%%，%.1fR）　平 50%% ≈ %d 张" % (
        fmt_price(pl["tp1"]), (pl["tp1"] / price - 1) * 100, pl["rr1"], round(qty / 2)))
    print("TP2　　%s（+%.2f%%，%.1fR）　平剩余" % (
        fmt_price(pl["tp2"]), (pl["tp2"] / price - 1) * 100, pl["rr2"]))
    print("单笔风险 %.2f USDT（本金 %.0f%%）　摩擦成本 %.2f%% ≈ %.0f%%R" % (
        pl["risk_actual"], pl["risk_actual"] / pl["equity"] * 100, pl["cost_pct"], pl["cost_r_pct"]))
    print("-" * W)
    for name, ok, detail in gates:
        print("  [%s] %-14s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("-" * W)
    print("结论：%s" % ("全部通过，可按下单指令执行" if ok_all else "未通过闸门，本单不下（等下一个信号）"))
    if ok_all:
        print("纪律：入场后立刻挂上止损；TP1 成交后把止损移到入场价；单笔亏损到线就走，不加仓不摊平。")
    print("=" * W)


def record_loss(cfg, amount):
    state, path = load_push_state()
    daily = _today_stats(state)
    daily["loss_usdt"] = round(float(daily.get("loss_usdt") or 0) + abs(amount), 4)
    state["daily"] = daily
    save_push_state(state, path)
    equity = float((cfg.get("account") or {}).get("equity_usdt", 40))
    line = float((cfg.get("discipline") or {}).get("daily_stop_loss_pct", 20))
    limit = equity * line / 100.0
    print(
        "已登记亏损 %.2f USDT → 当日累计 %.2f / 熔断线 %.2f（%s）"
        % (abs(amount), daily["loss_usdt"], limit, "已熔断，今日停止出信号" if daily["loss_usdt"] >= limit else "未触发")
    )


def print_status(cfg):
    state, path = load_push_state()
    daily = state.get("daily") or {}
    disc = cfg.get("discipline") or {}
    equity = float((cfg.get("account") or {}).get("equity_usdt", 40))
    limit = equity * float(disc.get("daily_stop_loss_pct", 20)) / 100.0
    print("状态文件: %s" % path)
    print("今日 %s：已推 %s/%s 条 · 已实现亏损 %.2f USDT（熔断线 %.2f）"
          % (daily.get("date", "-"), daily.get("signals", 0), disc.get("max_signals_per_day", 2),
             float(daily.get("loss_usdt") or 0), limit))
    cd = float((cfg.get("push") or {}).get("cooldown_hours") or 0)
    syms = state.get("symbols") or {}
    if not syms:
        print("冷却列表: 空")
        return
    print("冷却列表（%d 个）：" % len(syms))
    now = time.time()
    for sym, rec in sorted(syms.items()):
        tier, ts = _sig_record(rec)
        left = max(0.0, cd - (now - ts) / 3600.0)
        print("  %-14s %s  推送于 %s  剩余冷却 %.1f 小时"
              % (sym, tier or "-", (rec or {}).get("at", "-") if isinstance(rec, dict) else "-", left))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--limit", type=int, default=None, help="只扫描成交额前 N 个合约(调试用)")
    ap.add_argument("--min-volume", type=float, default=None, help="24h 成交额下限(USDT)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--loop", type=int, default=0, help="常驻模式: 每 N 秒扫描一次")
    ap.add_argument(
        "--align",
        action="store_true",
        help="常驻模式下对齐到周期收盘(:00/:15/:30/:45)后 20 秒再扫描(默认开启, 用 --no-align 关闭)",
    )
    ap.add_argument("--no-align", dest="align", action="store_false", help="不对齐收盘时间")
    ap.add_argument("--loop-log", default="", help="常驻模式日志文件(默认 logs/loop.log)")
    ap.add_argument("--loss", type=float, default=None, help="登记当日已实现亏损(USDT, 正数), 达到熔断线后当日停止出信号")
    ap.add_argument("--status", action="store_true", help="查看推送状态: 今日已推条数/亏损/各币种冷却")
    ap.add_argument("--symbol", default="", help="只扫描指定合约, 如 HBAR_USDT 或 HBAR")
    ap.add_argument("--order", default="", help="输出某合约的下单指令清单(不用等 S 级)")
    ap.add_argument("--diag", action="store_true", help="输出各条件通过率诊断")
    ap.add_argument("--no-cache", action="store_true", help="忽略本地K线缓存")
    ap.add_argument("--risk-matrix", action="store_true", help="打印账户杠杆/止损风险矩阵")
    ap.set_defaults(align=True)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.risk_matrix:
        risk_matrix(cfg)
        return
    if args.loss is not None:
        record_loss(cfg, args.loss)
        return
    if args.status:
        print_status(cfg)
        return
    if args.order:
        order_sheet(cfg, args.order)
        return
    if args.loop and args.loop > 0:
        log_path = args.loop_log or os.path.join(HERE, "logs", "loop.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        print("[loop] 每 %d 秒扫描一次，对齐收盘=%s，日志 %s" % (args.loop, args.align, log_path))

        def _log(msg):
            line = "[%s] %s" % (datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), msg)
            print(line)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

        while True:
            try:
                # 每轮重新读取配置, 改 webhook / 阈值无需重启进程
                cfg = load_config(args.config)
                run_once(cfg, args)
                _log("本轮完成")
            except Exception as e:  # noqa: BLE001
                _log("[error] 本轮失败: %s" % e)
            if args.align:
                wait = args.loop - (time.time() % args.loop) + 20
            else:
                wait = args.loop
            _log("下一轮 %.0f 秒后" % wait)
            time.sleep(wait)
    else:
        run_once(cfg, args)


if __name__ == "__main__":
    main()
