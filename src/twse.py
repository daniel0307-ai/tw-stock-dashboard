"""證交所（上市）盤後資料抓取與解析。

使用 www.twse.com.tw 的 rwd JSON 端點（可指定日期，才能補歷史）：
- MI_INDEX  每日收盤行情（全部個股）＋ 大盤指數
- BWIBBU_d  個股本益比、殖利率、股價淨值比
- T86       三大法人買賣超（個股）
- BFI82U    三大法人買賣金額統計（市場合計）

證交所有頻率限制（短時間大量請求會被暫時封鎖 IP），所以每次請求間隔 REQUEST_GAP 秒。
"""
from __future__ import annotations

import re
import time
from datetime import date

import pandas as pd
import requests

BASE = "https://www.twse.com.tw/rwd/zh"
REQUEST_GAP = 3.5
_last_request = 0.0

HEADERS = {
    "User-Agent": "Mozilla/5.0 (tw-stock-dashboard; +https://github.com/)",
    "Accept": "application/json",
}


class NoData(Exception):
    """當天沒有資料（休市或尚未公布）。"""


# ---------------------------------------------------------------- HTTP

def get_json(path: str, params: dict) -> dict:
    return http_json(f"{BASE}/{path}", {**params, "response": "json"})


def http_json(url: str, params: dict) -> dict:
    """GET 並回傳 JSON；含節流與重試（證交所、櫃買共用）。測試時會被替換成假資料。"""
    global _last_request
    last_err: Exception | None = None
    for attempt in range(4):
        wait = REQUEST_GAP - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            _last_request = time.time()
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"抓取失敗 {url} {params}: {last_err}")


def http_post_text(url: str, data: dict, encoding: str = "cp950") -> str:
    """POST 表單並回傳文字（期交所 CSV 下載用，Big5 編碼）。測試時會被替換成假資料。"""
    global _last_request
    last_err: Exception | None = None
    headers = {**HEADERS, "Accept": "text/csv,text/plain,*/*", "Referer": "https://www.taifex.com.tw/",
               "Content-Type": "application/x-www-form-urlencoded"}
    for attempt in range(4):
        wait = REQUEST_GAP - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            _last_request = time.time()
            r = requests.post(url, data=data, headers=headers, timeout=60)
            r.raise_for_status()
            return r.content.decode(encoding, errors="replace")
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"抓取失敗 {url} {data}: {last_err}")


# ---------------------------------------------------------------- 解析工具

def _tables(resp: dict) -> list[dict]:
    """把各種回應格式統一成 [{title, fields, data}]。"""
    if str(resp.get("stat", "OK")).upper() != "OK":  # 證交所回 "OK"、櫃買回 "ok"
        return []
    out = []
    if isinstance(resp.get("tables"), list):
        for t in resp["tables"]:
            if t.get("fields") and t.get("data"):
                out.append({"title": t.get("title", ""), "fields": t["fields"], "data": t["data"]})
    if resp.get("fields") and resp.get("data"):
        out.append({"title": resp.get("title", ""), "fields": resp["fields"], "data": resp["data"]})
    i = 1
    while resp.get(f"fields{i}") is not None:  # 舊格式 fields1/data1 ...
        if resp.get(f"data{i}"):
            out.append({"title": "", "fields": resp[f"fields{i}"], "data": resp[f"data{i}"]})
        i += 1
    return out


def _find_table(resp: dict, *must_have: str) -> dict | None:
    for t in _tables(resp):
        fields = [str(f) for f in t["fields"]]
        if all(any(m in f for f in fields) for m in must_have):
            return t
    return None


def _col(fields: list[str], *keywords: str, exclude: tuple[str, ...] = ()) -> int:
    """找第一個同時包含所有關鍵字、且不含 exclude 的欄位索引。"""
    for i, f in enumerate(fields):
        f = str(f)
        if all(k in f for k in keywords) and not any(x in f for x in exclude):
            return i
    raise KeyError(f"找不到欄位 {keywords}；實際欄位：{fields}")


_TAG = re.compile(r"<[^>]+>")


def num(v) -> float:
    if v is None:
        return float("nan")
    s = _TAG.sub("", str(v)).replace(",", "").strip()
    if s in ("", "--", "---", "-", "N/A", "X"):
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _sign(v) -> int:
    s = _TAG.sub("", str(v))
    return -1 if "-" in s else 1


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


# ---------------------------------------------------------------- 各端點

