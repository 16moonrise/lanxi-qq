# -*- coding: utf-8 -*-
"""[fish-meter 2026-11-18] 她的本地账本：读 usage/*.jsonl，出日/用途报表。

数据来自 venv 里 openai_source.py 的 `_fish_meter_usage`（每次真实请求一条）。
只做估算——平台「按 API Key 查看用量」才是账单真相；本地误差主要来自
缓存命中率与峰谷判定。价目表见下，官方调价时改这里。

用法：
  python usage_report.py                # 最近 7 天
  python usage_report.py --days 1       # 只看今天
  python usage_report.py --day 2026-11-18
  python usage_report.py --json         # 机器可读
"""
import argparse
import json
import os
import time
from collections import defaultdict

USAGE_DIR = os.environ.get("FISH_USAGE_DIR") or "memory/usage"

# 元 / 百万 token（谷时价）。峰时 ×2，见 PEAK_MULT。
PRICES = {
    "flash": {"hit": 0.02, "miss": 1.0, "out": 4.0},
    "pro": {"hit": 1.0, "miss": 12.0, "out": 24.0},
}
PEAK_MULT = 2.0
# 谷时：北京时间 00:30–08:30（其余为峰时）
OFFPEAK_START = (0, 30)
OFFPEAK_END = (8, 30)


def _offpeak(ts: int) -> bool:
    t = time.localtime(ts)
    cur = t.tm_hour * 60 + t.tm_min
    a = OFFPEAK_START[0] * 60 + OFFPEAK_START[1]
    b = OFFPEAK_END[0] * 60 + OFFPEAK_END[1]
    return a <= cur < b


def _kind(model: str) -> str:
    m = (model or "").lower()
    return "pro" if "pro" in m else "flash"


def _cost(rec: dict) -> float:
    p = PRICES[_kind(rec.get("model", ""))]
    base = (
        int(rec.get("in_cached") or 0) * p["hit"]
        + int(rec.get("in_other") or 0) * p["miss"]
        + int(rec.get("out") or 0) * p["out"]
    ) / 1e6
    return base * (1.0 if _offpeak(int(rec.get("ts") or 0)) else PEAK_MULT)


def load(days: int | None = None, day: str | None = None):
    recs = []
    if not os.path.isdir(USAGE_DIR):
        return recs
    for name in sorted(os.listdir(USAGE_DIR)):
        if not name.endswith(".jsonl"):
            continue
        d = name[:-6]
        if day and d != day:
            continue
        try:
            for line in open(os.path.join(USAGE_DIR, name), encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                o["day"] = d
                recs.append(o)
        except Exception:
            continue
    if days:
        keep = {(time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)))
                for i in range(days)}
        recs = [r for r in recs if r["day"] in keep]
    return recs


def fmt_rows(recs, key="day"):
    g = defaultdict(lambda: {"n": 0, "hit": 0, "miss": 0, "out": 0, "cost": 0.0})
    for r in recs:
        k = r.get(key) or "-"
        x = g[k]
        x["n"] += 1
        x["hit"] += int(r.get("in_cached") or 0)
        x["miss"] += int(r.get("in_other") or 0)
        x["out"] += int(r.get("out") or 0)
        x["cost"] += _cost(r)
    return g


def print_table(g, title):
    print(f"\n{title}")
    print(f"{'':10s}{'调用':>6s}{'输入命中':>12s}{'输入未命中':>12s}{'输出':>10s}{'估算':>10s}")
    tot = {"n": 0, "hit": 0, "miss": 0, "out": 0, "cost": 0.0}
    for k in sorted(g, reverse=True) if len(str(list(g)[0] if g else "")) == 10 else sorted(g):
        x = g[k]
        for f in tot:
            tot[f] += x[f]
        print(f"{k:10s}{x['n']:>6d}{x['hit']:>12,d}{x['miss']:>12,d}{x['out']:>10,d}"
              f"{'￥%.4f' % x['cost']:>10s}")
    print(f"{'合计':10s}{tot['n']:>6d}{tot['hit']:>12,d}{tot['miss']:>12,d}{tot['out']:>10,d}"
          f"{'￥%.4f' % tot['cost']:>10s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--day", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if not os.path.isdir(USAGE_DIR):
        print(f"还没有账本目录：{USAGE_DIR}")
        return
    recs = load(days=None if a.day else a.days, day=a.day or None)
    if not recs:
        print(f"{a.day or '最近 %d 天' % a.days}：还没有记录")
        return
    if a.json:
        byday = fmt_rows(recs, "day")
        bytag = fmt_rows(recs, "tag")
        print(json.dumps({"days": byday, "tags": bytag,
                          "total_cost": round(sum(_cost(r) for r in recs), 6)},
                         ensure_ascii=False, indent=1))
        return
    print_table(fmt_rows(recs, "day"), "按天")
    print_table(fmt_rows(recs, "tag"), "按用途")


if __name__ == "__main__":
    main()
