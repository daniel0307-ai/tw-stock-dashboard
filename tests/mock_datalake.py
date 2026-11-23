"""用模擬回應測試回測資料庫寫入（不連網）。

    python tests/mock_datalake.py /tmp/lake 2026-11-18 2026-11-19 2026-11-20
先用 tests/mock_run.py 產生儀表板歷史（決定哪些是交易日），再跑這支。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mock_run  # noqa: E402  （會把 twse / tpex 的抓取換成假資料）
from src import datalake, tpex, twse  # noqa: E402

_base = mock_run.fake


def fake(url, params):
    if url.endswith("TWT49U"):
        f = ["資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價", "權值+息值", "權/息", "漲停價格", "跌停價格",
             "開盤競價基準", "減除股利參考價", "詳細資料", "最近一次申報資料 季別/日期", "最近一次申報每股 (單位)淨值", "最近一次申報每股 (單位)盈餘"]
        return {"stat": "OK", "fields": f, "data": [
            ["115年11月19日", "2330", "台積電", "300.00", "295.00", "5.00", "息", "1", "1", "295.00", "295.00", "", "", "", ""],
        ]}
    if url.endswith("tpex_exright_daily"):
        return [{"Date": "1151120", "SecuritiesCompanyCode": "3100", "CompanyName": "櫃買股00",
                 "ClosePriceBeforeExRightsDiviend": "50.00", "ExRightsDiviendQuote": "49.00", "StockDividend": "0.000000",
                 "CashDividend": "1.000000", "ExRightsDiviend": "除息"}]
    if url.endswith("peQryDate"):
        return {"stat": "ok", "tables": [{"fields": ["股票代號", "名稱", "本益比", "每股股利", "殖利率(%)", "股價淨值比"],
                "data": [[c, n, "15.5", "1.0", "3.2", "1.8"] for c, n in mock_run.TPEX_CODES]}]}
    return _base(url, params)


twse.http_json = fake
tpex.http_json = fake

if __name__ == "__main__":
    root = sys.argv[1]
    for ds in sys.argv[2:]:
        datalake.main(["--dir", root, "--date", ds])