def fetch_quotes(d: date) -> tuple[pd.DataFrame, dict]:
    """回傳（個股行情 DataFrame, 大盤資訊 dict）。休市丟 NoData。"""
    resp = get_json("afterTrading/MI_INDEX", {"date": ymd(d), "type": "ALLBUT0999"})
    t = _find_table(resp, "證券代號", "收盤價")
    if t is None:
        raise NoData(f"{d} 無行情資料：{resp.get('stat')}")
    f = [str(x) for x in t["fields"]]
    ic, inm = _col(f, "證券代號"), _col(f, "證券名稱")
    io, ih, il, icl = _col(f, "開盤價"), _col(f, "最高價"), _col(f, "最低價"), _col(f, "收盤價")
    iv, ival = _col(f, "成交股數"), _col(f, "成交金額")
    isg, ich = _col(f, "漲跌(+/-)"), _col(f, "漲跌價差")
    rows = []
    for r in t["data"]:
        rows.append({
            "code": str(r[ic]).strip(),
            "name": str(r[inm]).strip(),
            "open": num(r[io]), "high": num(r[ih]), "low": num(r[il]), "close": num(r[icl]),
            "change": _sign(r[isg]) * num(r[ich]),
            "volume": num(r[iv]) / 1000.0,   # 股 → 張
            "value": num(r[ival]),
        })
    quotes = pd.DataFrame(rows)

    market = {"taiex": None, "taiex_change": None, "taiex_change_pct": None, "value": None}
    ti = _find_table(resp, "指數", "收盤指數")
    if ti:
        fi = [str(x) for x in ti["fields"]]
        for r in ti["data"]:
            if str(r[0]).strip() == "發行量加權股價指數":
                market["taiex"] = num(r[_col(fi, "收盤指數")])
                sgn = _sign(r[_col(fi, "漲跌(+/-)")])
                market["taiex_change"] = sgn * num(r[_col(fi, "漲跌點數")])
                market["taiex_change_pct"] = sgn * abs(num(r[_col(fi, "漲跌百分比")]))
                break
    ts = _find_table(resp, "成交統計", "成交金額")
    if ts:
        fs = [str(x) for x in ts["fields"]]
        for r in ts["data"]:
            if "總計" in str(r[0]):
                market["value"] = num(r[_col(fs, "成交金額")])
                break
    if market["value"] is None:
        market["value"] = float(quotes["value"].sum())
    return quotes, market


def fetch_valuation(d: date) -> pd.DataFrame:
    resp = get_json("afterTrading/BWIBBU_d", {"date": ymd(d), "selectType": "ALL"})
    t = _find_table(resp, "證券代號", "本益比")
    if t is None:
        raise NoData(f"{d} 無本益比資料：{resp.get('stat')}")
    f = [str(x) for x in t["fields"]]
    ic, ipe, iy, ipb = _col(f, "證券代號"), _col(f, "本益比"), _col(f, "殖利率"), _col(f, "股價淨值比")
    return pd.DataFrame([{
        "code": str(r[ic]).strip(), "pe": num(r[ipe]), "yield": num(r[iy]), "pb": num(r[ipb]),
    } for r in t["data"]])


def fetch_institutional(d: date) -> pd.DataFrame:
    """個股三大法人買賣超（張）。"""
    resp = get_json("fund/T86", {"date": ymd(d), "selectType": "ALLBUT0999"})
    t = _find_table(resp, "證券代號", "買賣超")
    if t is None:
        raise NoData(f"{d} 無三大法人資料：{resp.get('stat')}")
    f = [str(x) for x in t["fields"]]
    ic = _col(f, "證券代號")
    i_for = _col(f, "外陸資買賣超")  # 「外陸資買賣超股數(不含外資自營商)」
    i_tru = _col(f, "投信買賣超")
    i_dea = _col(f, "自營商買賣超", exclude=("外資", "自行", "避險"))
    i_tot = _col(f, "三大法人買賣超")
    return pd.DataFrame([{
        "code": str(r[ic]).strip(),
        "foreign": num(r[i_for]) / 1000.0,
        "trust": num(r[i_tru]) / 1000.0,
        "dealer": num(r[i_dea]) / 1000.0,
        "total": num(r[i_tot]) / 1000.0,
    } for r in t["data"]])


def fetch_margin(d: date) -> tuple[dict, pd.DataFrame]:
    """融資：回傳（{margin_chg, margin_bal}（元）, 個股融資餘額 DataFrame[code, margin_prev, margin_today]（張））。"""
    resp = get_json("marginTrading/MI_MARGN", {"date": ymd(d), "selectType": "ALL"})
    summary: dict = {}
    ts = _find_table(resp, "項目", "今日餘額")
    if ts:
        f = [str(x) for x in ts["fields"]]
        for r in ts["data"]:
            if "融資金額" in str(r[0]):
                today, prev = num(r[_col(f, "今日餘額")]), num(r[_col(f, "前日餘額")])
                summary = {"margin_chg": (today - prev) * 1000, "margin_bal": today * 1000}  # 仟元 → 元
                break
    t = _find_table(resp, "代號", "今日餘額")
    if t is None:
        if not summary:
            raise NoData(f"{d} 無融資資料：{resp.get('stat')}")
        return summary, pd.DataFrame(columns=["code", "margin_prev", "margin_today"])
    f = [str(x) for x in t["fields"]]
    # 欄位依序是「融資」群組再「融券」群組，第一個「前日餘額／今日餘額」即為融資
    ic, ip, it = _col(f, "代號"), _col(f, "前日餘額"), _col(f, "今日餘額")
    df = pd.DataFrame([{
        "code": str(r[ic]).strip(), "margin_prev": num(r[ip]), "margin_today": num(r[it]),
    } for r in t["data"]])
    return summary, df


def fetch_market_institutional(d: date) -> dict:
    """三大法人市場合計買賣差額（元）。"""
    resp = get_json("fund/BFI82U", {"type": "day", "dayDate": ymd(d)})
    t = _find_table(resp, "單位名稱", "買賣差額")
    if t is None:
        raise NoData(f"{d} 無法人合計資料")
    f = [str(x) for x in t["fields"]]
    idiff = _col(f, "買賣差額")
    out = {"foreign": 0.0, "trust": 0.0, "dealer": 0.0, "total": 0.0}
    for r in t["data"]:
        name, v = str(r[0]), num(r[idiff])
        if name.startswith("外資"):
            out["foreign"] += v
        elif name.startswith("投信"):
            out["trust"] += v
        elif name.startswith("自營商"):
            out["dealer"] += v
        elif name.startswith("合計"):
            out["total"] = v
    return out
