"""FinMind 資料來源（預留）。

目前尚未實作。註冊 FinMind 並把 token 設為 GitHub Secret「FINMIND_TOKEN」後，
實作 get_monthly_revenue() 即可啟用成長型篩選，其他程式不用改。

預期回傳格式（pandas DataFrame）：
    code      str   股票代號，例如 "2330"
    ym        str   營收年月，例如 "2026-10"
    revenue   float 當月營收（元）
    yoy       float 年增率（%）

建議做法：dataset=TaiwanStockMonthRevenue，只查「通過流動性門檻的股票」以節省呼叫次數，
並把結果快取到 docs/data/history/revenue.csv（月營收一個月只更新一次，不必每天全抓）。
"""
from __future__ import annotations

import os

import pandas as pd

API = "https://api.finmindtrade.com/api/v4/data"


def token() -> str | None:
    t = os.environ.get("FINMIND_TOKEN", "").strip()
    return t or None


def get_monthly_revenue(codes: list[str], months: int = 15) -> pd.DataFrame:
    raise NotImplementedError("FinMind 月營收尚未實作")
