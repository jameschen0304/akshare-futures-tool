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

# 与快照中中文名一致 → 100ppi symbol（螺纹钢由 hybrid 单独处理）
PPI_VARIETIES: dict[str, str] = {
    VAR_HC: "HC",
    "线材": "WR",
    "不锈钢": "SS",
    "硅铁": "SF",
    "锰硅": "SM",
    "焦炭": "J",
    "焦煤": "JM",
}

INVENTORY_EM_SYMBOLS: dict[str, str] = {
    VAR_RB: "螺纹钢",
    VAR_HC: "热卷",
    "不锈钢": "不锈钢",
    "硅铁": "硅铁",
    "锰硅": "锰硅",
    "焦炭": "焦炭",
    "焦煤": "焦煤",
}

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


def _rows_from_100ppi_df(sub: pd.DataFrame) -> list[dict]:
    rows = []
    for _, r in sub.iterrows():
        d = pd.Timestamp(r["date"]).strftime("%Y-%m-%d")
        sp = float(r["spot_price"]) if pd.notna(r["spot_price"]) else None
        fp = float(r["dominant_contract_price"]) if pd.notna(r["dominant_contract_price"]) else None
        rows.append({"date": d, "fp": fp, "sp": sp})
    return rows


def rows_from_100ppi(symbol: str, start: str, end: str) -> list[dict]:
    df = ak.futures_spot_price_daily(start_day=start, end_day=end)
    sub = df[df["symbol"] == symbol][["date", "spot_price", "dominant_contract_price"]]
    return _rows_from_100ppi_df(sub)


def fetch_100ppi_batch(start: str, end: str) -> dict[str, list[dict]]:
    """一次请求 100ppi，按 symbol 分组（供日更多品种）。"""
    df = ak.futures_spot_price_daily(start_day=start, end_day=end)
    out: dict[str, list[dict]] = {}
    for symbol, sub in df.groupby("symbol"):
        out[str(symbol)] = _rows_from_100ppi_df(
            sub[["date", "spot_price", "dominant_contract_price"]]
        )
    return out


def refresh_ppi_varieties(
    symbols: dict,
    sources: dict,
    batch: dict[str, list[dict]],
    ppi_map: dict[str, str],
) -> None:
    for cn_name, code in ppi_map.items():
        fresh = batch.get(code) or []
        if not fresh:
            if cn_name in symbols:
                print(f"  警告: 100ppi 近端无 {code}（{cn_name}），沿用快照")
            continue
        symbols[cn_name] = merge_rows(symbols.get(cn_name), fresh)
        sources[cn_name] = "100ppi"
        print(f"  {cn_name} ({code}): 末行 {fresh[-1]['date']}")


def build_inventory_wow_rows(df: pd.DataFrame) -> list[dict]:
    """东财库存序列转标准结构，并计算周环比。"""
    if df is None or df.empty:
        return []
    out = []
    hist: list[tuple[date, float]] = []
    for _, r in df.iterrows():
        ds = pd.Timestamp(r.iloc[0]).strftime("%Y-%m-%d")
        qty = pd.to_numeric(r.iloc[1], errors="coerce")
        if not pd.notna(qty):
            continue
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        qty_val = float(qty)
        out.append({"date": ds, "inventory": round(qty_val, 2), "wow_pct": None})
        hist.append((d, qty_val))

    for row in out:
        d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        prev_target = d.toordinal() - 7
        prev_qty = None
        for hd, hq in reversed(hist):
            if hd.toordinal() <= prev_target:
                prev_qty = hq
                break
        if prev_qty is not None and prev_qty != 0:
            row["wow_pct"] = round((row["inventory"] - prev_qty) / prev_qty * 100, 2)
    return out


def fetch_inventory_em_batch() -> tuple[dict[str, list[dict]], dict[str, str]]:
    inventory: dict[str, list[dict]] = {}
    latest_dates: dict[str, str] = {}
    for cn_name, em_symbol in INVENTORY_EM_SYMBOLS.items():
        try:
            df = ak.futures_inventory_em(symbol=em_symbol)
            rows = build_inventory_wow_rows(df)
            if rows:
                inventory[cn_name] = rows
                latest_dates[cn_name] = rows[-1]["date"]
                print(f"  库存 {cn_name}: 末行 {rows[-1]['date']}")
        except Exception as exc:
            print(f"  警告: 东财库存 {cn_name} 拉取失败 ({exc})")
    # 冷轧板/镀锌板无稳定独立库存口径，继承热卷库存用于表格展示周度库存方向
    hc_inv = inventory.get(VAR_HC)
    if hc_inv:
        for derived in (VAR_CR, VAR_GI):
            inventory[derived] = [dict(x) for x in hc_inv]
            latest_dates[derived] = hc_inv[-1]["date"]
            print(f"  库存 {derived}: 继承{VAR_HC}，末行 {hc_inv[-1]['date']}")
    rb_inv = inventory.get(VAR_RB)
    if rb_inv and "线材" not in inventory:
        inventory["线材"] = [dict(x) for x in rb_inv]
        latest_dates["线材"] = rb_inv[-1]["date"]
        print(f"  库存 线材: 继承{VAR_RB}，末行 {rb_inv[-1]['date']}")
    return inventory, latest_dates


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


