# bookhub 优化建议

> 对象：当前仓库 `main`（提交 `420f544`）
> 方法：通读 `server.py` / `engine/*.py` / `autobs/autobs.py`，关键结论均在本机实跑验证，不靠推测
> 原则：每条都写「问题 → 证据 → 影响 → 改法 → 怎么验证」，改完能对照验收

---

## 0. 摘要

| # | 优先级 | 问题 | 影响 | 预估 |
|---|---|---|---|---|
| 1 | P0 | 网关路径的种子沉淀是死代码 | 从 Reader 打开的书永不参与沉淀 | 10 分钟 |
| 2 | P0 | 章节缓存非原子写 | 半截缓存永久生效，章节再也抓不回来 | 15 分钟 |
| 3 | P0 | 半章正文被当完整章节缓存 | ddkshu 类分页站永久少一半字 | 30 分钟 |
| 4 | P1 | 全局 0.6s 节流把 10 站并发压成串行 | 搜索被锁在 ~1.6 req/s | 30 分钟 |
| 5 | P1 | L3 浏览器单页面 + 全局锁 | 并发过防护时排队 25s+ | 2 小时 |
| 6 | P1 | 体检逐本串行 | 书越多越慢，无并发 | 40 分钟 |
| 7 | P1 | 章节缓存无全局配额 | `data/cache` 只增不减 | 1 小时 |
| 8 | P2 | `JOBS` 字典只增不减 | 长跑进程内存缓慢上涨 | 20 分钟 |
| 9 | P2 | `autobs` 靠 sys.path hack 当模块导入 | 能跑纯属 import 顺序巧合 | 1 小时 |
| 10 | P2 | 体检重复实现网关的缓存键算法 | 缓存键一改就静默错位 | 20 分钟 |
| 11 | P3 | 水印规则 `^\d{1,4}$` 误杀面 | 正文里的数字行被删 | 40 分钟 |
| 12 | P3 | 结果硬上限 60 条 | 加站只分散来源不增加总数 | 20 分钟 |
| 13 | P3 | 搜索 45s 总超时与单站 40s 耦合 | 一个卡死站点吃掉全部预算 | 1 小时 |
| 14 | P3 | P0 三条无单测覆盖 | 修完无法防回归 | 1 小时 |

**建议批次**：A（1+2+3+14，半天）→ B（4+6+8，一天）→ C（9+10+5，两天）→ D（11+12+13+7，打磨）。

---

## 1. 网关路径的种子沉淀是死代码

**问题**
`BookStore._try_sediment()` 用裸模块名导入，但 `search` / `fetcher` 是 `engine` 包内的模块，根目录不在它们的父路径上。

**证据**
`engine/gateway.py:238-250`：

```python
def _try_sediment(self, bid):
    try:
        from search import maybe_add_seed
        from fetcher import LayeredFetcher
        ...
    except Exception:
        pass
```

在本机实跑（`import server` 之后，即完整启动上下文）：

```
search FAIL: No module named 'search'
fetcher FAIL: No module named 'fetcher'
```

异常被 `except Exception: pass` 吞掉，日志里一个字都没有。

**影响**
从 Reader 打开的书走 `ensure_resolved` → 后台线程 → 第一步就抛 ImportError → 直接返回。
**这条链路从未生效**，只有网页台解析（走 `server.py` 的 `_run_resolve_job`）才会沉淀种子。搜索页看到的「自动沉淀」对 Reader 用户是空的。

**改法**

```python
def _try_sediment(self, bid):
    try:
        from engine.search import maybe_add_seed
        from engine.fetcher import LayeredFetcher
    except ImportError:                      # 兼容非包方式运行
        from search import maybe_add_seed
        from fetcher import LayeredFetcher
    ...
```

同时把外层 `except Exception: pass` 改成打印一行（`print("[seed] 沉淀失败: %r" % e)` 即可）。同文件 `_bg_refresh`（`engine/gateway.py:195-200`）已经是 `try engine.fetcher / except ImportError: from fetcher` 的写法——**同一文件两种风格，说明这里是遗漏，不是设计**。

**验证**
`python -c "import server; from engine.search import maybe_add_seed; print('ok')"`，再补一条单测：monkeypatch `maybe_add_seed` 返回假种子，断言 `_try_sediment` 调用了它。

---

## 2. 章节缓存非原子写

**问题**
正文落盘是「打开 → 写 → 关」，中途进程被杀、磁盘满、编码异常，都会留下半截文件；而读取端只要文件存在就直接返回，永不重抓。

