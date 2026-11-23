"""FinMind 資料來源：月營收（成長型篩選用）。

- 需要 GitHub Secret「FINMIND_TOKEN」；token 放在 header（Authorization: Bearer）。
- 免費會員只能逐檔查（全市場一次查要 backer 以上），每小時 600 次。
  所以只查「通過流動性門檻且站上 60 日均線」的候選股，並快取到
  docs/data/history/revenue.csv：已有最新一期營收的股票不再查。
- 每次執行最多查 MAX_CALLS 檔；額度用完（HTTP 402）就停，下次排程接著補。
- 公司在每月 10 日前陸續公布上月營收，還沒公布的股票同一天不重查（記在 revenue_checked.csv）。

回傳格式（pandas DataFrame）：
    code      str   股票代號，例如 "2330"
    ym        str   營收年月，例如 "2026-10"
    revenue   float 當月營收（元）
    yoy       float 年增率（%），與去年同月比；缺去年資料時為 NaN
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

from .. import store

API = "https://api.finmindtrade.com/api/v4/data"
CACHE = store.HIST / "revenue.csv"
CHECKED = store.HIST / "revenue_checked.csv"
MAX_CALLS = 250          # 一次執行最多呼叫次數（排程一天兩次，間隔 2 小時，不會超過每小時 600 次）
REQUEST_GAP = 0.3
KEEP_MONTHS = 27
TPE = timezone(timedelta(hours=8))

# 執行後的統計，給主程式寫進 warnings
last_stats: dict = {}


class QuotaExceeded(Exception):
    pass


def token() -> str | None:
    t = os.environ.get("FINMIND_TOKEN", "").strip()
    return t or None


def _ym_add(ym: str, k: int) -> str:
    y, m = map(int, ym.split("-"))
    n = y * 12 + (m - 1) + k
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def expected_latest_ym(today: date) -> str:
    """理論上最新一期已公布的營收月份：10 日（含）以後是上個月，之前保守當作上上個月。"""
    ym = f"{today.year:04d}-{today.month:02d}"
    return _ym_add(ym, -1 if today.day >= 10 else -2)


def fetch_one(code: str, start_date: str) -> pd.DataFrame:
    """查一檔的月營收（測試時會被替換）。回傳欄位 code, ym, revenue。"""
    headers = {"Authorization": f"Bearer {token()}"}
    params = {"dataset": "TaiwanStockMonthRevenue", "data_id": code, "start_date": start_date}
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            r = requests.get(API, params=params, headers=headers, timeout=30)
            if r.status_code == 402:
                raise QuotaExceeded("FinMind 本小時呼叫額度已用完")
            if r.status_code in (401, 403):
                raise RuntimeError(f"FinMind token 無效或權限不足（HTTP {r.status_code}）")
            r.raise_for_status()
            js = r.json()
            if js.get("status") not in (200, None):
                raise RuntimeError(f"FinMind 回應異常：{js.get('msg')}")
            rows = js.get("data") or []
            if not rows:
                return pd.DataFrame(columns=["code", "ym", "revenue"])
            df = pd.DataFrame(rows)
            return pd.DataFrame({
                "code": df["stock_id"].astype(str),
                "ym": [f"{int(y):04d}-{int(m):02d}" for y, m in zip(df["revenue_year"], df["revenue_month"])],
                "revenue": pd.to_numeric(df["revenue"], errors="coerce"),
            })
        except (QuotaExceeded, RuntimeError):
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"FinMind 抓取失敗 {code}: {last_err}")


def _load(path, cols) -> pd.DataFrame:
    if path.exists():
        return pd.read_csv(path, dtype={"code": str, "ym": str, "date": str})
    return pd.DataFrame(columns=cols)


def get_monthly_revenue(codes: list[str], months: int = 15, today: date | None = None) -> pd.DataFrame:
    today = today or datetime.now(TPE).date()
    want = expected_latest_ym(today)
    cache = _load(CACHE, ["code", "ym", "revenue"])
    checked = _load(CHECKED, ["code", "date"])
    checked_today = set(checked.loc[checked["date"] == today.isoformat(), "code"])

    latest = cache.groupby("code")["ym"].max().to_dict() if not cache.empty else {}
    todo = []
    for c in codes:
        have = latest.get(c)
        if have is not None and have >= want:
            continue
        if c in checked_today:
            continue
        todo.append(c)
    # 完全沒快取的優先，其次是快取最舊的
    todo.sort(key=lambda c: latest.get(c, "0000-00"))

    first_ym = _ym_add(want, -(months - 1))
    new_parts, calls, quota_hit, errors = [], 0, False, []
    for c in todo[:MAX_CALLS]:
        have = latest.get(c)
        start_ym = _ym_add(have, 1) if have and have >= first_ym else first_ym
        # FinMind 的 date 是公布月（營收月份的下個月 1 日）
        start = _ym_add(start_ym, 1) + "-01"
        try:
            part = fetch_one(c, start)
        except QuotaExceeded:
            quota_hit = True
            break
        except RuntimeError as e:
            errors.append(str(e))
            if "token" in str(e):
                raise
            continue
        finally:
            calls += 1
            time.sleep(REQUEST_GAP)
        new_parts.append(part)
        checked_today.add(c)

    if new_parts:
        cache = pd.concat([cache] + new_parts, ignore_index=True)
        cache = cache.drop_duplicates(["code", "ym"], keep="last")
    if not cache.empty:
        cutoff = _ym_add(want, -(KEEP_MONTHS - 1))
        cache = cache[cache["ym"] >= cutoff].sort_values(["code", "ym"])
        cache.to_csv(CACHE, index=False)
    pd.DataFrame({"code": sorted(checked_today), "date": today.isoformat()}).to_csv(CHECKED, index=False)

    remaining = max(len(todo) - calls, 0)
    last_stats.clear()
    last_stats.update(calls=calls, remaining=remaining, quota_hit=quota_hit, errors=errors[:3])

    out = cache[cache["code"].isin(codes)].copy()
    if out.empty:
        return pd.DataFrame(columns=["code", "ym", "revenue", "yoy"])
    prev = out.assign(ym=out["ym"].map(lambda x: _ym_add(x, 12)))[["code", "ym", "revenue"]]
    out = out.merge(prev.rename(columns={"revenue": "rev_ly"}), on=["code", "ym"], how="left")
    out["yoy"] = np.where(out["rev_ly"] > 0, (out["revenue"] / out["rev_ly"] - 1) * 100, np.nan)
    return out[["code", "ym", "revenue", "yoy"]]
