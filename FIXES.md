# 修复记录 FIXES.md

对应 `OPTIMIZATION.md` 的 14 项逐条落地。每项记录：改了什么、为什么、改了哪个文件的哪几行、怎么验证。
每项修完都跑 `python -m pytest tests/ -q`，全绿才算完成。

## 进度总表

| 记录 | 对应优化项 | 主题 | 状态 |
| --- | --- | --- | --- |
| FIX-01 | §1 | 网关路径的种子沉淀是死代码 | 已修 |
| FIX-02 | §2 | 章节缓存原子写 | 已修 |
| FIX-03 | §3 | 半章正文被当完整章节缓存 | 已修 |
| FIX-04 | §14 | 上述三条补单测 | 已修 |
| FIX-05 | 新增 | `parse_doc` 只探 title 判断乱码，正文页漏判 | 已修 |
| FIX-06 | 新增 | `drop_latest_block` 跨页镜像多删 1~2 章 | 已修 |
| FIX-07 | §4 | 全局 0.6s 节流 → 每站节流 | 已修 |
| FIX-08 | §5 | L3 浏览器单实例+全局锁 → 标签页池 | 已修 |
| FIX-09 | §6 | 体检逐本串行 → 4 路并发 | 已修 |
| FIX-10 | §7 | 章节缓存无上限 → 配额 + 统计接口 | 已修 |
| FIX-11 | §8 | 后台任务永不回收 → TTL + 数量上限 | 已修 |
| FIX-12 | §9 | autobs 不是包 + 死 sys.path | 已修 |
| FIX-13 | §10 | 缓存键两处各算一遍 → 统一 `cache_path` | 已修 |
| FIX-14 | §11 | 水印规则误杀正文数字行 | 已修 |
| FIX-15 | §12 | 结果 60 条硬截断 → 可配 + 「显示更多」 | 已修 |
| FIX-16 | §13 | SSE 超时与站点超时耦合 | 已修 |

---

## FIX-01 网关路径的种子沉淀是死代码

**对应** `OPTIMIZATION.md` §1
**文件** `engine/gateway.py:238-250`

**问题**
`_try_sediment` 用的是裸模块名导入：

```python
from search import maybe_add_seed
from fetcher import LayeredFetcher
```

项目里这两个模块在 `engine/` 下。从 `server.py` 启动时 `sys.path` 里有 `engine/`，能导入；但 `engine.gateway` 被当包导入时（`from engine import gateway`、单测、CI）`sys.path` 里没有 `engine/`，`ImportError` 被外层 `except Exception: pass` 吞掉。后果是**通过 Reader 打开的书永远不沉淀种子**，而这条路径是种子库唯一的自动增长来源。

**改法**
先按包名导入，失败再退回裸名（兼容直接跑 `python engine/gateway.py` 的场景）；外层不再静默，失败打一行日志：

```python
try:
    from engine.search import maybe_add_seed
    from engine.fetcher import LayeredFetcher
except ImportError:      # 直接以脚本方式跑 engine/ 里的文件时
    from search import maybe_add_seed
    from fetcher import LayeredFetcher
...
except Exception as e:
    print("[seed] 沉淀失败 %s: %r" % (bid, e))
```

**验证**
`python -c "import server; from engine import gateway"` 后调用 `_try_sediment` 不再抛 `ModuleNotFoundError`；`tests/test_gateway_cache.py::test_try_sediment_uses_package_import` 覆盖。

---

## FIX-02 章节缓存原子写

**对应** `OPTIMIZATION.md` §2
**文件** `engine/gateway.py:335-345`

**问题**
`open(cpath, "w").write(text)` 直接截断目标文件再写。进程在写到一半时被杀（用户关 Reader、Ctrl+C、磁盘满），磁盘上就留下一个半截文件；读取端 `engine/gateway.py:266-276` 是「存在即返回」，于是半章永久生效。

**改法**
写 `.tmp` 再 `os.replace` 同卷原子替换，失败清掉残留 tmp：

```python
tmp = cpath + ".tmp"
try:
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
    os.replace(tmp, cpath)
except Exception:
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
```