**证据**
`engine/gateway.py:310-314`：

```python
text = _filter_domain_watermark(text, ch["url"])
try:
    open(cpath, "w", encoding="utf-8").write(text)
except Exception:
    pass
```

读取端 `engine/gateway.py:266-276`：`if os.path.exists(cpath): ... return text`——存在即权威。

**影响**
一次 Ctrl+C、一次断电、一次磁盘满，那一章就永久损坏。用户看到的是「这章只有几百字」，且点「刷新目录」也不会修（缓存键是 URL 哈希，URL 没变）。

**改法**

```python
tmp = cpath + ".tmp"
with open(tmp, "w", encoding="utf-8") as fp:
    fp.write(text)
os.replace(tmp, cpath)          # 同分区原子替换
```

读取端补一道损坏兜底：读到的文本 < 50 字（或长度比上次记录的少一半）就删文件重抓。`os.replace` 在 Windows 同卷上是原子的，已有先例——`_atomic_write_json` 就是这么写的。

**验证**
写缓存时 `taskkill /F` 掉 python，重启后该章仍能重新抓取并落盘。

---

## 3. 半章正文被当完整章节缓存

**问题**
正文分页循环里任何一页失败就 `break`，但**已经收集到的部分照常拼成 text 落缓存**，只要总字数 ≥50 就算成功。

**证据**
`engine/gateway.py:285-309`：

```python
while cu and hops < 8:
    rr = fetcher.get(cu, referer=b.get("list_url"))
    if rr is None:
        break                      # ← 第 2 页 403，循环退出
    ...
    parts.append(extract_content_text(got[0]))
...
text = "\n".join(p for p in parts if p)
if len(text) < 50:
    return None
text = _filter_domain_watermark(text, ch["url"])
```

第 1 页成功、第 2 页失败 → `parts` 只有半章 → `len(text)` 通常 700~1200 字，远超 50 → **半章被当作完整章节永久缓存**。

**影响**
ddkshu（9.html / 9_2.html）、速读谷（「1 / 9」多页章）这类站点，只要某一页偶发失败，那一章就永久只有一半。CHANGELOG 里「第 100 章 700 字 → 1385 字」的修复只解决了「规则识别」，没解决「失败时的降级」。

**改法**
区分「正常结束」与「异常中断」：

```python
interrupted = False
while cu and hops < 8:
    rr = fetcher.get(cu, referer=b.get("list_url"))
    if rr is None:
        interrupted = True
        break
    ...
if interrupted:
    return None            # 不缓存，下次重试（代价是重复抓，收益是自愈）
```

或折中：中断时写 `cpath + ".part"`，下次读取时优先拼 `.part` + 新抓内容。

**验证**
mock `fetcher.get` 让第 2 页返回 `None`，断言 `fetch_chapter` 返回 `None` 且缓存目录无该章文件。

---

## 4. 全局 0.6s 节流把 10 站并发压成串行

**问题**
节流用的是**实例级**时间戳，不区分站点。搜索时 10 个种子站同时提交，每个请求都要排队过同一个 0.6 秒闸门。

**证据**
`engine/fetcher.py:151-155`：

```python
def _throttle(self):
    dt = time.time() - self.last
    if dt < 0.6:
        time.sleep(0.6 - dt)
    self.last = time.time()
```

调用点 `engine/fetcher.py:198`（`_req` 的非 quick 路径，即搜索、目录、正文全走这里）。

**算一下**：10 站 × 0.6s ≈ **6.0s**，而 CHANGELOG 记录的「遮天 6.0s / 60 条」正好是这个数——60 条 = 10 站 × 6 条，耗时 = 10 × 0.6s。节流就是当前的硬瓶颈，站点本身的响应时间被完全掩盖。

**改法**

```python
self._last_by_host = {}

def _throttle(self, url):
    host = self._host_of(url)
    now = time.time()
    last = self._last_by_host.get(host, 0.0)
    if now - last < 0.6:
        time.sleep(0.6 - (now - last))
    self._last_by_host[host] = time.time()
```

语义从「全局限速」变成「每站限速」：对任何单个站点仍然礼貌（同站 ≥0.6s），跨站不再互相拖累。`_host_state`（熔断）已经是按 host 的，节流按 host 是同一套思路，只是当初漏了。

**验证**
搜「遮天」连测三次，p50 应从 6.0s 降到 ≈2s（取决于最慢那个站）；结果条数应保持不变（60 条）。

---

## 5. L3 浏览器是单页面 + 全局锁