def rows_from_rb_hybrid(start: str, end: str, ppi_lookback_days: int | None = None) -> tuple[list[dict], str]:
    """螺纹钢：优先 99qh 全历史，再用 100ppi(RB) 补齐近端交易日（99qh 常滞后数天）。"""
    qh_rows: list[dict] = []
    try:
        qh_rows = rows_from_99qh(VAR_RB)
        print(f"  99qh {VAR_RB}: {len(qh_rows)} 条，末行 {qh_rows[-1]['date'] if qh_rows else '—'}")
    except Exception as exc:
        print(f"  警告: 99qh {VAR_RB} 失败 ({exc})，将仅用 100ppi")
    ppi_start = start
    if ppi_lookback_days and ppi_lookback_days > 0:
        from datetime import timedelta

        end_d = datetime.strptime(end[:10], "%Y-%m-%d").date()
        ppi_start = (end_d - timedelta(days=ppi_lookback_days)).isoformat()
    ppi_rows = rows_from_100ppi("RB", ppi_start, end)
    print(f"  100ppi RB: {len(ppi_rows)} 条，末行 {ppi_rows[-1]['date'] if ppi_rows else '—'}")
    merged = merge_rows(qh_rows, ppi_rows)
    if qh_rows and ppi_rows:
        return merged, "99qh+100ppi"
    if qh_rows:
        return merged, "99qh"
    return merged, "100ppi"


def symbol_latest_dates(symbols: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, rows in symbols.items():
        if isinstance(rows, list) and rows and rows[-1].get("date"):
            out[name] = str(rows[-1]["date"])
    return out


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
    parser.add_argument(
        "--start",
        default="2023-01-01",
        help="100ppi 日度回补起始日（未指定 --lookback-days 时生效）",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=0,
        help="100ppi 仅拉最近 N 个自然日并与快照合并（日更推荐 120）",
    )
    parser.add_argument(
        "--rb-ppi-days",
        type=int,
        default=120,
        help="--refresh-rb 时 100ppi 仅拉最近 N 个自然日（加快更新）",
    )
    parser.add_argument(
        "--coated-only",
        action="store_true",
        help="仅补齐冷轧板/镀锌板（复用快照中已有热卷序列，最快）",
    )
    parser.add_argument(
        "--refresh-rb",
        action="store_true",
        help="刷新螺纹钢（99qh+100ppi 混合，解决仅更新到数日前的问题）",
    )
    args = parser.parse_args()

    data = load_json(args.out)
    symbols: dict = data.setdefault("symbols", {})
    sources: dict = data.setdefault("sources", {})

    print("拉取东财钢铁月度锚点…")
    anchor_dates, em_prices = fetch_em_steel_monthly()

    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ppi_start = args.start
    if args.lookback_days and args.lookback_days > 0:
        from datetime import timedelta

        end_d = datetime.strptime(end[:10], "%Y-%m-%d").date()
        ppi_start = (end_d - timedelta(days=args.lookback_days)).isoformat()

    if args.refresh_rb or not args.coated_only:
        print(f"更新 {VAR_RB}（99qh + 100ppi 混合，{args.start} ~ {end}）…")
        ppi_days = args.rb_ppi_days if args.refresh_rb else None
        symbols[VAR_RB], sources[VAR_RB] = rows_from_rb_hybrid(args.start, end, ppi_days)
    if not args.coated_only:
        print(f"更新 100ppi 全品种（{ppi_start} ~ {end}）…")
        batch = fetch_100ppi_batch(ppi_start, end)
        refresh_ppi_varieties(symbols, sources, batch, PPI_VARIETIES)
    elif not args.refresh_rb:
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
    print("拉取东财库存并计算周环比…")
    inventory_rows, inventory_latest_dates = fetch_inventory_em_batch()

    data["source"] = "99qh primary + 100ppi fallback + EM coated proxy"
    data["generated_at"] = datetime.now(timezone.utc).isoformat()
    data["symbols"] = symbols
    data["sources"] = sources
    data["latest_dates"] = symbol_latest_dates(symbols)
    data["inventory"] = inventory_rows
    data["inventory_latest_dates"] = inventory_latest_dates

    with args.out.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    sample = [r for r in cr_rows if r["date"] == "2026-05-21"]
    sample_g = [r for r in gi_rows if r["date"] == "2026-05-21"]
    print(f"已写入 {args.out}")
    print(f"  品种: {', '.join(sorted(symbols))}")
    for name in sorted(symbols):
        rows = symbols[name]
        if rows:
            print(f"  最新 {name}: {rows[-1]['date']}")
    if sample:
        print(f"  2026-05-21 {VAR_CR} sp={sample[0]['sp']}")
    if sample_g:
        print(f"  2026-05-21 {VAR_GI} sp={sample_g[0]['sp']}")


if __name__ == "__main__":
    main()