**验证**
`tests/test_gateway_cache.py::test_normal_fetch_writes_cache_atomically`：抓完断言缓存文件存在、内容完整、目录里没有残留 `.tmp`。

---

## FIX-03 半章正文被当完整章节缓存

**对应** `OPTIMIZATION.md` §3
**文件** `engine/gateway.py:301-334`、`engine/gateway.py:281-288`

**问题**
正文分页循环 `while cu and hops < 8` 里，任何一页失败就 `break`，但已抓到的部分仍拼成 `text`，只要 ≥50 字就落缓存 —— 半章被永久化，用户重读也读不到后半段。

**改法**
三处失败（`rr is None` / `parse_doc` 返回 None / `len(got) != 1`）置 `interrupted = True` 再 `break`；结尾区分两种退出：

```python
if interrupted:
    return text      # 正文照常给用户读，但不落缓存，下次重读会重抓
```

另外读取端补一条损坏兜底：缓存文件 <50 字视为上次写坏的残留，删掉重抓（覆盖 FIX-02 之前已经落盘的历史脏数据）：

```python
if hit and len(text.strip()) < 50:
    hit = False
    try:
        os.remove(cpath)
    except Exception:
        pass
```

**验证**
`tests/test_gateway_cache.py::test_interrupted_pagination_is_not_cached`（第 2 页失败 → 返回已抓内容、不写缓存）、`::test_corrupt_cache_self_heals`（预置 10 字节脏缓存 → 重抓并覆盖）。

---

## FIX-04 上述三条补单测

**对应** `OPTIMIZATION.md` §14
**文件** `tests/test_gateway_cache.py`（新增，4 条用例）

`_Fetcher` 按序吐出预置整页 HTML，`None` 表示该页抓取失败；`_page` / `_page_with_next` 构造带「下一页」锚点的正文页；`_make_store` 把 `DATA_DIR` / `BOOKS_DIR` / `CACHE_DIR` 指到 `tmp_path` 并关掉预取线程，测试不碰真实 `data/` 目录、不发网络请求。

---

## FIX-05 `parse_doc` 只探 title 判断乱码

**对应** 修复 §3 时发现的新问题（`OPTIMIZATION.md` 未列）
**文件** `autobs/autobs.py:231-245`

**问题**
`parse_doc` 把 bytes 交给 libxml2 时，若 `<meta>` charset 写法非标准（`content` 在 `http-equiv` 前面），libxml2 认不出编码、退化成 Latin-1，中文全变 mojibake。原有兜底只探 `//title/text()` 判断是否乱码 —— 页面没有 `<title>`、或 title 是纯 ASCII 而正文是中文时漏判。

漏判的后果不只是正文乱码：`find_content_next_anchor` 按文案匹配「下一页」，文案变 mojibake 就永远匹配不上，**正文分页静默失效，每章只抓到第一页**（实测锚点文本 `['ä¸\x8bä¸\x80é¡µ']`，修复后 `['下一页']`）。

**改法**
title 未判出乱码时，再探正文前 5 个文本节点：

```python
if not _looks_mojibake(head):
    try:
        head = head + " " + " ".join(
            doc.xpath("//body//text()[position() <= 5]"))[:300]
    except Exception:
        pass
```

**验证**
`tests/test_gateway_cache.py` 的续页用例本身就是回归测试：测试页面不带 `<meta charset>`，修复前该用例因锚点匹配失败而红。

---

## FIX-06 `drop_latest_block` 跨页镜像多删 1~2 章

**对应** 修 FIX-01~03 跑全量测试时暴露（`tests/test_units.py:152` 红）
**文件** `autobs/autobs.py:1580-1607`

**问题**
形态2（目录页顶部「最新章节」倒序预览块，其 URL 不在下方正序列表里重现）用

```python
for k in range(n // 3, 2, -1):     # 从大到小
```

从大往小找边界，而「前缀递减」用的是 `>= 0.9` 的比例容差（允许一次反转）。两个条件叠加：k 一旦越过真实边界，前缀里多带一个正文首章仍然满足「递减 ≥90%」，于是先被接受 —— 边界章被当成预览块删掉。实测该用例 `dropped=11`，正解 `9`。