**问题**
`BrowserLayer` 全进程只有一个 `ChromiumPage`，`get()` 从头到尾持锁：`page.get()`（最长 25s）+ 等待 JS 挑战（最长 10s）。并发场景下第二个请求要等第一个跑完。

**证据**
`engine/fetcher.py:44`（`self._lock = threading.Lock()`）、`engine/fetcher.py:72-83`：

```python
def get(self, url, timeout=25):
    with self._lock:
        page = self._ensure()
        page.get(url, timeout=timeout)
        deadline = time.time() + 10
        while time.time() < deadline:
            ...
```

**影响**
搜索降级到 L3 时（Bing 反爬、百度兜底），8 个候选会严格排队。最坏 8 × 35s = 280s，被 `as_completed(timeout=45)` 一刀切掉，等于白跑。

**改法**
- 短期：L3 只保留给「必须过防护」的单点调用，预验阶段显式 `force_layer=None` + 禁止 L3；
- 中期：对象池 2~3 个 tab（DrissionPage 支持 `browser.new_tab()`），每个 tab 一把锁；
- 无论哪种，`get()` 都要支持 `timeout` 快速失败，不要用 10s 硬等待兜底。

**验证**
构造 4 个同时需要 L3 的 URL，并发调用，总耗时应从 ≈4× 单次降到 ≈1.5×。

---

## 6. 体检逐本串行

**问题**
`/api/healthcheck` 用一个 `for` 循环遍历书架，每本做 1 次探活 + 最多 3 章快抓，全部串行。

**证据**
`server.py:522` 起：

```python
for i, b in enumerate(books):
    r = fetcher.get(b.get("list_url") or b.get("url"), quick=True)   # 8s 超时
    ...
    for idx in sorted({0, n // 2, n - 1}):
        text = _quick_chapter(b2, ch, fetcher)
```

单本最坏 1×8s + 3×8s = 32s。5 本书就是 160s，网页台看着像卡死。

**改法**

```python
with ThreadPoolExecutor(max_workers=4) as ex:
    futs = {ex.submit(_check_one, b): b for b in books}
    for fut in as_completed(futs):
        entry = fut.result()
        report.append(entry)
        job["progress"] = int(100 * len(report) / len(books))
```

注意：`quick=True` 不走 `_throttle`，并发不会互相限速；但 4 并发对目标站仍是可接受压力。

**验证**
5 本书体检从 160s 级降到 ≤40s，且 `data/health.json` 内容结构不变。

---

## 7. 章节缓存无全局配额

**问题**
`data/cache` 只有「按书清理」（`clear_cache`），没有全局大小控制，也没有 LRU。

**证据**
`engine/gateway.py` 的 `clear_cache()` 只处理单本；`server.py` 的 `/api/cache/{bid}` 同理。

**实测**：当前 96 个章节文件 = 0.82MB，约 **8.5KB/章**。一本 1859 章的书全缓存 ≈ 16MB，20 本 ≈ 320MB。长期使用会持续膨胀。

**改法**
- `/api/cache` 增加全局统计（总大小、每本大小、最后访问时间）；
- 启动时或每日检查：超过阈值（建议 500MB）按「最后访问时间最旧」整本清理，保留书架与进度；
- 清理前在网页台给一条可撤销提示。

**验证**
手动把阈值调到 1MB，重启后应自动清理最旧的书，`data/books/*.json` 不受影响。

---

## 8. `JOBS` 字典只增不减

**问题**
后台任务记录永不清理，`log` 列表随任务累积（txt 导出会写很多行）。

**证据**
`server.py:218-227`：`JOBS = {}`、`JOBS[jid] = {...}`，全文件没有 `del JOBS[...]` 或过期清理。

**影响**
长跑进程（比如挂一整天）内存缓慢上涨。单条记录不大，但属于「迟早要还」的债。

**改法**
任务完成时记 `finished_at`；`_new_job` 里顺手清理「完成超过 30 分钟」或「总数超过 20 条」的旧记录。

**验证**
连续触发 30 次解析任务，`len(JOBS)` 不超过 20。

---

## 9. `autobs` 靠 sys.path hack 当模块导入

**问题**
`autobs/` 目录没有 `__init__.py`，但代码把它当**模块文件**导入：先把 `autobs/` 目录塞进 `sys.path`，再 `from autobs import ...`，实际命中的是 `autobs/autobs.py`。

