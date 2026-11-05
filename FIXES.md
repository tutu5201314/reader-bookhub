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
| FIX-17 | 新增 | 体检只会说「站点不可达」，分不清 CF 拦截/超时/404 | 已修 |
| FIX-18 | 新增 | `_refreshing` 被写进磁盘 → 重启后后台刷新永久停摆 | 已修 |
| FIX-19 | 新增 | L3 依赖缺失：包在 requirements 里但没装，L3 静默全废 | 已修 |
| FIX-20 | 新增 | 天空书屋内容投毒却仍是启用种子站 | 已修 |
| FIX-21 | 新增 | 目录分页只翻 20 页 + 分页方向不一致导致目录腰斩/乱序 | 已修 |

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

---

## FIX-17 体检只会说「站点不可达」

**对应** 用户实测反馈：`ddkshu.com` 体检报「站点不可达」（`OPTIMIZATION.md` 未列）
**文件** `engine/fetcher.py:274-325`（新增 `diagnose`）、`server.py:589-660`、`static/index.html:284-300`

**问题**
体检探活只有一个布尔：`r = fetcher.get(url, quick=True)`，`r is None` 就算不可达。四种完全不同的情况在界面上长得一样：

1. Cloudflare 硬拦截（`Attention Required!`，本机 IP 被挡，换域名/换 IP 才行）
2. Cloudflare 人机挑战（`Just a moment`，真浏览器 L3 有可能过）
3. 连接超时（网络层）
4. HTTP 404（页面没了，该书要换源）

实测 `https://www.ddkshu.com/dk-416895.html` → `server: cloudflare`、`cf-ray: ...-LAX`、403 + `Attention Required!`：**属于第 1 类，站点本身没挂，是我们这个出口 IP 被挡了**。而 `biquge.tw` 同样 L1 恒 403，L2 却能过 —— 若只看布尔值，这两本书会被判成同一种病。

顺带两个显示问题：
- 站点挂了时中/末章「未取到」会被误读成正文解析坏了（实际只是没缓存又抓不到）。
- 不可达的书仍显示 🟢（`ok` 只看目录与章节 problems，不看 alive）。

**改法**
新增 `LayeredFetcher.diagnose(url)`，返回 `{"ok", "code", "reason"}`：

```python
raw = self.s.get(url, timeout=timeout)      # 裸 L1，先拿状态码
# 200 且非 WAF 页 -> 正常
# 超时 -> 直接返回「连接超时」（L2 救不了，省 20s）
# 其它 -> 再给 L2 一次机会，过了就是「L1 被挡，L2 兜底通过」
# 仍不过 -> 按特征页分类：CF 硬拦截 / CF 人机挑战 / 403 / 404 / HTTP 5xx
```

体检侧：
- `entry["alive_reason"]` 存原因，前端标签显示「站点不可达：Cloudflare 硬拦截（403，本机 IP 被挡）」。
- `alive=False` 时**不再实抓**中/末章，只读缓存；没缓存标 `skipped: "站点不可达，未抽查"`（省 3 次超时，也不再把站点故障误报成章节故障）。
- 新增三态 `level`：`ok` 绿 / `warn` 黄（站点当前不可达但目录与缓存还在，仍可读已缓存章）/ `bad` 红（目录或首章坏了）。汇总文案带「N 本站点暂不可达（缓存仍可读）」。

**验证**
`tests/test_health_diagnose.py`（新增 6 条）：干净页判 ok；L2 救回不算挂；CF 硬拦截与 CF 挑战分开命名；超时不走 L2；404 命名。
真网复跑体检（2 本）：`笔趣阁tw` → `alive=True / L1 被挡，L2 兜底通过 / level=ok`；`ddkshu.com` → `alive=False / Cloudflare 硬拦截（403，本机 IP 被挡）/ level=warn`，中/末章显示「站点不可达，未抽查」。

---

## FIX-18 `_refreshing` 被写进磁盘 → 后台刷新永久停摆

**对应** 查 FIX-17 时顺手在 `data/books/6e31a7d02c.json` 里看到 `"_refreshing": true`
**文件** `engine/gateway.py:135-145`（加载）、`engine/gateway.py:166-176`（持久化）

**问题**
`get_catalog` 在触发后台刷新前置 `b["_refreshing"] = True`，`_bg_refresh` 的 `finally` 里复位。但只要刷新途中进程被杀（关 Reader、Ctrl+C），`_persist` 已经把 `_refreshing: true` 写进了 `data/books/{id}.json`；重启后这个字段被当成真的读回来，于是 `if stale and not b.get("_refreshing")` 永远为假 —— **这本书的目录再也不会自动追更**，而界面上看不出任何异常。

**改法**
- `_persist` 落盘前过滤下划线开头的键（运行时状态不进文件）。
- 加载时 `b.pop("_refreshing", None)`（已经写脏的旧文件也能自愈）。

**验证**
`tests/test_gateway_cache.py` 追加 2 条：`_persist` 后磁盘上没有 `_refreshing` 但 `title` 等持久字段还在；磁盘上带 `_refreshing: true` 的旧文件加载后该字段被丢弃。

---

## FIX-19 L3 依赖缺失：包在 requirements 里但没装，L3 静默全废

**文件** `requirements.txt`（`DrissionPage>=4.0`）、`engine/fetcher.py:44-131`（`BrowserLayer`）