**改法**
改成从小到大扫，并收紧三个判据，让「最小命中值」就是真实边界：

```python
for k in range(3, n // 3 + 1):
    ...
    if any(a <= b for a, b in hp):          # 前缀必须严格递减
        continue
    if head[-1] <= tailn[0]:                # 边界处必须掉头向下
        continue
    if tailn[0] >= tailn[1]:                # 边界后第一对必须已经在涨
        continue
    if sum(1 for a, b in tp if a < b) / len(tp) >= 0.98:   # 原 0.95
        return items[k:], k
```

第三条是关键：k 少一格时，预览块最后一个章号会作为尾部首元素出现，它比后一个还大（第62章 → 第1章），据此挡掉。原来只靠 0.95 的比例，尾部一大段正序（第1~60章）足以把几个逆序对稀释到阈值以上，这也是 k=6 / k=8 都能蒙混过关的原因。

**验证**
`python -m pytest tests/ -q` → 65 passed / 3 skipped，`test_drop_latest_block_crosspage` 恢复绿色且断言 `dropped == 9` 与 `len(out) == 60` 同时成立。

---

## FIX-07 全局 0.6s 节流 → 每站节流

**对应** `OPTIMIZATION.md` §4
**文件** `engine/fetcher.py:106-113`（初始化）、`engine/fetcher.py:156-170`（`_throttle`）、`engine/fetcher.py:216`（调用点）

**问题**
`_throttle` 用实例级 `self.last`，不区分站点。10 个种子站并发搜索时全排同一个 0.6s 闸门，总耗时 = 10 × 0.6s ≈ 6.0s，与 CHANGELOG 记录的「遮天 6.0s / 60 条」完全吻合 —— 站点响应时间被节流掩盖，节流就是硬瓶颈。

**改法**
按 host 计时，并用「先占位再睡」保证并发下同站仍然串行：

```python
def _throttle(self, url=""):
    host = self._host_of(url) if url else ""
    now = time.time()
    with self._throttle_lock:
        last = self._last_by_host.get(host, 0.0)
        wait = 0.6 - (now - last)
        self._last_by_host[host] = max(now, last + 0.6)
        self.last = now
    if wait > 0:
        time.sleep(wait)
```

语义从「全局限速」变成「每站限速」：对任何单个站点仍然礼貌（同站 ≥0.6s），跨站不再互相拖累。熔断 `_host_state` 本来就是按 host 的，节流按 host 是同一套思路，当初只是漏了。

**验证**
`tests/test_fetcher_throttle.py`（新增 3 条）：4 个不同站连发 < 0.3s；同站第二次 ≥ 0.5s；两个线程同时冲同一站被串行化。

---

## FIX-08 L3 浏览器单实例+全局锁 → 标签页池

**对应** `OPTIMIZATION.md` §5
**文件** `engine/fetcher.py:40-131`

**问题**
`BrowserLayer` 只有一个 `ChromiumPage`，`get()` 整段套 `with self._lock`：所有走 L3 的请求严格排队，单次最坏 `page.get` 25s + 等挑战 10s = 35s。`web_discover` 的第二级降级（`force_l3` 重抓 Bing）和预验都可能同时要浏览器，8 个候选串起来直接撞穿外层预算。

**改法**
改成固定大小的标签页池，锁只保护「启动浏览器 / 新建 tab」：

```python
POOL_SIZE = 2
def _acquire(self, timeout=30):     # 池未满就 new_tab，满了排队
def get(self, url, timeout=25, wait=6):   # 等挑战改成 0.4s 轮询、上限 wait 秒
```

等挑战的硬上限从 10s 降到 `wait=6` 秒，取不到空闲 tab 直接返回 `None`（让上层熔断计数，而不是干等）。

**验证**
`tests/test_browser_pool.py`（新增 3 条，用假 tab 替身，不启动真浏览器）：pool=2 两个并发 < 0.75s；pool=1 必须排队 ≥ 0.45s；返回的 `BrowserResponse` 带 `browser=True` 与正文。

---

## FIX-09 体检逐本串行 → 4 路并发

**对应** `OPTIMIZATION.md` §6
**文件** `server.py:589`（`_check_one`）、`server.py:600-640`（并发调度）

