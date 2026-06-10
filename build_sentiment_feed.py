# -*- coding: utf-8 -*-
"""
抓取舆情快讯并写入 sentiment_feed.json，供静态页「舆情窗口」读取。

数据源（akshare 1.8+ 已移除 js_news，此处直连金十 flash API）：
  - 金十快讯：flash-api.jin10.com（等同原 js_news 最新资讯）
  - 上海金属网快讯：ak.futures_news_shmet
  - 上期所库存周报：ak.futures_stock_shfe_js（最近有效周五）

用法:
  python build_sentiment_feed.py
  python build_sentiment_feed.py --out sentiment_feed.json
"""

from __future__ import annotations

import argparse
import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "sentiment_feed.json"

JIN10_HEADERS = {
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "x-app-id": "rU6QIu7JHe2gOUeR",
    "x-csrf-token": "x-csrf-token",
    "x-version": "1.0.0",
    "referer": "https://www.jin10.com/",
}

STEEL_KEYWORDS = (
    "螺纹", "热卷", "热轧", "冷轧", "镀锌", "钢铁", "钢材", "铁矿", "焦炭", "焦煤",
    "双焦", "钢坯", "高炉", "去库", "累库", "SHFE", "上期所", "黑色",
)


def strip_html(text: str) -> str:
    if not text:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def tag_sentiment(text: str) -> str:
    """轻量关键词倾向：bull / bear / neutral"""
    if not text:
        return "neutral"
    bull = ("上涨", "反弹", "走强", "去库", "限产", "涨价", "拉升", "利多", "超预期", "扩产受限")
    bear = ("下跌", "走弱", "累库", "下调", "利空", "暴跌", "下挫", "承压", "减产", "过剩")
    score = 0
    for w in bull:
        if w in text:
            score += 1
    for w in bear:
        if w in text:
            score -= 1
    if score > 0:
        return "bull"
    if score < 0:
        return "bear"
    return "neutral"


def is_steel_related(text: str) -> bool:
    return any(k in text for k in STEEL_KEYWORDS)


def fetch_jin10_flash(pages: int = 8) -> list[dict]:
    """金十快讯（原 ak.js_news indicator=最新资讯）。"""
    url = "https://flash-api.jin10.com/get_flash_list"
    out: list[dict] = []
    seen: set[str] = set()
    max_time = ""
    for _ in range(pages):
        params = {"channel": "-8200", "vip": 1, "max_time": max_time}
        r = requests.get(url, headers=JIN10_HEADERS, params=params, timeout=20)
        r.raise_for_status()
        batch = (r.json() or {}).get("data") or []
        if not batch:
            break
        for item in batch:
            iid = str(item.get("id") or "")
            if iid in seen:
                continue
            seen.add(iid)
            payload = item.get("data") or {}
            content = strip_html(str(payload.get("content") or payload.get("title") or ""))
            if not content:
                continue
            dt = str(item.get("time") or "")
            out.append(
                {
                    "datetime": dt,
                    "content": content,
                    "important": int(item.get("important") or 0),
                    "source": "jin10",
                    "sentiment": tag_sentiment(content),
                    "steel": is_steel_related(content),
                }
            )
        max_time = str(batch[-1].get("time") or "")
        if not max_time:
            break
    out.sort(key=lambda x: x["datetime"], reverse=True)
    return out


def fetch_shmet_flash(symbol: str = "全部") -> list[dict]:
    df = ak.futures_news_shmet(symbol=symbol)
    out: list[dict] = []
    for _, row in df.iterrows():
        dt = row.iloc[0]
        if hasattr(dt, "strftime"):
            dt_text = pd.Timestamp(dt).tz_convert("Asia/Shanghai").strftime("%Y-%m-%d %H:%M:%S")
        else:
            dt_text = str(dt)
        content = str(row.iloc[1] or "").strip()
        if not content:
            continue
        out.append(
            {
                "datetime": dt_text,
                "content": content,
                "important": 0,
                "source": "shmet",
                "sentiment": tag_sentiment(content),
                "steel": is_steel_related(content),
            }
        )
    out.sort(key=lambda x: x["datetime"], reverse=True)
    return out


def last_fridays(count: int = 6) -> list[str]:
    d = datetime.now(timezone.utc).date()
    fridays: list[str] = []
    while len(fridays) < count:
        if d.weekday() == 4:
            fridays.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return fridays


def fetch_shfe_weekly_stock() -> dict | None:
    """上期所指定交割仓库库存周报（金十）。"""
    for ds in last_fridays(8):
        try:
            df = ak.futures_stock_shfe_js(date=ds)
            if df is None or df.empty:
                continue
            rows = []
            for _, r in df.iterrows():
                row = {str(c): (None if pd.isna(v) else v) for c, v in zip(df.columns, r)}
                rows.append(row)
            return {
                "date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}",
                "date_raw": ds,
                "columns": [str(c) for c in df.columns],
                "rows": rows,
            }
        except Exception as exc:
            print(f"  警告: 库存周报 {ds} 失败 ({exc})")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 sentiment_feed.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--jin10-pages", type=int, default=8, help="金十翻页次数")
    args = parser.parse_args()

    print("拉取金十快讯…")
    jin10 = fetch_jin10_flash(pages=args.jin10_pages)
    print(f"  金十 {len(jin10)} 条，末条 {jin10[0]['datetime'] if jin10 else '—'}")

    print("拉取上海金属网快讯…")
    shmet = fetch_shmet_flash("全部")
    print(f"  SHMET {len(shmet)} 条")

    print("拉取上期所库存周报…")
    weekly_stock = fetch_shfe_weekly_stock()
    if weekly_stock:
        print(f"  库存周报 {weekly_stock['date']}，{len(weekly_stock['rows'])} 行")
    else:
        print("  库存周报暂无可用数据（接口可能空返回）")

    steel_news = [x for x in jin10 + shmet if x.get("steel")]
    steel_news.sort(key=lambda x: x["datetime"], reverse=True)

    data = {
        "source": "jin10 flash + shmet + shfe weekly stock",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "jin10": jin10,
        "shmet": shmet,
        "steel_highlight": steel_news[:80],
        "weekly_stock": weekly_stock,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