**问题**
`requirements.txt` 声明了 `DrissionPage>=4.0`，但本机实际 `import DrissionPage` → `ModuleNotFoundError`。`BrowserLayer._ensure()` 抛的异常被 `get()` 外层 `except` 吞成 `[L3] browser failed: No module named 'DrissionPage'` 后返回 `None`，**L3 整层等于不存在**，而所有上层逻辑（`_req_layers` 的第三层、`diagnose` 的「Cloudflare 人机挑战（L3 可试）」判定）都以为它还在。换源排查时才暴露：任何 JS 渲染站、CF 挑战站都只能判「不可达」。

**改法**
- 环境层：`python -m pip install "DrissionPage>=4.0"`（装到 4.1.1.4），依赖本机 Chrome/Edge（本机两者都有）。
- 文档层：README 环境要求里把「可选：Chrome 或 Edge」改成明确的两件套（Python 包 + 本机浏览器），并给出 3 行自检命令，避免下次再靠翻日志才发现。

**验证**
```
biquge.tw      L3 -> 44385 bytes  5.4s
shanyuread.com L3 -> 44616 bytes  4.6s
ddkshu.com     L3 ->  4936 bytes  1.5s   ← CF 硬拦截页，IP 级封禁，L3 也过不去（符合预期）
```
`biquge.tw` 之前 L1 恒 403、只能靠 L2 兜底，现在 L3 直接 44KB 正常页 → L3 通路恢复。
注：`shanyuread.com` 标题仍是「请稍候…」，说明 CF 挑战在 `BrowserLayer.get()` 的 10s 等待窗内没解完，属已知限制（该站种子仍停用）。

---

## FIX-20 天空书屋内容投毒却仍是启用种子站

**文件** `engine/seeds.json:53-61`

**问题**
换源《从鹰人进化为天使后，我荡平诸神》时实测：`m.tkshuw.com`（及 www 域）目录章数与原站一致（1863），但**第 5/6/900 章随机返回别的书的正文**（「第59章 你知道皓月的真正身份吗」「宋家被抄了」「冒险者工会…」），且污染会直接落进 `data/cache/{id}/`。它又是所有种子里章数最多的一个，搜索排序靠前，用户点进去就是串书。

**改法**
`engine/seeds.json` 该条目加 `"disabled": true` + `disabled_reason`，与既有 4 个停用站的记录格式一致。启用种子站 10 → 9。

**验证**
`load_seeds()` 返回 9 站，天空书屋不再出现；搜索《鹰人》时结果里只剩笔趣阁tw 与书迷楼两个真实源。条目保留未删，日后若站点修好，去掉 `disabled` 即可复活。

---

## FIX-21 目录分页只翻 20 页 + 分页方向不一致

**文件** `autobs/autobs.py`（`MAX_CATALOG_PAGES`、`CATALOG_MERGE_BUDGET`、`_chap_num`、`sort_chapters_by_num`、目录分页合并段）

**问题**
换源《从鹰人进化为天使后，我荡平诸神》时，一心文学 `www.shyixin.cn/ydxs/80/80152/` 的目录是 94 页 × 20 章 = 1864 章，但 `resolve_book` 只合并到 **430 章**就停了 —— 合并循环写死 `for _ in range(20)`。20 页封顶意味着**每页 20 章的书最多只能拿到 400 章**，1800+ 章的长篇被腰斩，而且日志只报一句「目录分页合并: 20 → 430 章」，看不出是被截断的。

把上限放宽后又暴露第二个问题：该站第 1 页是「最新在前」（第1849→第1830章），第 2 页起是正序（第1章起）。合并结果变成 `1849..1840` 打头、后面接 `1..1839`，Reader 目录里最新 10 章跑到最前面去了。

**改法**
- `MAX_CATALOG_PAGES = 300` + `CATALOG_MERGE_BUDGET = 300`（秒）：页数上限放宽，另配时间预算，超时就停并 WARN，宁缺勿卡死；每 25 页打一行进度（「已 530 章（第 26 页）」）。
- `sort_chapters_by_num(chapters)`：章号齐全且**最长递增段 ≥90%** 时按章号重排。90% 这道闸是给分卷书留的 —— 分卷会重新编号（第1..210 章之后又回到第24章），那种书按号排序等于把正文彻底打乱，`大日金乌纵横诸天` 就是这种（473 章，最长递增段 210/473 = 44%，会被正确跳过）。
- `_chap_num` 容错：原正则只认 `第X章`，遇到 `第268（2） 章 法凯伦世界` 返回 None，导致整本书因「有章号取不到」而放弃排序。改为允许 `第X（N）章` 这种同章分节写法。
- 被 `drop_latest_block` 当镜像删掉的头部块，若其 URL 不在全书目录里（即真是新章而非镜像），补回尾部；真镜像块的 URL 必然重复，会被跳过。

**验证**
```
修前：[OK] 目录分页合并: 20 → 430 章   （94 页只翻了 21 页）
修后：[OK] 目录分页合并: 20 → 1864 章
      [INFO] 目录顺序已按章号重排（1864 章）
      resolve ok 用时 71.1s
章数 1864 | 首 第1 章 高蛮山鹰人部落 | 末 第 1849章落去旧时代的残余
正文抽查：第1章 2494 字 / 第2章 1722 / 第500章 2260 / 第1500章 2156 / 第1864章 1064
```
新增 `tests/test_catalog_paging.py`(6)：页数上限、`_chap_num` 三种写法、倒序头重排、干净列表不动、分卷书不动、小列表不动。全量 **98 passed, 3 skipped**。
