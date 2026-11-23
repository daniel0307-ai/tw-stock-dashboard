"""櫃買中心（上櫃）大盤數據：櫃買指數、成交金額、三大法人、融資增減。

只用在儀表板上方的大盤數據（篩選仍只看上市）。任何一項抓不到都不會中斷主流程，
只會在儀表板「注意事項」顯示，Actions 紀錄也會印出實際欄位名稱方便除錯。

上櫃的三大法人與融資只有「個股股數」，金額以「股數 × 當日收盤價」加總估算。
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

from .twse import NoData, _col, _find_table, _tables, http_json, num

BASE = "https://www.tpex.org.tw/www/zh-tw"


def _d(d: date) -> str:
    return d.strftime("%Y/%m/%d")


def _get(path: str, params: dict) -> dict:
    return http_json(f"{BASE}/{path}", {**params, "response": "json"})


def _fields_hint(resp: dict) -> str:
    return " | ".join(",".join(map(str, t["fields"]))[:200] for t in _tables(resp)) or f"stat={resp.get('stat')}"


def _row_date(s) -> date | None:
    m = re.match(r"^\s*(\d{2,4})[/-](\d{1,2})[/-](\d{1,2})", str(s))
    if not m:
        return None
    y = int(m.group(1))
    if y < 1911:
        y += 1911  # 民國年
    return date(y, int(m.group(2)), int(m.group(3)))


def fetch_index(d: date) -> dict:
    resp = _get("indexInfo/inx", {"date": d.strftime("%Y/%m/01")})
    t = _find_table(resp, "日期", "收市")
    if t is None:
        raise NoData(f"櫃買指數格式不符：{_fields_hint(resp)}")
    f = [str(x) for x in t["fields"]]
    ic, ich = _col(f, "收市"), _col(f, "漲")
    for r in t["data"]:
        if _row_date(r[0]) == d:
            close, ch = num(r[ic]), num(r[ich])
            prev = close - ch
            return {"tpex_index": close, "tpex_change": ch,
                    "tpex_change_pct": ch / prev * 100 if prev else None}
    raise NoData(f"櫃買指數 {d} 尚無資料")


def fetch_quotes(d: date) -> pd.DataFrame:
    resp = _get("afterTrading/dailyQuotes", {"date": _d(d), "type": "EW"})
    t = _find_table(resp, "代號", "收盤")
    if t is None:
        raise NoData(f"上櫃行情格式不符：{_fields_hint(resp)}")
    f = [str(x) for x in t["fields"]]
    ic, icl, iv = _col(f, "代號"), _col(f, "收盤"), _col(f, "成交金額")
    return pd.DataFrame([{"code": str(r[ic]).strip(), "close": num(r[icl]), "value": num(r[iv])}
                         for r in t["data"]])


def fetch_institutional(d: date) -> pd.DataFrame:
    """個股三大法人買賣超（股）。"""
    resp = _get("insti/dailyTrade", {"type": "Daily", "sect": "EW", "date": _d(d)})
    t = _find_table(resp, "代號")
    if t is None or not t["data"]:
        raise NoData(f"上櫃三大法人格式不符：{_fields_hint(resp)}")
    f = [str(x) for x in t["fields"]]
    if len(f) == 24 and "三大法人" in f[23]:
        # 外資及陸資合計(買,賣,超)=8-10、投信=11-13、自營商合計=20-22、三大法人合計=23
        i_for, i_tru, i_dea, i_tot = 10, 13, 22, 23
    else:
        i_for = _col(f, "外資及陸資", "買賣超")
        i_tru = _col(f, "投信", "買賣超")
        i_dea = _col(f, "自營商", "買賣超", exclude=("外資", "自行", "避險"))
        i_tot = _col(f, "三大法人")
    ic = _col(f, "代號")
    return pd.DataFrame([{
        "code": str(r[ic]).strip(), "foreign": num(r[i_for]), "trust": num(r[i_tru]),
        "dealer": num(r[i_dea]), "total": num(r[i_tot]),
    } for r in t["data"]])


def fetch_margin(d: date) -> pd.DataFrame:
    """個股融資前日／今日餘額（張）。"""
    resp = _get("margin/balance", {"date": _d(d)})
    t = _find_table(resp, "代號", "資餘額")
    if t is None:
        raise NoData(f"上櫃融資格式不符：{_fields_hint(resp)}")
    f = [str(x) for x in t["fields"]]
    ic = _col(f, "代號")
    ip = _col(f, "前資餘額")
    it = _col(f, "資餘額", exclude=("前",))
    return pd.DataFrame([{"code": str(r[ic]).strip(), "margin_prev": num(r[ip]), "margin_today": num(r[it])}
                         for r in t["data"]])


def market_summary(d: date, log=print) -> tuple[dict, list[str]]:
    """回傳（上櫃大盤數據 dict, 警告訊息）。"""
    out: dict = {}
    warns: list[str] = []
    try:
        out.update(fetch_index(d))
    except Exception as e:  # noqa: BLE001
        warns.append("櫃買指數暫無資料")
        log(f"[櫃買] 指數：{e}")
    close = None
    try:
        q = fetch_quotes(d)
        out["tpex_value"] = float(q["value"].sum())
        close = q.set_index("code")["close"]
    except Exception as e:  # noqa: BLE001
        warns.append("上櫃成交資料暫無資料")
        log(f"[櫃買] 行情：{e}")
    if close is not None:
        try:
            ins = fetch_institutional(d).set_index("code")
            px = close.reindex(ins.index)
            for k in ("foreign", "trust", "dealer", "total"):
                out[f"tpex_inst_{k}"] = float((ins[k] * px).sum())
        except Exception as e:  # noqa: BLE001
            warns.append("上櫃三大法人暫無資料")
            log(f"[櫃買] 三大法人：{e}")
        try:
            m = fetch_margin(d).set_index("code")
            px = close.reindex(m.index)
            out["tpex_margin_chg"] = float(((m["margin_today"] - m["margin_prev"]) * 1000 * px).sum())
            out["tpex_margin_bal"] = float((m["margin_today"] * 1000 * px).sum())
        except Exception as e:  # noqa: BLE001
            warns.append("上櫃融資暫無資料")
            log(f"[櫃買] 融資：{e}")
    return out, warns
