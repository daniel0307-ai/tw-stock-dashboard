"""每日排程進入點：抓資料 → 補歷史 → 篩選 → 輸出 docs/data/latest.json。

用法：
    python -m src.main                 # 以台北時間「今天」執行
    python -m src.main --date 2026-11-17
    python -m src.main --latest        # 以最近一個已有資料的交易日重算（儀表板改設定後用，週末也可）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import analysis, store, taifex, tpex, twse

TPE = timezone(timedelta(hours=8))


def log(*a):
    print(*a, flush=True)


def load_cfg() -> dict:
    return json.loads((store.CONFIG / "strategies.json").read_text(encoding="utf-8"))


def save_quotes(d: date, quotes: pd.DataFrame, market: dict) -> None:
    ds = d.isoformat()
    store.save_day(store.PRICES, ds, quotes)
    store.update_market(ds, {k: market.get(k) for k in ("taiex", "taiex_change", "taiex_change_pct", "value")})


def backfill(target: date, cfg: dict, warnings: list[str]) -> None:
    """確保 target 往前 backfill_price_days 個交易日都有資料（首次執行或漏跑時自動補）。"""
    h = cfg["history"]
    have = set(store.dates_in(store.PRICES))
    holidays = store.load_holidays()
    need = h["backfill_price_days"] - 1  # 不含 target 當天
    found, d, tries = 0, target - timedelta(days=1), 0
    while found < need and tries < 200:
        tries += 1
        ds = d.isoformat()
        if d.weekday() < 5 and ds not in holidays:
            if ds in have:
                found += 1
            else:
                try:
                    q, m = twse.fetch_quotes(d)
                    save_quotes(d, q, m)
                    have.add(ds)
                    found += 1
                    log(f"  ✓ 補行情 {ds}")
                except twse.NoData:
                    holidays.add(ds)
                    log(f"  - {ds} 休市")
        d -= timedelta(days=1)
    store.save_holidays(holidays)

    price_dates = [x for x in store.dates_in(store.PRICES) if x < target.isoformat()]
    have_inst = set(store.dates_in(store.INST))
    for ds in price_dates[-h["backfill_inst_days"]:]:
        if ds in have_inst:
            continue
        try:
            store.save_day(store.INST, ds, twse.fetch_institutional(date.fromisoformat(ds)))
            log(f"  ✓ 法人 {ds}")
        except twse.NoData:
            warnings.append(f"{ds} 三大法人資料缺漏")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設為台北時間今天")
    ap.add_argument("--latest", action="store_true", help="用最近一個已有資料的交易日重算")
    args = ap.parse_args(argv)

    cfg = load_cfg()
    if args.date:
        target = date.fromisoformat(args.date)
    elif args.latest and store.dates_in(store.PRICES):
        target = date.fromisoformat(store.dates_in(store.PRICES)[-1])
        log(f"重算模式：使用最近交易日 {target}")
    else:
        target = datetime.now(TPE).date()
    ds = target.isoformat()
    warnings: list[str] = []

    if target.weekday() >= 5:
        log(f"{ds} 是週末，不執行")
        return 0

    # 1. 當日行情（拿不到 = 休市或尚未公布，直接結束，不覆蓋舊資料）
    try:
        quotes, market = twse.fetch_quotes(target)
    except twse.NoData as e:
        log(f"{e}；可能是休市日或資料尚未公布，本次不更新")
        return 0
    save_quotes(target, quotes, market)
    log(f"行情 {ds}：{len(quotes)} 檔")

    # 2. 本益比／殖利率、三大法人
    try:
        valuation = twse.fetch_valuation(target)
    except twse.NoData:
        valuation = pd.DataFrame(columns=["code", "pe", "yield", "pb"])
        warnings.append("今日本益比／殖利率資料尚未公布，價值型篩選暫無結果")
    try:
        store.save_day(store.INST, ds, twse.fetch_institutional(target))
    except twse.NoData:
        warnings.append("今日三大法人個股資料尚未公布，動能型的法人條件以前一日為準")
    try:
        mi = twse.fetch_market_institutional(target)
        store.update_market(ds, {"inst_foreign": mi["foreign"], "inst_trust": mi["trust"],
                                 "inst_dealer": mi["dealer"], "inst_total": mi["total"]})
    except twse.NoData:
        warnings.append("今日三大法人合計資料尚未公布")
    margin = pd.DataFrame(columns=["code", "margin_prev", "margin_today"])
    try:
        msum, margin = twse.fetch_margin(target)
        store.update_market(ds, msum)
    except twse.NoData:
        warnings.append("今日上市融資資料尚未公布")

    # 2b. 上櫃大盤數據（失敗不中斷）
    tp, tw = tpex.market_summary(target, log=log)
    if tp:
        store.update_market(ds, tp)
    warnings.extend(tw)

    # 2c. 期貨（台指期、法人未平倉、P/C 比；失敗不中斷）
    fu, fw = taifex.market_summary(target, market.get("taiex"), log=log)
    if fu:
        store.update_market(ds, fu)
    warnings.extend(fw)

    # 3. 補歷史 & 清理
    backfill(target, cfg, warnings)
    store.trim(store.PRICES, cfg["history"]["price_days"])
    store.trim(store.INST, cfg["history"]["inst_days"])

    # 4. 計算與篩選
    prices = store.load_days(store.PRICES)
    inst = store.load_days(store.INST)
    df = analysis.indicators(prices, inst, quotes, valuation, cfg, margin)
    n_days = prices["date"].nunique()
    if n_days < cfg["growth"]["ma_days"]:
        warnings.append(f"歷史資料目前 {n_days} 個交易日，60 日均線相關數值需滿 60 天才會出現")

    labels = {k: cfg[k]["label"] for k in ("value", "momentum", "growth")}
    results: dict[str, pd.DataFrame] = {}
    status: dict[str, str] = {}
    if cfg["value"].get("enabled", True):
        results["value"] = analysis.screen_value(df, cfg)
    if cfg["momentum"].get("enabled", True):
        results["momentum"] = analysis.screen_momentum(df, cfg)
    results["growth"], status["growth"] = analysis.screen_growth(df, cfg)

    hits: dict[str, list[str]] = {}
    for key, r in results.items():
        for code in r.index:
            hits.setdefault(code, []).append(labels[key])

    # 5. 選股紀錄與成效追蹤
    pick_rows = [{"date": ds, "strategy": k, "code": c, "name": r.at[c, "name"], "close": r.at[c, "close"]}
                 for k, r in results.items() for c in r.index]
    store.replace_picks(ds, pd.DataFrame(pick_rows, columns=["date", "strategy", "code", "name", "close"]))
    market_df = store.load_market()
    picks = analysis.update_tracking(store.load_picks(), prices, market_df)
    picks.to_csv(store.PICKS, index=False)

    # 6. 輸出
    base_cols = ["name", "close", "chg_pct", "volume", "avg_vol20"]
    cols = {
        "value": base_cols + ["pe", "yield", "pb"],
        "momentum": base_cols + ["ma20_dist", "vol_ratio", "foreign_streak", "trust_streak", "foreign5", "trust5"],
        "growth": base_cols + ["ma60_dist", "rev_yoy", "rev_ym"],
    }
    mrow = market_df.set_index("date").loc[ds]
    hist = market_df.tail(60)
    out = {
        "date": ds,
        "generated_at": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        "history_days": int(n_days),
        "warnings": warnings,
        "config": cfg,
        "market": {k: analysis._clean(mrow.get(k)) for k in store.MARKET_COLS if k != "date"},
        "market_history": [{"date": r["date"], "taiex": analysis._clean(r.get("taiex")),
                            "tpex_index": analysis._clean(r.get("tpex_index"))} for _, r in hist.iterrows()],
        "watchlist": analysis.watchlist_rows(store.load_watchlist(), df, hits),
        "groups": store.load_groups(),
        "names": {c: n for c, n in zip(quotes["code"], quotes["name"])},
        "screens": [
            {
                "key": k, "label": labels[k],
                "status": status.get(k, "") if k in results else "已在設定檔關閉",
                "count": int(len(results[k])) if k in results else 0,
                "rows": analysis.records(results[k], cols[k], hits) if k in results else [],
            }
            for k in ("value", "momentum", "growth")
        ],
        "tracking": analysis.tracking_summary(picks, labels),
    }
    (store.DATA / "latest.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    log("完成：" + "、".join(f"{s['label']} {s['count']} 檔" for s in out["screens"]))
    for w in warnings:
        log("注意：" + w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
