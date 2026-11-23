"""指標計算、三種篩選、自選股、策略成效追蹤。"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd

from .providers import finmind

COMMON_STOCK = re.compile(r"^[1-9]\d{3}$")  # 一般上市股票（排除 00 開頭 ETF、權證、特別股等）


# ---------------------------------------------------------------- 指標

def _streak(arr: np.ndarray) -> int:
    n = 0
    for v in arr[::-1]:
        if v > 0:
            n += 1
        else:
            break
    return n


def indicators(prices: pd.DataFrame, inst: pd.DataFrame, latest_quotes: pd.DataFrame,
               valuation: pd.DataFrame, cfg: dict, margin: pd.DataFrame | None = None) -> pd.DataFrame:
    close = prices.pivot(index="date", columns="code", values="close").sort_index()
    vol = prices.pivot(index="date", columns="code", values="volume").sort_index()

    mom_n = cfg["momentum"]["ma_days"]
    gro_n = cfg["growth"]["ma_days"]
    base_n = cfg["momentum"]["volume_ratio_base_days"]

    ma_m = close.rolling(mom_n, min_periods=mom_n).mean()
    ma_g = close.rolling(gro_n, min_periods=gro_n).mean()

    df = latest_quotes.set_index("code")[["name", "close", "change", "volume"]].copy()
    prev_close = df["close"] - df["change"]
    df["chg_pct"] = np.where(prev_close > 0, df["change"] / prev_close * 100, np.nan)

    df["ma20"] = ma_m.iloc[-1].reindex(df.index)
    df["ma20_prev"] = ma_m.iloc[-2].reindex(df.index) if len(ma_m) > 1 else np.nan
    df["ma60"] = ma_g.iloc[-1].reindex(df.index)
    df["ma20_dist"] = (df["close"] / df["ma20"] - 1) * 100
    df["ma60_dist"] = (df["close"] / df["ma60"] - 1) * 100

    df["avg_vol20"] = vol.iloc[-20:].mean().reindex(df.index)
    base = vol.iloc[-(base_n + 1):-1].mean() if len(vol) > base_n else pd.Series(dtype=float)
    df["vol_ratio"] = (df["volume"] / base.reindex(df.index)).replace([np.inf, -np.inf], np.nan)

    if not inst.empty:
        all_dates = sorted(inst["date"].unique())
        f = inst.pivot(index="date", columns="code", values="foreign").reindex(all_dates).fillna(0)
        t = inst.pivot(index="date", columns="code", values="trust").reindex(all_dates).fillna(0)
        dl = inst.pivot(index="date", columns="code", values="dealer").reindex(all_dates).fillna(0)
        df["foreign1"] = f.iloc[-1].reindex(df.index)
        df["trust1"] = t.iloc[-1].reindex(df.index)
        df["dealer5"] = dl.iloc[-5:].sum().reindex(df.index)
        df["foreign_streak"] = pd.Series({c: _streak(f[c].values) for c in f.columns}).reindex(df.index).fillna(0)
        df["trust_streak"] = pd.Series({c: _streak(t[c].values) for c in t.columns}).reindex(df.index).fillna(0)
        df["foreign5"] = f.iloc[-5:].sum().reindex(df.index)
        df["trust5"] = t.iloc[-5:].sum().reindex(df.index)
    else:
        for c in ("foreign_streak", "trust_streak", "foreign5", "trust5", "foreign1", "trust1", "dealer5"):
            df[c] = np.nan

    if margin is not None and not margin.empty:
        mg = margin.drop_duplicates("code").set_index("code")
        df["margin_chg"] = (mg["margin_today"] - mg["margin_prev"]).reindex(df.index)
        df["margin_bal"] = mg["margin_today"].reindex(df.index)
    else:
        df["margin_chg"] = df["margin_bal"] = np.nan

    v = valuation.set_index("code")[["pe", "yield", "pb"]] if not valuation.empty else pd.DataFrame(columns=["pe", "yield", "pb"])
    df = df.join(v, how="left")
    df["is_common"] = df.index.to_series().str.match(COMMON_STOCK)
    df["days"] = close.iloc[-gro_n:].notna().sum().reindex(df.index).fillna(0)
    return df


# ---------------------------------------------------------------- 篩選

def _liquid(df: pd.DataFrame, cfg: dict) -> pd.Series:
    return df["is_common"] & (df["avg_vol20"] >= cfg["common"]["min_avg_volume_20d"])


def screen_value(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    c = cfg["value"]
    m = (_liquid(df, cfg) & (df["pe"] > 0) & (df["pe"] < c["pe_max"])
         & (df["yield"] > c["yield_min"]) & (df["pb"] < c["pb_max"]))
    return df[m].sort_values("yield", ascending=False)


def screen_momentum(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    c = cfg["momentum"]
    m = (_liquid(df, cfg) & (df["close"] > df["ma20"]) & (df["ma20"] > df["ma20_prev"])
         & (df["vol_ratio"] >= c["volume_ratio_min"])
         & ((df["foreign_streak"] >= c["inst_streak_min"]) | (df["trust_streak"] >= c["inst_streak_min"])))
    return df[m].sort_values("vol_ratio", ascending=False)


def screen_growth(df: pd.DataFrame, cfg: dict) -> tuple[pd.DataFrame, str]:
    """回傳（結果, 狀態訊息）。狀態為空字串代表正常執行。"""
    c = cfg["growth"]
    if not c.get("enabled", True):
        return df.iloc[0:0], "已在設定檔關閉"
    if not finmind.token():
        return df.iloc[0:0], "尚未啟用：需要 FinMind token（設定 GitHub Secret「FINMIND_TOKEN」並實作月營收抓取）"
    cand = df[_liquid(df, cfg) & (df["close"] > df["ma60"])]
    try:
        rev = finmind.get_monthly_revenue(cand.index.tolist())
    except NotImplementedError:
        return df.iloc[0:0], "尚未啟用：FinMind token 已設定，但月營收抓取尚未實作"
    except Exception as e:  # noqa: BLE001
        return df.iloc[0:0], f"月營收抓取失敗：{e}"
    rev = rev.sort_values(["code", "ym"])
    k = c["revenue_positive_months"]
    ok = []
    for code, g in rev.groupby("code"):
        last = g.tail(k)
        if len(last) == k and (last["yoy"] > 0).all() and last["yoy"].iloc[-1] > c["revenue_yoy_min"]:
            ok.append((code, last["yoy"].iloc[-1], last["ym"].iloc[-1]))
    if not ok:
        return df.iloc[0:0], ""
    extra = pd.DataFrame(ok, columns=["code", "rev_yoy", "rev_ym"]).set_index("code")
    out = cand.join(extra, how="inner")
    return out.sort_values("rev_yoy", ascending=False), ""


# ---------------------------------------------------------------- 輸出轉換

def _clean(v):
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(v) else round(float(v), 2)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def records(df: pd.DataFrame, cols: list[str], hits: dict[str, list[str]]) -> list[dict]:
    out = []
    for code, r in df.iterrows():
        rec = {"code": code}
        rec.update({c: _clean(r.get(c)) for c in cols})
        rec["hits"] = hits.get(code, [])
        out.append(rec)
    return out


WATCH_COLS = ["close", "chg_pct", "volume", "vol_ratio", "ma20_dist", "ma60_dist",
              "foreign1", "foreign5", "trust1", "trust5", "dealer5", "margin_chg", "margin_bal",
              "pe", "yield", "pb"]


def watchlist_rows(wl: pd.DataFrame, df: pd.DataFrame, hits: dict[str, list[str]]) -> list[dict]:
    rows = []
    for _, w in wl.iterrows():
        code = w["code"]
        if code in df.index:
            r = df.loc[code]
            rec = {"code": code, "name": w["name"] or r["name"], "note": w["note"], "status": "ok"}
            rec.update({c: _clean(r.get(c)) for c in WATCH_COLS})
        else:
            rec = {"code": code, "name": w["name"], "note": w["note"], "status": "無資料（可能為上櫃或代號錯誤）"}
        rec["hits"] = hits.get(code, [])
        rows.append(rec)
    return rows


# ---------------------------------------------------------------- 成效追蹤

HORIZONS = (5, 20)


def update_tracking(picks: pd.DataFrame, prices: pd.DataFrame, market: pd.DataFrame) -> pd.DataFrame:
    """補上每筆選股在 5 / 20 個交易日後的報酬與同期大盤報酬（算過的不重算）。"""
    if picks.empty:
        return picks
    picks = picks.copy()
    for h in HORIZONS:
        for col in (f"ret{h}", f"mkt{h}"):
            if col not in picks:
                picks[col] = np.nan
    dates = sorted(prices["date"].unique())
    pos = {d: i for i, d in enumerate(dates)}
    close = prices.pivot(index="date", columns="code", values="close")
    taiex = market.set_index("date")["taiex"] if not market.empty else pd.Series(dtype=float)
    for idx, p in picks.iterrows():
        i = pos.get(p["date"])
        if i is None:
            continue
        for h in HORIZONS:
            if not pd.isna(p.get(f"ret{h}")) or i + h >= len(dates):
                continue
            d2 = dates[i + h]
            c2 = close.at[d2, p["code"]] if p["code"] in close.columns else np.nan
            if pd.notna(c2) and p["close"] > 0:
                picks.at[idx, f"ret{h}"] = (c2 / p["close"] - 1) * 100
            t1, t2 = taiex.get(p["date"]), taiex.get(d2)
            if pd.notna(t1) and pd.notna(t2) and t1:
                picks.at[idx, f"mkt{h}"] = (t2 / t1 - 1) * 100
    return picks


def tracking_summary(picks: pd.DataFrame, labels: dict[str, str]) -> dict:
    if picks.empty:
        return {"strategies": [], "recent": [], "first_date": None}
    strategies = []
    for s, g in picks.groupby("strategy"):
        item = {"key": s, "label": labels.get(s, s), "total_picks": int(len(g)), "pick_days": int(g["date"].nunique())}
        for h in HORIZONS:
            done = g.dropna(subset=[f"ret{h}"])
            item[f"n{h}"] = int(len(done))
            if len(done):
                item[f"avg{h}"] = _clean(done[f"ret{h}"].mean())
                item[f"win{h}"] = _clean((done[f"ret{h}"] > 0).mean() * 100)
                item[f"excess{h}"] = _clean((done[f"ret{h}"] - done[f"mkt{h}"]).mean())
            else:
                item[f"avg{h}"] = item[f"win{h}"] = item[f"excess{h}"] = None
        strategies.append(item)
    recent = []
    for (d, s), g in picks.groupby(["date", "strategy"]):
        row = {"date": d, "strategy": labels.get(s, s), "count": int(len(g))}
        for h in HORIZONS:
            done = g.dropna(subset=[f"ret{h}"])
            row[f"avg{h}"] = _clean(done[f"ret{h}"].mean()) if len(done) else None
            row[f"mkt{h}"] = _clean(done[f"mkt{h}"].mean()) if len(done) else None
        recent.append(row)
    recent.sort(key=lambda r: (r["date"], r["strategy"]), reverse=True)
    return {"strategies": strategies, "recent": recent[:30], "first_date": picks["date"].min()}