**问题**
`/api/healthcheck` 逐本串行：每本探活 + 目录 + 首/中/末章抽查，单本 4 个请求。书架 10 本 = 40 个串行请求，实测 30s+，网页台转圈到超时。

**改法**
抽出 `_check_one(b)` 单本检查，外层 `ThreadPoolExecutor(max_workers=4)` + `as_completed`；单本异常只跳过该本并记日志，不炸整个体检；结果按书架原顺序重排后再写 `data/health.json`（否则前端顺序会随完成顺序乱跳）。

**验证**
`python -c "import server"` 通过；`_check_one` 内无共享可变状态，单本异常被 `as_completed` 那层的 `try/except` 捕获后只记一行日志（`server.py:643-647`），不影响其余本；输出 `report = [by_id[b["id"]] for b in books ...]` 保证与书架顺序一致。

---

## FIX-10 章节缓存无上限 → 配额 + 统计接口

**对应** `OPTIMIZATION.md` §7
**文件** `engine/gateway.py:75-118`（`cache_stats` / `prune_cache`）、`server.py:45`（配置）、`server.py:512`（接口）、`server.py:788-798`（启动检查）

**问题**
章节缓存只增不减。实测约 8.5KB/章，一本 1859 章的书全缓存 ≈ 16MB，20 本就 320MB，且没有任何地方能看出谁在占空间。

**改法**
- `cache_stats()`：遍历 `data/cache/<bid>/`，按书汇总 `size/files/last_access`，最旧在前。
- `prune_cache(max_bytes)`：超配额就按 `last_access` 从旧到新**整本**删（书架记录、阅读进度不动），返回 `{freed, removed, before, limit}`。
- 配置新增 `cache_max_mb`（默认 500），启动时跑一次并打印清理结果。
- 新接口 `GET /api/cache/stats`：总大小 / 配额 / 每本占用（带书名）。

**验证**
`tests/test_cache_quota.py`（新增 3 条）：统计按书求和且最旧在前；超配额只删最旧那本；配额内不动。

---

## FIX-11 后台任务永不回收 → TTL + 数量上限

**对应** `OPTIMIZATION.md` §8
**文件** `server.py:229-268`

**问题**
`JOBS` 是个只增不减的 dict，每个任务还带着完整日志列表。解析/导出用久了内存只涨不落。

**改法**
```python
JOBS_TTL = 1800      # 完成的任务留 30 分钟
JOBS_MAX = 20        # 再多就按创建时间砍
def _finish(job, status):    # 统一置 status + finished_at
def _prune_jobs(now=None):   # _new_job 开头调用
```
`finished_at` 是回收判据，所以 8 处 `job["status"] = "done"/"failed"` 全部换成 `_finish(job, ...)`，避免漏设时间戳导致任务永远不被回收。

**验证**
`tests/test_server_jobs.py`（新增 4 条）：`_finish` 必须打 `finished_at`；超 TTL 的已完成任务被回收；跑着的任务再老也不回收；已结束记录数被裁到 `JOBS_MAX` 内。

写测试时踩到一个真实的 off-by-one：`_prune_jobs` 在插入**之前**裁，裁到 20 条后立刻又插进 1 条，稳态是 21。改成 `_prune_jobs(reserve=1)`，`cap = JOBS_MAX - reserve`，插入后正好 20。

---

## FIX-12 autobs 不是包 + 死 sys.path

**对应** `OPTIMIZATION.md` §9
**文件** `autobs/__init__.py`（新增）、`server.py:15`（删除）

**问题**
`autobs/` 没有 `__init__.py`，靠 `engine/fetcher.py:15`、`engine/gateway.py:20` 的 `sys.path.insert(..., "autobs")` 当顶层模块导入；`server.py` 能 import 成功纯属「先 import 了 engine 包」的副作用。另外 `server.py:15` 的 `sys.path.insert(0, .../".."/"autobs")` 指向不存在的目录，是死代码。

**改法**
新建 `autobs/__init__.py`（`from .autobs import *` + 显式导出 `UA` / `READER_UA` / `is_waf_page`），删掉 `server.py` 那行死代码。两种导入方式都保留兼容。