**证据**
- `engine/fetcher.py:15-16`：`sys.path.insert(0, .../"autobs")` + `from autobs import UA, is_waf_page`
- `engine/gateway.py:20-21`：同样一段，`from autobs import (parse_doc, norm_url, ...)`
- `server.py:15`：又插了一个 `.../".."/"autobs"`——**这个目录不存在**，是死代码
- `server.py:26`：`from autobs import resolve_book` 之所以能成功，**纯粹因为第 23 行先导入了 `engine.fetcher`**，它已经把路径塞好了

**证据（实跑）**
单独执行 `python -c "from autobs import resolve_book"` → `ImportError: cannot import name 'resolve_book' from 'autobs' (unknown location)`；
先 `import server` 再执行同样的语句 → 成功。**导入顺序是隐式契约**，谁调整一下 import 排序就会炸。

**改法**
二选一，都只改一次：
1. 给 `autobs/` 加 `__init__.py`，内容 `from .autobs import *`（需在 `autobs.py` 里补 `__all__`）；
2. 把 `autobs/autobs.py` 挪成 `engine/autobs_core.py`，全仓库改一处 import。

顺手删掉 `server.py:15` 那条指向不存在目录的 `sys.path.insert`。

**验证**
`python -c "from autobs import resolve_book"`（不带 server 上下文）能成功；`pytest tests/ -q` 全绿。

---

## 10. 体检重复实现网关的缓存键算法

**问题**
体检为了读缓存，自己重算了一遍 `md5(url)[:16]`。

**证据**
`server.py:536-540`：

```python
import hashlib as _h, os as _os
cache_dir = _os.path.join(BASE_DIR, "data", "cache", b["id"])
cpath = _os.path.join(cache_dir, _h.md5(ch["url"].encode()).hexdigest()[:16] + ".txt")
```

而 `engine/gateway.py` 里有 `_url_hash()` 做同一件事（私有函数，所以体检抄了一份）。

**影响**
缓存键一旦变化（加盐、换算法、换目录结构），体检会静默读不到缓存 → 每次都实抓 → 体检变慢，但**不报错**，最难发现的那种。

**改法**
`engine/gateway.py` 暴露公共函数：

```python
def cache_path(bid, url):
    return os.path.join(CACHE_DIR, bid, _url_hash(url) + ".txt")
```

体检改调 `gw.cache_path(...)`，删掉本地那份 md5。

**验证**
改缓存键算法后，体检耗时不应变化（说明读到了缓存）。

---

## 11. 水印规则 `^\d{1,4}$` 误杀面

**问题**
水印过滤把「整行只有 1~4 位数字」和「`#...#...#`」直接判为水印删除。正文里独立成行的章节序号、诗行数字、分隔符会被一起删掉。

**证据**
`autobs/autobs.py:1368` 的 `WATERMARK_LINE` 列表，`autobs/autobs.py:1390-1393` 的 `is_watermark_line()`：`if any(p.search(line) for p in WATERMARK_LINE): return True`。另一处 `engine/gateway.py:346-359` 的 `_filter_domain_watermark()` 删「含主域名的 <100 字符短行」。

**影响**
正文首尾常有的「1」「2」「3」小节号消失；有的作者用 `#` 做分隔，整行被删。单章看是「排版怪」，全本看是「内容缺」。

**改法**
- 数字行只在**首尾 3 行内**才判水印；
- 或要求同时命中「含站名/网址/域名」；
- `#...#...#` 改成「以 # 开头且以 # 结尾且中间无中文」；
- 给 `is_watermark_line` 加一个 `position` 参数（行号 + 总行数）。

**验证**
拿一本正常书抽 20 章，比对过滤前后字数差 < 0.5%；再拿一本有水印的书，确认水印仍被清掉。

---

## 12. 结果硬上限 60 条

**问题**
`uniq[:60]` 是硬编码。10 个站已经能把「遮天」打满 60 条，继续加站只会让来源更分散。

**证据**
`engine/search.py:263`：`return uniq[:60], errors`。

**改法**
- 提成配置项 `max_results`（默认 60，可改 120）；
- 网页台加「显示更多」按钮，走 `?limit=` 参数；
- 注意 Reader 网关那条路返回的是 HTML 页面，别把 60 条都渲染出来（Reader 搜索框只显示前若干条，多余的会拖慢渲染）。

**验证**
搜「遮天」，`limit=120` 时返回 120 条，`limit` 缺省仍 60 条。

---

## 13. 搜索 45s 总超时与单站 40s 耦合

**问题**
SSE 端点的 `as_completed(timeout=45)` 与 `aggregate_search` 内部的 `timeout_per_site*2`（默认 80s）不是一个数量级，慢站会先撑爆外层预算。

