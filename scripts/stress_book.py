# -*- coding: utf-8 -*-
"""整本压测：把书架上某本书的所有章节抓一遍，输出耗时分布与失败清单。

    python scripts/stress_book.py <书id> [并发数]

目的不是"能抓"，而是回答四个问题：
  1. 中途会不会被站点限流（连续失败 / 全站 403）
  2. 会不会出现半章（落盘但字数异常短）
  3. 单章耗时分布如何，整本 ETA 是否可接受
  4. 抓完整本后缓存完整性如何

结果写入 out/stress_result.json，进度每 50 章打一行。
"""
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import engine.gateway as gw
from engine.fetcher import LayeredFetcher

BID = sys.argv[1] if len(sys.argv) > 1 else ""
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
SHORT_LIMIT = 300
OUT_DIR = os.path.join(BASE, "out")
LOG = os.path.join(OUT_DIR, "stress.log")
RESULT = os.path.join(OUT_DIR, "stress_result.json")

if not BID:
    print("用法: python scripts/stress_book.py <书id> [并发数]")
    sys.exit(2)

os.makedirs(OUT_DIR, exist_ok=True)
store = gw.BookStore()
book = store.get(BID)
if not book:
    print("书架里没有这本书: %s" % BID)
    sys.exit(1)
chapters = book.get("chapters") or []
N = len(chapters)
print("书名 %s | 章数 %d | 站点 %s | 并发 %d" % (book.get("title"), N, book.get("site"), WORKERS))

fetcher = LayeredFetcher(debug=False, headless=True)
lock = threading.Lock()
done = ok = fail = short = 0
times = []
fails = []
t_start = time.time()


def work(n):
    global done, ok, fail, short
    t0 = time.time()
    err = None
    text = None
    try:
        text = store.fetch_chapter(BID, n, fetcher, prefetch=False)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, e)
    cost = time.time() - t0
    with lock:
        done += 1
        times.append(cost)
        if err:
            fail += 1
            fails.append((n, err))
        elif not text:
            fail += 1
            fails.append((n, "返回空"))
        else:
            ok += 1
            if len(text.strip()) < SHORT_LIMIT:
                short += 1
                fails.append((n, "偏短 %d 字" % len(text.strip())))
        if done % 50 == 0 or done == N:
            el = time.time() - t_start
            avg = el / done
            line = "[%4d/%d] ok=%d fail=%d 短章=%d | 平均 %.2fs 已用 %.1fmin 预计还需 %.1fmin" % (
                done, N, ok, fail, short, avg, el / 60, avg * (N - done) / 60)
            print(line)
            with open(LOG, "a", encoding="utf-8") as fp:
                fp.write("%d/%d ok=%d fail=%d short=%d avg=%.2fs\n" % (done, N, ok, fail, short, avg))


print("开始（%d 并发，接近 Reader 阅读 + 预取的实际量级）" % WORKERS)
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    list(ex.map(work, range(1, N + 1)))

el = time.time() - t_start
times.sort()
res = {
    "book": book.get("title"),
    "bid": BID,
    "site": book.get("site"),
    "chapters": N,
    "ok": ok,
    "fail": fail,
    "short": short,
    "elapsed_min": round(el / 60, 2),
    "avg_s": round(sum(times) / len(times), 2),
    "p50_s": round(times[len(times) // 2], 2),
    "p95_s": round(times[int(len(times) * 0.95)], 2),
    "max_s": round(times[-1], 2),
    "fails": fails[:60],
    "layer_stats": getattr(fetcher, "layer_stats", None),
}
print("\n" + "=" * 60)
print("完成：%d 章，成功 %d，失败 %d，偏短 %d" % (N, ok, fail, short))
print("总用时 %.1f 分钟 | 平均 %.2fs | P50 %.2fs | P95 %.2fs | 最慢 %.2fs"
      % (el / 60, res["avg_s"], res["p50_s"], res["p95_s"], res["max_s"]))
print("分层命中: %s" % (res["layer_stats"],))
for n, e in fails[:15]:
    print("   第%d章 %s" % (n, e))
with open(RESULT, "w", encoding="utf-8") as fp:
    json.dump(res, fp, ensure_ascii=False, indent=2)
print("结果已写入 out/stress_result.json")

miss = bad = 0
for ch in chapters:
    p = gw.cache_path(BID, ch["url"])
    if not os.path.exists(p):
        miss += 1
    else:
        with open(p, encoding="utf-8") as fp:
            if len(fp.read().strip()) < SHORT_LIMIT:
                bad += 1
print("缓存复核：缺 %d 个文件，偏短 %d 个" % (miss, bad))