**验证**
`python -c "from autobs import resolve_book"`（仓库根直接跑）成功，修复前是 `ImportError`。

---

## FIX-13 缓存键两处各算一遍 → 统一 `cache_path`

**对应** `OPTIMIZATION.md` §10
**文件** `engine/gateway.py:65-72`、`server.py:589` 附近

**问题**
章节缓存键 `md5(url)[:16] + ".txt"` 在 `gateway.fetch_chapter` 和 `server.py` 的体检里各实现一遍。任何一边改动（换哈希长度、加后缀）都会让体检读不到缓存、误报「章节为空」。

**改法**
`gateway.cache_path(bid, url)` 作为唯一出口，`fetch_chapter` 与体检都调它。

**验证**
`grep -n "md5" server.py` 已无缓存键相关实现；体检对已缓存章节判定为缓存命中。

---

## FIX-14 水印规则误杀正文数字行

**对应** `OPTIMIZATION.md` §11
**文件** `autobs/autobs.py:1404-1450`

**问题**
`WATERMARK_LINE` 里有两条过宽的规则：`^\d{1,4}$`（任何 1~4 位数字行）和 `#.*#.*#`（任何带两个 # 的行）。正文里的「3」「1998」这种独立成行的数字、以及含 `#` 的对白/标记行会被整行删掉 —— 读者看到的是缺字的正文。

**改法**
- 裸数字行只在**首尾 3 行**内删（水印通常在页头页尾），中间位置一律保留。
- 带 ≥2 个 `#` 的行必须同时命中分隔线形态（`_HASH_SEP`）或水印语义词（`_HASH_WM`：退出/阅读/模式/章节/缺失/本站/域名/记住/最新章节）才删。
- `is_watermark_line(line, idx=None, total=None)` 增加位置参数；位置未知（老调用方）时保持旧行为。
- `extract_content_text` 改为 `enumerate` 后传 `(ln, i, total)`。

**验证**
`tests/test_units.py` 追加 4 条：首尾裸数字被删、中间裸数字保留、`#` 行需命中语义才删、超长行不被当水印。

---

## FIX-15 结果 60 条硬截断 → 可配 + 「显示更多」

**对应** `OPTIMIZATION.md` §12
**文件** `engine/search.py:26,151,268`、`server.py:345-370`、`static/index.html:79-150`

**问题**
`return uniq[:60]` 写死。种子站多于 10 个或书名常见（「遮天」这类）时，后面的站结果直接被切掉，用户无从知道还有没有。

**改法**
- `engine/search.py` 提 `MAX_RESULTS = 60`，`aggregate_search(..., max_results=None)` 用参数覆盖。
- 两个搜索端点接 `limit`（`0~300`，0 = 用配置），`DEF_CONFIG` 增 `max_results`。
- 网页台读 `/api/config` 拿上限；结果打满上限时才在底部画「显示更多」，点击把 `limit` 加倍重搜（上限 300）。没打满就不画按钮 —— 说明真没了。

**验证**
`?limit=120` 返回条数上限生效；网页台默认 60 条时底部出现按钮，点一次变 120。

---

## FIX-16 SSE 超时与站点超时耦合

**对应** `OPTIMIZATION.md` §13
**文件** `server.py:60`、`server.py:400-440`

**问题**
SSE 用 `as_completed(timeout=45)`，而单站超时是 `timeout_per_site=40` 且 `as_completed` 的 timeout 是**整体**超时：一旦触发就抛 `TimeoutError`，把已经算完但还没 yield 的结果一起丢掉。慢站（历史最慢 45s）等于永久丢结果。

**改法**
```python
SSE_TOTAL_BUDGET = 60
while pending:
    done, pending = wait(pending, timeout=min(left, 5), return_when=FIRST_COMPLETED)
    for f in done: 立刻 yield        # 谁先回谁先推
    if 超 cap: 只 drain 不推
```
不再抛 `TimeoutError`；预算到点只停止等待，已推出去的批次不受影响。

**验证**
慢站（手动加 sleep 的假种子）下，快站结果仍在 1~3s 内先到；预算耗尽时已到达的批次全部保留。
