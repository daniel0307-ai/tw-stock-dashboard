"""用模擬的證交所回應跑完整流程（不連網）。

    python tests/mock_run.py 2026-11-17
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import main, twse  # noqa: E402

HOLIDAYS = {"2026-10-09", "2026-10-26"}
END = date(2026, 11, 20)
DAYS = []
d = END
while len(DAYS) < 200:
    if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
        DAYS.append(d)
    d -= timedelta(days=1)
DAYS = DAYS[::-1]
IDX = {x: i for i, x in enumerate(DAYS)}

CODES = [("2330", "台積電"), ("2317", "鴻海"), ("2412", "中華電"), ("0050", "元大台灣50"), ("0056", "元大高股息")]
CODES += [(str(1101 + i * 37), f"測試股{i:02d}") for i in range(60)]
rng = np.random.default_rng(7)
N = len(DAYS)
SER = {}
for k, (c, _) in enumerate(CODES):
    drift = 0.002 if k % 5 == 0 else (-0.001 if k % 7 == 0 else 0.0003)
    px = 30 + rng.random() * 400
    close = px * np.exp(np.cumsum(rng.normal(drift, 0.018, N)))
    vol = rng.lognormal(np.log(800 + 4000 * rng.random()), 0.4, N) * 1000
    if k % 5 == 0:
        vol[-1] *= 2.5
    fr = rng.normal(0, 300, N) * 1000
    tr = rng.normal(0, 80, N) * 1000
    if k % 5 == 0:
        fr[-4:] = np.abs(fr[-4:]) + 50000
    SER[c] = dict(close=np.round(close, 2), vol=vol, fr=fr, tr=tr,
                  pe=(8 + rng.random() * 30) if k % 4 else 11.0,
                  yld=(1 + rng.random() * 5) if k % 4 else 6.2,
                  pb=(0.8 + rng.random() * 3) if k % 4 else 1.1)
TAIEX = 22000 * np.exp(np.cumsum(rng.normal(0.0005, 0.01, N)))

NODATA = {"stat": "很抱歉，沒有符合條件的資料!"}


TPEX_CODES = [(str(3100 + k * 11), f"櫃買股{k:02d}") for k in range(40)]


def path_is_index(url):
    return url.endswith("indexInfo/inx")


def fake_tpex(path, dd, i):
    roc = f"{dd.year - 1911}/{dd.month:02d}/{dd.day:02d}"
    if path.endswith("indexInfo/inx"):
        rows = []
        for x in DAYS:
            if x.year == dd.year and x.month == dd.month:
                j = IDX[x]
                v = 250 + j * 0.3
                rows.append([f"{x.year - 1911}/{x.month:02d}/{x.day:02d}", "1", "1", "1", f"{v:,.2f}", f"{0.3 if j % 3 else -1.2:.2f}"])
        return {"stat": "ok", "tables": [{"title": "櫃買指數", "fields": ["日期", "開市", "最高", "最低", "收市", "漲/跌"], "data": rows}]}
    if path.endswith("dailyQuotes"):
        return {"stat": "ok", "date": roc, "tables": [{"fields": ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數", "成交金額(元)", "成交筆數"],
                "data": [[c, n, f"{50 + k:.2f}", "0.5", "1", "1", "1", "1", "1,000", f"{(k + 1) * 123_456_789:,}", "1"] for k, (c, n) in enumerate(TPEX_CODES)]}]}
    if path.endswith("insti/dailyTrade"):
        f = ["代號", "名稱"] + [f"欄{k}" for k in range(2, 23)] + ["三大法人買賣超股數合計"]
        data = []
        for k, (c, n) in enumerate(TPEX_CODES):
            r = [c, n] + ["0"] * 22
            r[10] = f"{(k - 20) * 10000:,}"; r[13] = f"{(k % 5) * 3000:,}"; r[22] = "-5,000"; r[23] = "0"
            data.append(r)
        return {"stat": "ok", "tables": [{"fields": f, "data": data}]}
    if path.endswith("margin/balance"):
        return {"stat": "ok", "tables": [{"fields": ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金", "資使用率(%)", "資限額", "前券餘額(張)", "券賣", "券買", "券償", "券餘額"],
                "data": [[c, n, "1,000", "1", "1", "1", f"{1000 + (k - 15) * 20:,}", "0", "1", "1", "1", "1", "1", "1", "1"] for k, (c, n) in enumerate(TPEX_CODES)]}]}
    return NODATA


def fake(url, params):
    ds = (params.get("date") or params.get("dayDate")).replace("/", "")
    dd = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
    i = IDX.get(dd)
    if "tpex.org.tw" in url:
        if path_is_index(url):
            return fake_tpex(url, dd, i)
        return NODATA if i is None else fake_tpex(url, dd, i)
    path = url
    if i is None:
        return NODATA
    if path.endswith("MI_INDEX"):
        rows = []
        for c, n in CODES:
            s = SER[c]
            ch = s["close"][i] - s["close"][i - 1]
            sign = '<p style= color:red>+</p>' if ch >= 0 else '<p style= color:green>-</p>'
            rows.append([c, n, f"{s['vol'][i]:,.0f}", "1,000", f"{s['vol'][i]*s['close'][i]:,.0f}",
                         f"{s['close'][i]*0.99:.2f}", f"{s['close'][i]*1.01:.2f}", f"{s['close'][i]*0.98:.2f}",
                         f"{s['close'][i]:.2f}", sign, f"{abs(ch):.2f}", "", "", "", "", "0.00"])
        tch = TAIEX[i] - TAIEX[i - 1]
        return {"stat": "OK", "date": ds, "tables": [
            {"title": "價格指數(臺灣證券交易所)", "fields": ["指數", "收盤指數", "漲跌(+/-)", "漲跌點數", "漲跌百分比(%)", "特殊處理註記"],
             "data": [["發行量加權股價指數", f"{TAIEX[i]:,.2f}", "<p style ='color:red'>+</p>" if tch >= 0 else "<p style ='color:green'>-</p>",
                       f"{abs(tch):.2f}", f"{abs(tch)/TAIEX[i-1]*100:.2f}", ""]]},
            {"title": "大盤統計資訊", "fields": ["成交統計", "成交金額(元)", "成交股數(股)", "成交筆數"],
             "data": [["1.一般股票", "400,000,000,000", "1", "1"], ["總計(1~15)", "452,123,456,789", "1", "1"]]},
            {"title": "每日收盤行情(全部(不含權證、牛熊證))",
             "fields": ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
                        "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"],
             "data": rows},
        ]}
    if path.endswith("BWIBBU_d"):
        return {"stat": "OK", "fields": ["證券代號", "證券名稱", "收盤價", "殖利率(%)", "股利年度", "本益比", "股價淨值比", "財報年/季"],
                "data": [[c, n, "1", f"{SER[c]['yld']:.2f}", "114", f"{SER[c]['pe']:.2f}", f"{SER[c]['pb']:.2f}", "115/3"]
                         for c, n in CODES if not c.startswith("00")]}
    if path.endswith("T86"):
        f = ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
             "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數", "投信買進股數", "投信賣出股數", "投信買賣超股數",
             "自營商買賣超股數", "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
             "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)", "三大法人買賣超股數"]
        data = []
        for c, n in CODES:
            s = SER[c]
            fr, tr = s["fr"][i], s["tr"][i]
            data.append([c, n, "0", "0", f"{fr:,.0f}", "0", "0", "0", "0", "0", f"{tr:,.0f}", "1,000",
                         "0", "0", "0", "0", "0", "0", f"{fr+tr+1000:,.0f}"])
        return {"stat": "OK", "fields": f, "data": data}
    if path.endswith("MI_MARGN"):
        return {"stat": "OK", "tables": [
            {"title": "信用交易統計", "fields": ["項目", "買進", "賣出", "現金(券)償還", "前日餘額", "今日餘額"],
             "data": [["融資(交易單位)", "1", "1", "1", "8,000,000", "7,950,000"], ["融券(交易單位)", "1", "1", "1", "1", "1"],
                      ["融資金額(仟元)", "1", "1", "1", "350,000,000", "342,500,000"]]},
            {"title": "融資融券彙總", "fields": ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
                                              "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"],
             "data": [[c, n, "1", "1", "1", "5,000", f"{5000 + (k % 9 - 4) * 37:,}", "1", "1", "1", "1", "9", "9", "1", "0", ""]
                      for k, (c, n) in enumerate(CODES)]}]}
    if path.endswith("BFI82U"):
        return {"stat": "OK", "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"], "data": [
            ["自營商(自行買賣)", "1", "1", "-1,234,567,890"], ["自營商(避險)", "1", "1", "2,345,678,901"],
            ["投信", "1", "1", "987,654,321"], ["外資及陸資(不含外資自營商)", "1", "1", "-12,345,678,901"],
            ["外資自營商", "1", "1", "0"], ["合計", "1", "1", "-10,246,913,569"]]}
    return NODATA


twse.http_json = fake
from src import tpex as _tp  # noqa: E402
_tp.http_json = fake
if __name__ == "__main__":
    for ds in sys.argv[1:]:
        main.main(["--date", ds])
