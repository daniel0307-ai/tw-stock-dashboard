"""期交所大盤數據：台指期／小台／微台行情、期現貨價差、三大法人台指期淨未平倉、
選擇權 P/C 比，以及小台、微台的散戶多空比。

期交所只提供 CSV 下載（POST 表單、Big5 編碼）。任何一項抓不到都不會中斷主流程，
只會在儀表板「注意事項」顯示，Actions 紀錄也會印出實際內容開頭方便除錯。

散戶多空比 ＝ −（三大法人多空淨額未平倉口數）÷ 全市場未平倉量 × 100%
（全市場未平倉量＝該商品一般交易時段所有單式合約的未沖銷契約數加總；正值＝散戶偏多）
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta

from .twse import NoData, http_post_text, num

BASE = "https://www.taifex.com.tw/cht/3"
_DATE = re.compile(r"^\s*(\d{4})/(\d{1,2})/(\d{1,2})")
LOOKBACK = 14  # 往前抓幾天，用來算「較前一交易日」

# (欄位前綴, 行情代碼 futDataDown, 法人代碼 futContractsDateDown, 顯示名稱)
PRODUCTS = [("fut", "TX", "TXF", "台指期"), ("mtx", "MTX", "MXF", "小台"), ("tmf", "TMF", "TMF", "微台")]


def _rows(text: str) -> list[list[str]]:
    return [r for r in csv.reader(io.StringIO(text.lstrip("﻿"))) if r]


def _row_date(s: str) -> date | None:
    m = _DATE.match(s or "")
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _hint(text: str) -> str:
    return text[:160].replace("\n", " | ")


def _form(start: date, end: date, **extra) -> dict:
    return {"down_type": "1", "queryStartDate": start.strftime("%Y/%m/%d"),
            "queryEndDate": end.strftime("%Y/%m/%d"), **extra}


def fut_daily(code: str, d: date) -> dict[date, dict]:
    """某期貨商品近 LOOKBACK 天每日：近月合約行情（一般時段）＋全市場未平倉量。"""
    text = http_post_text(f"{BASE}/futDataDown", _form(d - timedelta(days=LOOKBACK), d, commodity_id=code))
    rows = _rows(text)
    if len(rows) < 2:
        raise NoData(f"{code} 行情格式不符：{_hint(text)}")
    head = [h.strip() for h in rows[0]]

    def ix(key, fb):
        return next((i for i, h in enumerate(head) if key in h), fb)
    i_m, i_close, i_chg, i_pct = ix("到期月份", 2), ix("收盤價", 6), ix("漲跌價", 7), ix("漲跌%", 8)
    i_oi, i_sess = ix("未沖銷", 11), ix("交易時段", 17)
    days: dict[date, dict] = {}
    for r in rows[1:]:
        dd = _row_date(r[0])
        if dd is None or len(r) <= max(i_m, i_close):
            continue
        month = r[i_m].strip()
        sess = r[i_sess].strip() if len(r) > i_sess else "一般"
        if "/" in month or sess not in ("一般", ""):
            continue  # 排除價差單與盤後時段
        day = days.setdefault(dd, {"oi_total": 0.0, "near": None})
        oi = num(r[i_oi]) if len(r) > i_oi else float("nan")
        if oi == oi:
            day["oi_total"] += oi
        if re.fullmatch(r"\d{6}", month) and (day["near"] is None or month < day["near"][0]):  # 近月＝最近的月契約
            pct = r[i_pct].replace("%", "") if len(r) > i_pct else ""
            day["near"] = (month, {"month": month, "close": num(r[i_close]), "change": num(r[i_chg]), "change_pct": num(pct)})
    if d not in days or days[d]["near"] is None:
        raise NoData(f"{code} {d} 無近月資料：{_hint(text)}")
    return days


def inst_oi(code: str, d: date) -> dict[date, dict[str, float]]:
    """某期貨商品近 LOOKBACK 天三大法人多空淨額未平倉口數（外資／投信／自營商）。"""
    text = http_post_text(f"{BASE}/futContractsDateDown", _form(d - timedelta(days=LOOKBACK), d, commodityId=code))
    by_day: dict[date, dict[str, float]] = {}
    for r in _rows(text):
        dd = _row_date(r[0])
        if dd is None or len(r) < 15:
            continue
        who = r[2]
        key = "foreign" if "外資" in who else "trust" if "投信" in who else "dealer" if "自營" in who else None
        if key:
            by_day.setdefault(dd, {})[key] = num(r[13])  # 第 13 欄：多空淨額未平倉口數
    if d not in by_day:
        raise NoData(f"{code} 三大法人 {d} 無資料：{_hint(text)}")
    return by_day


def _prev(days: dict, d: date):
    p = [x for x in sorted(days) if x < d]
    return p[-1] if p else None


def fetch_pc_ratio(d: date) -> dict:
    """台指選擇權買賣權未平倉量比率（%）。"""
    text = http_post_text(f"{BASE}/pcRatioDown", _form(d - timedelta(days=LOOKBACK), d))
    by_day = {}
    for r in _rows(text):
        dd = _row_date(r[0])
        if dd is not None and len(r) >= 7:
            by_day[dd] = num(r[6])  # 第 6 欄：買賣權未平倉量比率%
    if d not in by_day:
        raise NoData(f"P/C 比 {d} 無資料：{_hint(text)}")
    pd_ = _prev(by_day, d)
    return {"pc_ratio": by_day[d], "pc_ratio_chg": by_day[d] - by_day[pd_] if pd_ else None}


def _retail_ratio(daily: dict, inst: dict, day: date) -> tuple[float | None, float | None]:
    """回傳（散戶多空比 %, 散戶淨口數）。"""
    if day not in daily or day not in inst:
        return None, None
    oi = daily[day]["oi_total"]
    net_inst = sum(inst[day].get(k, 0.0) for k in ("foreign", "trust", "dealer"))
    if not oi:
        return None, None
    return -net_inst / oi * 100, -net_inst


def market_summary(d: date, taiex: float | None, log=print) -> tuple[dict, list[str]]:
    out: dict = {}
    warns: list[str] = []
    for pre, qcode, icode, label in PRODUCTS:
        daily = inst = None
        try:
            daily = fut_daily(qcode, d)
            near = daily[d]["near"][1]
            out.update({f"{pre}_month": near["month"], f"{pre}_close": near["close"],
                        f"{pre}_change": near["change"], f"{pre}_change_pct": near["change_pct"],
                        f"{pre}_oi_total": daily[d]["oi_total"]})
        except Exception as e:  # noqa: BLE001
            warns.append(f"{label}行情暫無資料")
            log(f"[期貨] {label}行情：{e}")
        try:
            inst = inst_oi(icode, d)
            if pre == "fut":  # 台指期：顯示三大法人各自的淨未平倉
                p = _prev(inst, d)
                for k in ("foreign", "trust", "dealer"):
                    v = inst[d].get(k)
                    out[f"fut_{k}_oi"] = v
                    pv = inst[p].get(k) if p else None
                    out[f"fut_{k}_oi_chg"] = v - pv if v is not None and pv is not None else None
        except Exception as e:  # noqa: BLE001
            warns.append(f"{label}三大法人未平倉暫無資料")
            log(f"[期貨] {label}三大法人：{e}")
        if pre != "fut" and daily and inst:  # 小台、微台：散戶多空比
            r, net = _retail_ratio(daily, inst, d)
            p = _prev({k: v for k, v in daily.items() if k in inst}, d)
            pr, _ = _retail_ratio(daily, inst, p) if p else (None, None)
            out[f"{pre}_retail_ratio"] = r
            out[f"{pre}_retail_net"] = net
            out[f"{pre}_retail_ratio_chg"] = r - pr if r is not None and pr is not None else None
    try:
        out.update(fetch_pc_ratio(d))
    except Exception as e:  # noqa: BLE001
        warns.append("選擇權 P/C 比暫無資料")
        log(f"[期貨] P/C 比：{e}")
    if out.get("fut_close") is not None and taiex:
        out["fut_basis"] = out["fut_close"] - taiex  # 正值＝正價差
    return out, warns
