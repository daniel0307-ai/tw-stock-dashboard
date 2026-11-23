"""回測資料庫（私有 repo tw-stock-data）每日寫入。

和儀表板主流程分開執行，失敗不影響儀表板。資料一律存「原始值」，
還原股價在回測時才由 events/adjust.parquet 的除權息事件換算，所以舊檔永遠不用改寫。

    python -m src.datalake --dir datalake                # 台北時間今天（＋補最近幾個交易日的缺口）
    python -m src.datalake --dir datalake --date 2026-11-20

目錄（全部 parquet）：
    prices/YYYY-MM/YYYY-MM-DD.parquet   date, code, market, name, open, high, low, close, volume(股), value(元), ex_mark
    inst/...                            date, code, market, foreign, trust, dealer, total（股）
    valuation/...                       date, code, market, pe, yield, pb
    events/adjust.parquet               date(除權息日), code, market, kind, prev_close, ref_price, cash_div, stock_div, source
    已過完的月份會合併成 prices/YYYY-MM.parquet（其他資料夾同理）。

market：twse＝上市、tpex＝上櫃。ex_mark＝當天漲跌欄顯示「X／除權息」等不比價註記（用來檢查有沒有漏掉的事件）。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import store, tpex, twse
from .twse import NoData, _col, _find_table, num

TPE = timezone(timedelta(hours=8))
TABLES = ("prices", "inst", "valuation")
BACKFILL_DAYS = 10          # 每次檢查最近幾個交易日有沒有缺
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"

EVENT_COLS = ["date", "code", "market", "kind", "prev_close", "ref_price", "cash_div", "stock_div", "source"]


def log(*a):
    print("[資料庫]", *a, flush=True)


# ---------------------------------------------------------------- 抓取

def _roc_date(s) -> str | None:
    """'115年11月20日'、'1151120'、'115/11/20'、'2026-11-20' → '2026-11-20'。"""
    s = str(s).strip()
    if re.fullmatch(r"\d{7}", s):
        y, mo, d = int(s[:3]), int(s[3:5]), int(s[5:])
    else:
        m = re.match(r"^(\d{2,4})\D+(\d{1,2})\D+(\d{1,2})", s)
        if not m:
            return None
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:
        y += 1911
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _is_mark(v) -> bool:
    s = twse._TAG.sub("", str(v)).strip()
    return bool(s) and not re.fullmatch(r"[+\-]?[\d.,]*", s)


def twse_day(d: date) -> dict[str, pd.DataFrame]:
    ds = d.isoformat()
    out: dict[str, pd.DataFrame] = {}
    resp = twse.get_json("afterTrading/MI_INDEX", {"date": twse.ymd(d), "type": "ALLBUT0999"})
    t = _find_table(resp, "證券代號", "收盤價")
    if t is None:
        raise NoData(f"{ds} 上市無行情")
    f = [str(x) for x in t["fields"]]
    ic, inm = _col(f, "證券代號"), _col(f, "證券名稱")
    io, ih, il, icl = _col(f, "開盤價"), _col(f, "最高價"), _col(f, "最低價"), _col(f, "收盤價")
    iv, ival, isg = _col(f, "成交股數"), _col(f, "成交金額"), _col(f, "漲跌(+/-)")
    out["prices"] = pd.DataFrame([{
        "date": ds, "code": str(r[ic]).strip(), "market": "twse", "name": str(r[inm]).strip(),
        "open": num(r[io]), "high": num(r[ih]), "low": num(r[il]), "close": num(r[icl]),
        "volume": num(r[iv]), "value": num(r[ival]), "ex_mark": _is_mark(r[isg]),
    } for r in t["data"]])
    try:
        ins = twse.fetch_institutional(d)
        for k in ("foreign", "trust", "dealer", "total"):
            ins[k] = ins[k] * 1000  # 張 → 股
        out["inst"] = ins.assign(date=ds, market="twse")
    except NoData as e:
        log(f"上市法人：{e}")
    try:
        out["valuation"] = twse.fetch_valuation(d).assign(date=ds, market="twse")
    except NoData as e:
        log(f"上市本益比：{e}")
    return out


def tpex_day(d: date) -> dict[str, pd.DataFrame]:
    ds = d.isoformat()
    out: dict[str, pd.DataFrame] = {}
    resp = tpex._get("afterTrading/dailyQuotes", {"date": tpex._d(d), "type": "EW"})
    t = _find_table(resp, "代號", "收盤")
    if t is None:
        raise NoData(f"{ds} 上櫃無行情")
    f = [str(x) for x in t["fields"]]
    ic, inm = _col(f, "代號"), _col(f, "名稱")
    icl, ich = _col(f, "收盤"), _col(f, "漲跌")
    io, ih, il = _col(f, "開盤"), _col(f, "最高"), _col(f, "最低")
    iv, ival = _col(f, "成交股數"), _col(f, "成交金額")
    out["prices"] = pd.DataFrame([{
        "date": ds, "code": str(r[ic]).strip(), "market": "tpex", "name": str(r[inm]).strip(),
        "open": num(r[io]), "high": num(r[ih]), "low": num(r[il]), "close": num(r[icl]),
        "volume": num(r[iv]), "value": num(r[ival]), "ex_mark": _is_mark(r[ich]),
    } for r in t["data"]])
    try:
        out["inst"] = tpex.fetch_institutional(d).assign(date=ds, market="tpex")  # 已是股
    except Exception as e:  # noqa: BLE001
        log(f"上櫃法人：{e}")
    try:
        r = tpex._get("afterTrading/peQryDate", {"date": tpex._d(d)})
        t = _find_table(r, "代號", "本益比")
        if t is None:
            raise NoData(f"格式不符：{tpex._fields_hint(r)}")
        f = [str(x) for x in t["fields"]]
        ic, ipe, iy, ipb = _col(f, "代號"), _col(f, "本益比"), _col(f, "殖利率"), _col(f, "淨值比")
        out["valuation"] = pd.DataFrame([{
            "date": ds, "code": str(x[ic]).strip(), "market": "tpex",
            "pe": num(x[ipe]), "yield": num(x[iy]), "pb": num(x[ipb]),
        } for x in t["data"]])
    except Exception as e:  # noqa: BLE001
        log(f"上櫃本益比：{e}")
    return out


def twse_events(start: date, end: date) -> pd.DataFrame:
    """上市除權除息計算結果表（可查區間）。"""
    resp = twse.get_json("exRight/TWT49U", {"startDate": twse.ymd(start), "endDate": twse.ymd(end)})
    t = _find_table(resp, "股票代號", "參考價")
    if t is None:
        return pd.DataFrame(columns=EVENT_COLS)
    f = [str(x) for x in t["fields"]]
    idt, ic = _col(f, "資料日期"), _col(f, "股票代號")
    ipc, iref = _col(f, "除權息前收盤價"), _col(f, "除權息參考價")
    ival, ikind = _col(f, "權值"), _col(f, "權/息")
    return pd.DataFrame([{
        "date": _roc_date(r[idt]), "code": str(r[ic]).strip(), "market": "twse", "kind": str(r[ikind]).strip(),
        "prev_close": num(r[ipc]), "ref_price": num(r[iref]), "cash_div": float("nan"),
        "stock_div": float("nan"), "source": "TWT49U",
    } for r in t["data"]], columns=EVENT_COLS)


def tpex_events() -> pd.DataFrame:
    """上櫃除權除息計算結果表（OpenAPI，只有最近一批，通常是下一個除權息日）。"""
    rows = twse.http_json(f"{TPEX_OPENAPI}/tpex_exright_daily", {})
    if not isinstance(rows, list):
        return pd.DataFrame(columns=EVENT_COLS)
    return pd.DataFrame([{
        "date": _roc_date(r.get("Date")), "code": str(r.get("SecuritiesCompanyCode", "")).strip(), "market": "tpex",
        "kind": str(r.get("ExRightsDiviend", "")).strip(),
        "prev_close": num(r.get("ClosePriceBeforeExRightsDiviend")), "ref_price": num(r.get("ExRightsDiviendQuote")),
        "cash_div": num(r.get("CashDividend")), "stock_div": num(r.get("StockDividend")), "source": "tpex_exright_daily",
    } for r in rows], columns=EVENT_COLS)


# ---------------------------------------------------------------- 寫入

class Lake:
    def __init__(self, root: Path):
        self.root = root

    def day_path(self, table: str, ds: str) -> Path:
        return self.root / table / ds[:7] / f"{ds}.parquet"

    def has_day(self, table: str, ds: str) -> bool:
        if self.day_path(table, ds).exists():
            return True
        month = self.root / table / f"{ds[:7]}.parquet"
        if month.exists():
            return ds in set(pd.read_parquet(month, columns=["date"])["date"])
        return False

    def write_day(self, table: str, ds: str, df: pd.DataFrame) -> None:
        p = self.day_path(table, ds)
        p.parent.mkdir(parents=True, exist_ok=True)
        lead = ["date", "code", "market"]
        df = df[lead + [c for c in df.columns if c not in lead]]
        df.to_parquet(p, index=False, compression="zstd")

    def upsert_events(self, ev: pd.DataFrame) -> int:
        p = self.root / "events" / "adjust.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        ev = ev.dropna(subset=["date"])
        ev = ev[ev["code"] != ""]
        old = pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=EVENT_COLS)
        n0 = len(old)
        allev = pd.concat([old, ev], ignore_index=True).drop_duplicates(["date", "code", "market"], keep="last")
        allev.sort_values(["date", "code"]).to_parquet(p, index=False, compression="zstd")
        return len(allev) - n0

    def events(self) -> pd.DataFrame:
        p = self.root / "events" / "adjust.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=EVENT_COLS)

    def compact(self, today: date) -> list[str]:
        """把已過完月份的日檔合併成月檔。"""
        done = []
        cur = today.strftime("%Y-%m")
        for table in TABLES:
            base = self.root / table
            if not base.exists():
                continue
            for folder in sorted(x for x in base.iterdir() if x.is_dir() and x.name < cur):
                files = sorted(folder.glob("*.parquet"))
                month = base / f"{folder.name}.parquet"
                parts = [pd.read_parquet(month)] if month.exists() else []
                parts += [pd.read_parquet(f) for f in files]
                if parts:
                    df = pd.concat(parts, ignore_index=True).drop_duplicates(["date", "code", "market"], keep="last")
                    df.sort_values(["date", "code"]).to_parquet(month, index=False, compression="zstd")
                for f in files:
                    f.unlink()
                folder.rmdir()
                done.append(f"{table}/{folder.name}")
        return done


def update_day(lake: Lake, d: date, warnings: list[str]) -> bool:
    """寫入一天的上市＋上櫃資料。回傳是否有行情（False＝休市或尚未公布）。"""
    ds = d.isoformat()
    parts: dict[str, list[pd.DataFrame]] = {t: [] for t in TABLES}
    try:
        for k, v in twse_day(d).items():
            parts[k].append(v)
    except NoData:
        return False
    try:
        for k, v in tpex_day(d).items():
            parts[k].append(v)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"{ds} 上櫃資料抓取失敗：{e}")
    for table, dfs in parts.items():
        got = {m for df in dfs for m in df["market"].unique()}
        for m, label in (("twse", "上市"), ("tpex", "上櫃")):
            if m not in got:
                warnings.append(f"{ds} {label} {table} 無資料")
        if not dfs:
            continue
        lake.write_day(table, ds, pd.concat(dfs, ignore_index=True))
    markets = sorted({m for df in parts["prices"] for m in df["market"].unique()})
    log(f"✓ {ds}：" + "、".join(f"{t} {sum(len(x) for x in parts[t])} 列" for t in TABLES) + f"（{'+'.join(markets)}）")
    return True


def check_marks(lake: Lake, days: list[str], warnings: list[str]) -> None:
    """當天有不比價註記、但事件表沒有對應事件的普通股 → 提醒（可能是減資、分割等尚未收錄的事件）。"""
    ev = lake.events()
    have = set(zip(ev["date"], ev["code"]))
    miss = []
    for ds in days:
        p = lake.day_path("prices", ds)
        if not p.exists():
            continue
        px = pd.read_parquet(p, columns=["code", "ex_mark"])
        for c in px.loc[px["ex_mark"] & px["code"].str.fullmatch(r"[1-9]\d{3}"), "code"]:
            if (ds, c) not in have:
                miss.append(f"{ds} {c}")
    if miss:
        warnings.append("有不比價註記但查無除權息事件（可能是減資／新上市）：" + "、".join(miss[:10])
                        + (f" 等 {len(miss)} 筆" if len(miss) > 10 else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="資料庫 repo 的本機路徑")
    ap.add_argument("--date", help="YYYY-MM-DD，預設為台北時間今天")
    args = ap.parse_args(argv)
    lake = Lake(Path(args.dir))
    today = datetime.now(TPE).date()
    target = date.fromisoformat(args.date) if args.date else today
    warnings: list[str] = []

    # 要處理的交易日：儀表板歷史裡最近 BACKFILL_DAYS 個交易日 ＋ 目標日
    known = [x for x in store.dates_in(store.PRICES) if x <= target.isoformat()]
    days = sorted(set(known[-BACKFILL_DAYS:]) | ({target.isoformat()} if target.weekday() < 5 else set()))
    written = []
    for ds in days:
        if lake.has_day("prices", ds):
            continue
        try:
            if update_day(lake, date.fromisoformat(ds), warnings):
                written.append(ds)
            else:
                log(f"- {ds} 無行情（休市或尚未公布）")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{ds} 寫入失敗：{e}")

    # 除權息事件：上市查最近 30 天（含未來 7 天已公布的計算結果），上櫃取 OpenAPI 最新一批
    try:
        n = lake.upsert_events(twse_events(target - timedelta(days=30), target + timedelta(days=7)))
        log(f"上市除權息事件新增 {n} 筆")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"上市除權息事件抓取失敗：{e}")
    try:
        n = lake.upsert_events(tpex_events())
        log(f"上櫃除權息事件新增 {n} 筆")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"上櫃除權息事件抓取失敗：{e}")

    check_marks(lake, days, warnings)
    for m in lake.compact(today):
        log(f"合併月檔 {m}")
    log(f"完成：新寫入 {len(written)} 天" + (f"（{', '.join(written)}）" if written else ""))
    for w in warnings:
        log("注意：" + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