**证据**
`server.py:377`：`for fut in as_completed(list(seed_futs) + [web_fut], timeout=45)`；
`engine/search.py:171`：`for fut in as_completed(futs, timeout=timeout_per_site * 2)`。

**影响**
一个卡死站点（如已停用的平凡文学当年 45.2s）会吃掉全部预算，其余站的结果虽然拿到了也可能来不及推送。`ex.shutdown(wait=False)`（`server.py:397`）能救回响应，但用户看到的结果数会忽多忽少。

**改法**
两段式预算：首字节 8s（保证用户很快看到东西）+ 总时长 60s（继续收尾）。实现上把 `as_completed` 换成带 deadline 的手动轮询，或拆成两批：先推快站，慢站进第二轮。

**验证**
人为让一个站 sleep 50s，搜索结果仍能在 8s 内出现首批，总条数不受影响。

---

## 14. P0 三条无单测覆盖

**问题**
`tests/` 现有 `test_units.py`、`test_parse.py`、`quality.py`，覆盖的是解析工具函数。缓存读写、种子沉淀这两条关键链路没有测试。

**证据**
`tests/` 目录下没有 `test_gateway*.py`；CI（`.github/workflows/ci.yml`）只跑 `pytest tests/ -q`。

**改法**
新增 `tests/test_gateway_cache.py`，用 `tmp_path` 替换 `gw.CACHE_DIR`：

| 用例 | 断言 |
|---|---|
| 正常抓取 | 缓存文件存在，内容与返回值一致 |
| 第 2 页失败（问题 3） | 返回 `None`，无缓存文件 |
| 写入中断（问题 2） | 只留 `.tmp`，无正式文件，下次可重抓 |
| 沉淀调用（问题 1） | monkeypatch `maybe_add_seed`，断言被调用 |

`fetcher` 用假对象（只需 `.get()` 返回带 `.content` 的桩），不联网。

**验证**
`python -m pytest tests/ -q` 全绿，且断网可跑。

---

## 15. 怎么验证这份文档

```bash
# 1. 问题 1 确实存在（应打印 FAIL）
python -c "import server
try:
    from search import maybe_add_seed; print('OK')
except Exception as e: print('FAIL:', e)"

# 2. 问题 9 确实存在（应打印 ImportError）
python -c "from autobs import resolve_book"

# 3. 问题 4 的量级（10 站 × 0.6s）
python -c "print('理论下限 %.1fs' % (10 * 0.6))"

# 4. 缓存体量
python -c "import os
n=s=0
for r,_,fs in os.walk('data/cache'):
    for f in fs: n+=1; s+=os.path.getsize(os.path.join(r,f))
print('%d 章 / %.2f MB / %.1f KB 每章' % (n, s/1048576, s/n/1024))"
```

---

## 16. 验收标准

改完之后，这些数字应该成立：

| 指标 | 现状 | 目标 |
|---|---|---|
| 搜「遮天」p50 | 6.0s | ≤2.5s |
| 搜「大日金乌纵横诸天」 | 11s | ≤8s |
| 5 本书体检 | 串行，160s 级 | ≤40s |
| 半章缓存发生率 | 偶发且不可自愈 | 0（不缓存中断章节） |
| 从 Reader 打开的书的种子沉淀 | 0 | 与网页台一致 |
| `data/cache` | 无上限 | >500MB 自动收敛 |
| 单测 | 无缓存/沉淀用例 | 4 条新增，断网可跑 |

---

## 附：不建议改的地方

写文档也要说清楚哪些**看着能优化、实际不该动**，省得下次有人白忙：

1. **`uniq[:60]` 之外的去重逻辑**（同 URL 全丢、web 结果同主域丢弃）——看着粗暴，实际是搜索质量的护栏，放宽只会让结果变脏。
2. **每站 12 条 / 相关度 2 每站 4 条的上限**（`engine/search.py:204-216`）——调大后「遮天」这类热词会被单站刷屏，用户体验反而差。
3. **`trust_env = False`**（`engine/fetcher.py:100`）——强制直连是刻意的，系统代理会让书站返回 RST。
4. **每站熔断 3 次 / 90 秒**（`engine/fetcher.py:127-136`）——参数是调过的，放宽会让限流站点把整个搜索拖死。
5. **预验阶段关闭 L2**（`engine/websearch.py:203`）——CHANGELOG 里有对照实验：通过集合差集为 0，耗时 22.2s → 9.8s。这是优化不是妥协。
