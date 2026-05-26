# -*- coding: utf-8 -*-
"""
补齐钢材现货快照：在 99qh/100ppi 日度序列基础上，用东财「现货与股票-钢铁」
月度锚点推导冷轧板、镀锌板日度 sp（无期货合约则 fp=null）。

用法:
  python build_steel_spot_daily.py
  python build_steel_spot_daily.py --out steel_spot_daily.json
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import akshare as ak
import pandas as pd
import requests
from akshare.utils import demjson

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "steel_spot_daily.json"

# 与 index.html 中 resolve*SteelVariety 命名一致
VAR_CR = "冷轧板"
VAR_GI = "镀锌板"
VAR_HC = "热轧卷板"
VAR_RB = "螺纹钢"

EM_STEEL = {
    VAR_HC: "热轧板卷",
    VAR_CR: "冷轧板",
    VAR_GI: "镀锌板",
}


def fetch_em_steel_monthly() -> tuple[list[date], dict[str, list[float | None]]]:
    """东财钢铁板块：5 个月末价 + 最新价。"""
    url = "https://data.eastmoney.com/ifdata/xhgp.html"
    headers = {"User-Agent": "Mozilla/5.0"}
    text = requests.get(url, headers=headers, timeout=30).text
    chunk = (
        text[text.find("pagedata") : text.find("/newstatic/js/common/emdataview.js")]
        .strip("pagedata= ")
        .strip(';\n        </script>\n        <script src="')
    )
    payload = demjson.decode(chunk)
    labels = [payload["dates"][f"d{i}"] for i in range(1, 6)]
    steel = payload["datas"][5]["list"]
    by_name = {item["name"]: item for item in steel}

    as_of = datetime.now(timezone.utc).date()
    anchor_dates: list[date] = []
    for md in labels:
        m, d = (int(x) for x in md.split("-"))
        y = as_of.year
        if m > as_of.month:
            y -= 1
        anchor_dates.append(date(y, m, d))
    anchor_dates.append(as_of)

    prices: dict[str, list[float | None]] = {k: [] for k in EM_STEEL}
    for var, em_name in EM_STEEL.items():
        row = by_name.get(em_name)
        if not row:
            raise RuntimeError(f"东财钢铁板块缺少「{em_name}」")
        vals = [row.get(f"v{i}") for i in range(1, 6)] + [row.get("price")]
        prices[var] = [float(v) if v not in (None, "", "-") else None for v in vals]

    return anchor_dates, prices


def interpolate_spread(daily_dates: list[str], anchor_dates: list[date], anchor_spreads: list[float]) -> dict[str, float]:
    """按日期线性插值价差锚点。"""
    xs = [datetime.combine(d, datetime.min.time()).timestamp() for d in anchor_dates]
    ys = anchor_spreads
    out: dict[str, float] = {}
    for ds in daily_dates:
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        ts = datetime.combine(d, datetime.min.time()).timestamp()
        if ts <= xs[0]:
            out[ds] = ys[0]
        elif ts >= xs[-1]:
            out[ds] = ys[-1]
        else:
            for i in range(len(xs) - 1):
                if xs[i] <= ts <= xs[i + 1]:
                    t = (ts - xs[i]) / (xs[i + 1] - xs[i]) if xs[i + 1] != xs[i] else 0.0
                    out[ds] = ys[i] + t * (ys[i + 1] - ys[i])
                    break
    return out


def rows_from_100ppi(symbol: str, start: str, end: str) -> list[dict]:
    df = ak.futures_spot_price_daily(start_day=start, end_day=end)
    sub = df[df["symbol"] == symbol][["date", "spot_price", "dominant_contract_price"]]
    rows = []
    for _, r in sub.iterrows():
        d = pd.Timestamp(r["date"]).strftime("%Y-%m-%d")
        sp = float(r["spot_price"]) if pd.notna(r["spot_price"]) else None
        fp = float(r["dominant_contract_price"]) if pd.notna(r["dominant_contract_price"]) else None
        rows.append({"date": d, "fp": fp, "sp": sp})
    return rows


def rows_from_99qh(symbol: str) -> list[dict]:
    df = ak.spot_price_qh(symbol=symbol)
    rows = []
    for _, r in df.iterrows():
        d = pd.Timestamp(r["日期"]).strftime("%Y-%m-%d")
        fp = float(r["期货收盘价"]) if pd.notna(r["期货收盘价"]) else None
        sp = float(r["现货价格"]) if pd.notna(r["现货价格"]) else None
        rows.append({"date": d, "fp": fp, "sp": sp})
    return rows


def merge_rows(existing: list[dict] | None, fresh: list[dict]) -> list[dict]:
    by_date = {r["date"]: r for r in (existing or []) if r.get("date")}
    for r in fresh:
        by_date[r["date"]] = r
    return [by_date[k] for k in sorted(by_date)]


def build_coated_varieties(hc_rows: list[dict], anchor_dates: list[date], em_prices: dict[str, list[float | None]]) -> tuple[list[dict], list[dict]]:
    """由热卷日度 sp + 东财冷热/镀冷月度价差锚点，生成冷轧板、镀锌板日度 sp。"""
    daily_dates = [r["date"] for r in hc_rows if r.get("sp") is not None]
    if len(daily_dates) < 2:
        raise RuntimeError("热轧卷板日度 sp 不足，无法推导冷轧/镀锌")

    def em_spreads(prod: str, raw: str) -> list[float]:
        p = em_prices[prod]
        r = em_prices[raw]
        spreads = []
        for a, b in zip(p, r):
            if a is None or b is None:
                spreads.append(spreads[-1] if spreads else 0.0)
            else:
                spreads.append(a - b)
        return spreads

    spread_cr_hr = interpolate_spread(daily_dates, anchor_dates, em_spreads(VAR_CR, VAR_HC))
    spread_gi_cr = interpolate_spread(daily_dates, anchor_dates, em_spreads(VAR_GI, VAR_CR))

    cr_rows: list[dict] = []
    gi_rows: list[dict] = []
    for row in hc_rows:
        d = row["date"]
        hr_sp = row.get("sp")
        if hr_sp is None or d not in spread_cr_hr:
            cr_rows.append({"date": d, "fp": None, "sp": None})
            gi_rows.append({"date": d, "fp": None, "sp": None})
            continue
        cr_sp = hr_sp + spread_cr_hr[d]
        gi_sp = cr_sp + spread_gi_cr[d]
        cr_rows.append({"date": d, "fp": None, "sp": round(cr_sp, 2)})
        gi_rows.append({"date": d, "fp": None, "sp": round(gi_sp, 2)})
    return cr_rows, gi_rows


def load_json(path: Path) -> dict:
    if path.is_file():
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    return {"symbols": {}, "sources": {}}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成/更新 steel_spot_daily.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--start", default="2023-01-01", help="100ppi 日度回补起始日")
    parser.add_argument(
        "--coated-only",
        action="store_true",
        help="仅补齐冷轧板/镀锌板（复用快照中已有热卷序列，最快）",
    )
    args = parser.parse_args()

    data = load_json(args.out)
    symbols: dict = data.setdefault("symbols", {})
    sources: dict = data.setdefault("sources", {})

    print("拉取东财钢铁月度锚点…")
    anchor_dates, em_prices = fetch_em_steel_monthly()

    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not args.coated_only:
        print(f"更新 100ppi 日度 {VAR_HC} ({args.start} ~ {end})…")
        symbols[VAR_HC] = merge_rows(symbols.get(VAR_HC), rows_from_100ppi("HC", args.start, end))
        sources[VAR_HC] = "100ppi"
        print(f"更新 99qh {VAR_RB}…")
        try:
            symbols[VAR_RB] = merge_rows(symbols.get(VAR_RB), rows_from_99qh(VAR_RB))
            sources[VAR_RB] = "99qh"
        except Exception as exc:
            print(f"  警告: 99qh 螺纹钢失败，保留已有数据 ({exc})")
    else:
        print(f"跳过期货日度回补（--coated-only），沿用快照 {VAR_HC}")

    hc_rows = symbols.get(VAR_HC) or []
    if not hc_rows:
        raise SystemExit(f"快照中无 {VAR_HC} 序列，请先提供 steel_spot_daily.json 或去掉 --coated-only")
    print(f"推导 {VAR_CR} / {VAR_GI}（东财价差 + 热卷日度）…")
    cr_rows, gi_rows = build_coated_varieties(hc_rows, anchor_dates, em_prices)
    symbols[VAR_CR] = cr_rows
    symbols[VAR_GI] = gi_rows
    sources[VAR_CR] = "eastmoney_spread+100ppi_hc"
    sources[VAR_GI] = "eastmoney_spread+100ppi_hc"

    data["source"] = "99qh primary + 100ppi fallback + EM coated proxy"
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["symbols"] = symbols
    data["sources"] = sources

    with args.out.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    sample = [r for r in cr_rows if r["date"] == "2026-05-21"]
    sample_g = [r for r in gi_rows if r["date"] == "2026-05-21"]
    print(f"已写入 {args.out}")
    print(f"  品种: {', '.join(sorted(symbols))}")
    if sample:
        print(f"  2026-05-21 {VAR_CR} sp={sample[0]['sp']}")
    if sample_g:
        print(f"  2026-05-21 {VAR_GI} sp={sample_g[0]['sp']}")


if __name__ == "__main__":
    main()
