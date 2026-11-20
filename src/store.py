"""歷史資料存取：每個交易日一個 CSV，方便 git 追蹤與清理。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data"
HIST = DATA / "history"
PRICES = HIST / "prices"
INST = HIST / "inst"
MARKET = HIST / "market.csv"
PICKS = HIST / "picks.csv"
CONFIG = ROOT / "config"

for p in (PRICES, INST):
    p.mkdir(parents=True, exist_ok=True)

MARKET_COLS = ["date", "taiex", "taiex_change", "taiex_change_pct", "value",
               "inst_foreign", "inst_trust", "inst_dealer", "inst_total"]


def dates_in(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.glob("*.csv"))


def save_day(folder: Path, d: str, df: pd.DataFrame) -> None:
    df.to_csv(folder / f"{d}.csv", index=False)


def load_days(folder: Path, last_n: int | None = None) -> pd.DataFrame:
    ds = dates_in(folder)
    if last_n:
        ds = ds[-last_n:]
    frames = []
    for d in ds:
        df = pd.read_csv(folder / f"{d}.csv", dtype={"code": str})
        df.insert(0, "date", d)
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def trim(folder: Path, keep: int) -> None:
    ds = dates_in(folder)
    for d in ds[:-keep] if len(ds) > keep else []:
        (folder / f"{d}.csv").unlink()


def load_market() -> pd.DataFrame:
    if MARKET.exists():
        return pd.read_csv(MARKET, dtype={"date": str})
    return pd.DataFrame(columns=MARKET_COLS)


def upsert_market(row: dict) -> None:
    m = load_market()
    m = m[m["date"] != row["date"]]
    new = pd.DataFrame([{c: row.get(c) for c in MARKET_COLS}])
    m = new if m.empty else pd.concat([m, new], ignore_index=True)
    m.sort_values("date").to_csv(MARKET, index=False)


HOLIDAYS = HIST / "holidays.txt"


def load_holidays() -> set[str]:
    if HOLIDAYS.exists():
        return {x.strip() for x in HOLIDAYS.read_text().splitlines() if x.strip()}
    return set()


def save_holidays(h: set[str]) -> None:
    HOLIDAYS.write_text("\n".join(sorted(h)) + "\n")


def load_picks() -> pd.DataFrame:
    if PICKS.exists():
        return pd.read_csv(PICKS, dtype={"date": str, "code": str})
    return pd.DataFrame(columns=["date", "strategy", "code", "name", "close"])


def replace_picks(d: str, picks: pd.DataFrame) -> None:
    p = load_picks()
    p = p[p["date"] != d]
    if not picks.empty:
        p = picks if p.empty else pd.concat([p, picks], ignore_index=True)
    p.sort_values(["date", "strategy", "code"]).to_csv(PICKS, index=False)


def load_watchlist() -> pd.DataFrame:
    f = CONFIG / "watchlist.csv"
    if not f.exists():
        return pd.DataFrame(columns=["code", "name", "note"])
    w = pd.read_csv(f, dtype=str).fillna("")
    w = w.rename(columns={"代號": "code", "名稱": "name", "備註": "note"})
    for c in ("code", "name", "note"):
        if c not in w:
            w[c] = ""
    w["code"] = w["code"].str.strip()
    return w[w["code"] != ""][["code", "name", "note"]].drop_duplicates("code")
