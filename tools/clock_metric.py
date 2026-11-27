# -*- coding: utf-8 -*-
"""[fish-metric 2026-11-19] 量「报时」：她的话里出现钟点的比例。

只统计结构，不打印正文。用法：
  python clock_metric.py            # 全部日期
  python clock_metric.py 2026-11-16 2026-11-19
"""
import json
import os
import re
import sys

RAW = os.environ.get("FISH_RAW_DIR") or "memory/raw"

# 「六点零一」「七点二十三」「六点」「十二点半」「23:10」
CLOCK = re.compile(
    r"(?:\d{1,2}|[一二三四五六七八九十两]{1,3})\s*点\s*(?:半|一刻|三刻|(?:[零一二三四五六七八九十\d]{1,3}))?"
    r"|\d{1,2}\s*[:：]\s*\d{2}"
)


def main():
    days = sys.argv[1:] or sorted(
        n[:-6] for n in os.listdir(RAW) if n.endswith(".jsonl"))
    print(f"{'日期':12s}{'她的话':>7s}{'带钟点':>7s}{'占比':>8s}{'每句平均':>9s}{'开场报时':>9s}")
    for day in days:
        p = os.path.join(RAW, day + ".jsonl")
        if not os.path.exists(p):
            continue
        n = hit = total = opener = 0
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("role") != "assistant":
                continue
            t = str(o.get("text") or "")
            if not t.strip():
                continue
            n += 1
            m = CLOCK.findall(t)
            if m:
                hit += 1
                total += len(m)
            head = re.split(r"[，。！？\n]", t.strip())[0][:24]
            if CLOCK.search(head):
                opener += 1
        if n:
            print(f"{day:12s}{n:>7d}{hit:>7d}{hit/n*100:>7.0f}%{total/max(1,n):>9.2f}{opener:>9d}")


if __name__ == "__main__":
    main()
