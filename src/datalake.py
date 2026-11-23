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

EVENT_COLS = ["date", "code", "market", "kind", "prev_close", "ref_price", "cash_div", "stock_div",
              "sub_ratio", "sub_price", "ref_source", "source"]
WARRANT = re.compile(r"^[1-9]\d{5}$")   # 6 碼且非 00 開頭＝權證，不存


def _drop_warrants(df: pd.DataFrame) -> pd.DataFrame:
    return df[~df["code"].str.match(WARRANT)].reset_index(drop=True)


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
    t = twse._TAG.sub("", str(v)).strip()
    return "X" in t.upper() or bool(re.search(r"[\u4e00-\u9fff]", t))


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
    out["prices"] = _drop_warrants(out["prices"])
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
    out["prices"] = _drop_warrants(out["prices"])
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


def _ev(**kw) -> dict:
    row = {c: float("nan") for c in EVENT_COLS}
    row.update(kw)
    return row


def twse_events(start: date, end: date) -> pd.DataFrame:
    """上市除權除息計算結果表（官方參考價，可查區間）。"""
    resp = twse.get_json("exRight/TWT49U", {"startDate": twse.ymd(start), "endDate": twse.ymd(end)})
    t = _find_table(resp, "股票代號", "參考價")
    if t is None:
        return pd.DataFrame(columns=EVENT_COLS)
    f = [str(x) for x in t["fields"]]
    idt, ic = _col(f, "資料日期"), _col(f, "股票代號")
    ipc, iref, ikind = _col(f, "除權息前收盤價"), _col(f, "除權息參考價"), _col(f, "權/息")
    return pd.DataFrame([_ev(
        date=_roc_date(r[idt]), code=str(r[ic]).strip(), market="twse", kind=str(r[ikind]).strip(),
        prev_close=num(r[ipc]), ref_price=num(r[iref]), ref_source="official", source="TWT49U",
    ) for r in t["data"]], columns=EVENT_COLS)


def twse_notice() -> pd.DataFrame:
    """上市除權除息預告表（OpenAPI，含近期已除與即將除權息；只有股利，參考價由程式計算）。"""
    rows = twse.http_json("https://openapi.twse.com.tw/v1/exchangeReport/TWT48U_ALL", {})
    return pd.DataFrame([_ev(
        date=_roc_date(r.get("Date")), code=str(r.get("Code", "")).strip(), market="twse",
        kind=str(r.get("Exdividend", "")).strip(), cash_div=num(r.get("CashDividend")),
        stock_div=num(r.get("StockDividendRatio")), sub_ratio=num(r.get("SubscriptionRatio")),
        sub_price=num(r.get("SubscriptionPricePerShare")), source="TWT48U",
    ) for r in (rows if isinstance(rows, list) else [])], columns=EVENT_COLS)


def tpex_events() -> pd.DataFrame:
    """上櫃除權除息計算結果表（OpenAPI，只有最近一批）。"""
    rows = twse.http_json(f"{TPEX_OPENAPI}/tpex_exright_daily", {})
    return pd.DataFrame([_ev(
        date=_roc_date(r.get("Date")), code=str(r.get("SecuritiesCompanyCode", "")).strip(), market="tpex",
        kind=str(r.get("ExRightsDiviend", "")).strip(),
        prev_close=num(r.get("ClosePriceBeforeExRightsDiviend")), ref_price=num(r.get("ExRightsDiviendQuote")),
        cash_div=num(r.get("CashDividend")), stock_div=num(r.get("StockDividend")),
        ref_source="official", source="tpex_exright_daily",
    ) for r in (rows if isinstance(rows, list) else [])], columns=EVENT_COLS)


def tpex_notice() -> pd.DataFrame:
    """上櫃除權除息預告表（OpenAPI，含近期與即將除權息）。"""
    rows = twse.http_json(f"{TPEX_OPENAPI}/tpex_exright_prepost", {})
    return pd.DataFrame([_ev(
        date=_roc_date(r.get("ExRrightsExDividendDate")), code=str(r.get("SecuritiesCompanyCode", "")).strip(),
        market="tpex", kind=str(r.get("ExRrightsExDividend", "")).strip(), cash_div=num(r.get("CashDividend")),
        stock_div=num(r.get("StockDividendRatio")), sub_ratio=num(r.get("SubscriptionRatioToNewSharesIssued")),
        sub_price=num(r.get("SubscriptionPricePerShare")), source="tpex_exright_prepost",
    ) for r in (rows if isinstance(rows, list) else [])], columns=EVENT_COLS)


