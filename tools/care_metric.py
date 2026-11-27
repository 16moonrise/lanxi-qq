# -*- coding: utf-8 -*-
"""[fish-metric 2026-11-19] 量「照顾议程」：她的话里谈你吃饭睡觉上课的比例。

和 clock_metric.py 配套：一个量报时，一个量照顾。
只数关键词，不打印正文。

用法：
  python care_metric.py                 # 全部日期
  python care_metric.py 2026-11-16 2026-11-19
"""
import json
import os
import re
import sys

RAW = os.environ.get("FISH_RAW_DIR") or "memory/raw"

CARE = re.compile(
    r"饭|吃|饿|粥|面|鸡排|外卖|喝|水|睡|觉|闭眼|起床|醒|课|上班|身体|肚子|拉肚|"
    r"熬夜|通宵|半夜|早点|别拖|洗澡|头发"
)
CLOCK = re.compile(
    r"(?:\d{1,2}|[一二三四五六七八九十两]{1,3})\s*点|"
    r"\d{1,2}\s*[:：]\s*\d{2}|凌晨|半夜"
)


def main():
    days = sys.argv[1:] or sorted(
        n[:-6] for n in os.listdir(RAW) if n.endswith(".jsonl"))
    print(f"{'日期':12s}{'她的话':>7s}{'含照顾词':>9s}{'占比':>8s}{'其中主动':>9s}{'主动含照顾':>10s}")
    for day in days:
        p = os.path.join(RAW, day + ".jsonl")
        if not os.path.exists(p):
            continue
        n = care = ni = carei = 0
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
            if not t.strip() or t.startswith("（我看了"):
                continue
            hit = bool(CARE.search(t))
            inner = o.get("src") == "inner"
            n += 1
            care += hit
            if inner:
                ni += 1
                carei += hit
        if n:
            print(f"{day:12s}{n:>7d}{care:>9d}{care/n*100:>7.0f}%{ni:>9d}{carei:>10d}")


if __name__ == "__main__":
    main()