def calc_ref(prev_close, cash, stock, sub_ratio, sub_price) -> float:
    """除權息參考價 =（前日收盤 − 現金股利 ＋ 現增認購價 × 現增比率）÷（1 ＋ 無償配股率 ＋ 現增比率）。"""
    z = lambda v: 0.0 if v is None or pd.isna(v) else float(v)  # noqa: E731
    if prev_close is None or pd.isna(prev_close) or prev_close <= 0:
        return float("nan")
    return (prev_close - z(cash) + z(sub_price) * z(sub_ratio)) / (1 + z(stock) + z(sub_ratio))


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

    def needs_rebuild(self, ds: str) -> bool:
        """早期版本的日檔含權證、不比價註記判斷也較寬，偵測到就重抓一次。"""
        p = self.day_path("prices", ds)
        if not p.exists():
            return False
        return bool(pd.read_parquet(p, columns=["code"])["code"].str.match(WARRANT).any())

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
        ev = ev[(ev["code"] != "") & ~ev["code"].str.match(WARRANT)]
        old = self.events()
        n0 = len(old)
        key = ["date", "code", "market"]
        allev = pd.concat([old, ev], ignore_index=True)
        # 官方計算結果排前面，groupby.first() 會優先取到它的參考價，股利欄再由預告表補
        allev["_rank"] = (allev["ref_source"] != "official").astype(int)
        allev = allev.sort_values(key + ["_rank"]).drop(columns="_rank")
        merged = allev.groupby(key, as_index=False).first()
        srcs = allev.groupby(key)["source"].apply(lambda x: "+".join(sorted(set("+".join(x.dropna()).split("+")) - {""})))
        merged["source"] = merged.set_index(key).index.map(srcs)
        merged[EVENT_COLS].sort_values(key).to_parquet(p, index=False, compression="zstd")
        return len(merged) - n0

    def fill_refs(self) -> int:
        """沒有官方參考價的事件：等除權息日前一天的收盤價寫進資料庫後，用公式算出參考價。"""
        ev = self.events()
        need = ev["ref_price"].isna()
        if not need.any():
            return 0
        n = 0
        for i in ev.index[need]:
            ds, code = ev.at[i, "date"], ev.at[i, "code"]
            if not self.has_day("prices", ds):   # 除權息日還沒到（或還沒寫入）就先不算
                continue
            prev = self.prev_close(code, ds)
            if prev is None:
                continue
            ev.at[i, "prev_close"] = prev
            ev.at[i, "ref_price"] = calc_ref(prev, ev.at[i, "cash_div"], ev.at[i, "stock_div"],
                                             ev.at[i, "sub_ratio"], ev.at[i, "sub_price"])
            ev.at[i, "ref_source"] = "computed"
            n += 1
        ev.to_parquet(self.root / "events" / "adjust.parquet", index=False, compression="zstd")
        return n

    def prev_close(self, code: str, ds: str) -> float | None:
        """ds 之前最後一個有收盤價的交易日收盤（只往前找 10 天）。"""
        d = date.fromisoformat(ds)
        for k in range(1, 11):
            x = (d - timedelta(days=k)).isoformat()
            p = self.day_path("prices", x)
            month = self.root / "prices" / f"{x[:7]}.parquet"
            if p.exists():
                df = pd.read_parquet(p, columns=["code", "close"])
            elif month.exists():
                df = pd.read_parquet(month, columns=["date", "code", "close"])
                df = df[df["date"] == x]
            else:
                continue
            if df.empty:
                continue
            v = df.loc[df["code"] == code, "close"]
            return float(v.iloc[0]) if len(v) and pd.notna(v.iloc[0]) else None
        return None

    def events(self) -> pd.DataFrame:
        p = self.root / "events" / "adjust.parquet"
        if not p.exists():
            return pd.DataFrame(columns=EVENT_COLS)
        ev = pd.read_parquet(p)
        for c in EVENT_COLS:
            if c not in ev.columns:
                ev[c] = float("nan")
        # 舊版沒有 ref_source：官方計算結果表來的參考價補標 official
        ev["ref_source"] = ev["ref_source"].astype(object)
        old = ev["ref_source"].isna() & ev["ref_price"].notna() & ev["source"].astype(str).str.contains("TWT49U|exright_daily")
        ev.loc[old, "ref_source"] = "official"
        return ev[EVENT_COLS]

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
        if lake.has_day("prices", ds) and not lake.needs_rebuild(ds):
            continue
        try:
            if update_day(lake, date.fromisoformat(ds), warnings):
                written.append(ds)
            else:
                log(f"- {ds} 無行情（休市或尚未公布）")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{ds} 寫入失敗：{e}")

    # 除權息事件：官方計算結果（有參考價）＋預告表（有股利，參考價自己算），四個來源互補
    sources = [
        ("上市除權息計算結果", lambda: twse_events(target - timedelta(days=30), target + timedelta(days=7))),
        ("上市除權息預告", twse_notice),
        ("上櫃除權息計算結果", tpex_events),
        ("上櫃除權息預告", tpex_notice),
    ]
    for label, fn in sources:
        try:
            df = fn()
            n = lake.upsert_events(df)
            log(f"{label}：{len(df)} 筆，新增事件 {n} 筆")
        except Exception as e:  # noqa: BLE001
            warnings.append(f"{label}抓取失敗：{e}")
    try:
        log(f"計算參考價 {lake.fill_refs()} 筆")
    except Exception as e:  # noqa: BLE001
        warnings.append(f"計算參考價失敗：{e}")

    check_marks(lake, days, warnings)
    for m in lake.compact(today):
        log(f"合併月檔 {m}")
    log(f"完成：新寫入 {len(written)} 天" + (f"（{', '.join(written)}）" if written else ""))
    for w in warnings:
        log("注意：" + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
