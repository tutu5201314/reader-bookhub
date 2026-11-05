# bookhub 改动记录

> 时间跨度：2026-11-02 ~ 2026-11-03
> 覆盖范围：端口改造、搜索链路、种子站体系、书籍解析、Reader 网关、体检、可观测性、流式搜索、性能优化
> 记录原则：每条改动都写明「问题 → 根因 → 改动 → 验证」，便于回溯与复现

---

## 摘要

| # | 领域 | 核心问题 | 结果 |
|---|---|---|---|
| 1 | 端口 | 80 易被占用 | 主端口 8787 + 80 网关兜底，三种占用场景全自动 |
| 2 | 搜索可用性 | 结果恒为 0 | 修复编码缺陷 + 接入百度，0 → 6 条 |
| 3 | 种子站 | 仅 1 个可用 | 扩至 10 个启用 + 4 个停用（写明原因） |
| 4 | 解析 | 只出 12 章 / 目录乱序 / 半章正文 | 三处修复，473 章完整、顺序正确、字数翻倍 |
| 5 | Reader 网关 | 点书报「http 状态码错误」 | 去掉中文参数，400 → 200 |
| 6 | 体检 | 中/末章 0 字、站点误判不可达 | 三章实抓 + 快速模式补 L2 |
| 7 | 可观测性 | 无日志、无耗时 | 日志落盘 + 三层埋点，首个发现 45s 黑洞 |
| 8 | 流式搜索 | 等 48 秒才见结果 | SSE 逐批推送，首批 1.1 秒 |
| 9 | 性能 | 搜索 26 秒 | 降至 11 秒（约 -57%），结果数不变 |
| 10 | 数据维护 | 测试残留、孤儿文件 | 全部清理并备份 |

---

## 一、端口：80 → 8787

### 问题
80 端口容易被其他服务占用，导致 bookhub 启动失败。

### 约束
80 不只是监听参数：`bs_gateway.json` 的 `host` / `query_url` 都以它为基准，Reader 通过该书源访问网关。直接改端口会让 Reader 在线搜书失效。

### 方案：双端口
| 场景 | 网页台 | Reader 网关 | 书源文件 |
|---|---|---|---|
| 80 空闲（常态） | 8787 | 80，`http://127.0.0.1` | 不变 |
| 80 被占用 | 8787 | 并入 8787 | 自动改写为 `:8787` |
| 80 与 8787 都被占 | 顺延 8788 | 并入 8788 | 自动改写为 `:8788` |

实现要点：
- `port` 默认 **8787**，被占用时 `_pick_port()` 自动顺延（最多 20 个）
- `gateway_port` 默认 80：空闲则子线程再起一个 uvicorn 监听同一 app；被占用则 `gw_port=0`
- `GW_PRIMARY` 由启动时实际可用性决定；`gw_base(req)` 优先用 `req.base_url`，保证从哪个端口访问，页面内链接就指回哪个端口
- `_sync_gateway_source()` 启动时把书源对齐到实际地址，只改 host/query_url，其余字段保留，改动前留 `.bak`；端口没变则不碰文件

### 改动文件
`server.py`、`config.example.json`、`bookhub.bat`、`README.md`、`.gitignore`

### 附带修复
`bookhub.bat` 原来的停止逻辑用 `taskkill` 杀掉**所有占用 80 端口的进程**，可能误杀其他服务。已限定只杀 `python.exe`。

### 验证
1. 常态：8787 与 80 同时 200，书源 `http://127.0.0.1`
2. 80 被占：8787 正常，base 降级 `:8787`，书源自动改写
3. 双端口全被占：网页台顺延 8788，网关并入 8788
4. 恢复后重启：书源自动回正

### 遗留
端口探测与 uvicorn 实际 bind 之间有毫秒级竞争窗口，失败只打印日志、不重试。

---

## 二、搜索可用性

### 2.1 编码缺陷（影响所有书，最隐蔽）

**现象**：搜索任意关键词都返回 0 条，且 `errors` 为空——请求全部"成功"。

**根因链**：
1. Bing 的 meta 写成 `<meta content="..." http-equiv="content-type" />`，`http-equiv` 排在 `content` 之后
2. libxml2 认不出这个属性顺序，回退 Latin-1 解码
3. 标题变成 `é®å¤©ï¼2010å¹´…`
4. `engine/websearch.py` 要求标题必须包含关键词 → 10 条候选被 100% 过滤

**修复**：`autobs/autobs.py` 新增 `_looks_mojibake()`，用 `s.encode('latin-1').decode('utf-8')` 的可逆性检测乱码，命中则改用 `resp.text` 重解析。正常中文会让 `encode('latin-1')` 直接抛异常，不会误伤。

**验证**：`遮天` 0 → 3 条（编码修复单独生效）；接百度后 6 条。

### 2.2 Bing 收录不足 → 百度兜底

**根因**：「大日金乌纵横诸天」在 Bing 三个查询变体下返回的是**完全相同的「大」字字典释义**（汉典、百度百科、汉语国学），说明 Bing 判定无匹配后退化成单字检索。同一本书百度一次就命中番茄、黄易天地、零点等多家。

**修复**：`engine/websearch.py` 新增 `baidu_search()` 作为第三级降级，Bing 不足 6 条时补充。百度链接是 `baidu.com/link?url=` 跳转链，真实地址在 `_verify` 跟随重定向时取回。

### 2.3 预验超时设计错误

**根因**：`_verify` 原来走完整重试链（单站 3×15s = 45s），而并发总预算只有 `VERIFY_TIMEOUT*3 = 36s`。**一个无法访问的站点就能耗尽整批候选的验证时间**，导致结果时有时无。

**修复**：改用 `fetcher.get(url, quick=True)`（单次请求、不重试）。

### 2.4 站点名显示 bug

`host.replace("m.", "")` 会把域名中段的 `m.` 删掉：`zhenganfm.com` → `zhenganfcom`。改为只去 `www.` / `m.` 前缀。

### 2.5 动漫站黑名单

`DOMAIN_BL` 增加 `xgcartoon.com` / `cartoon` / `comic` / `anime`（43 → 47 条）。起因是搜「遮天」会命中「遮天（4K）【国语】免费高清动漫在线看」这类页面，解析不出正文，白占结果位。

**验证**：`遮天` 18 条，动漫类残留 0；`readnovel.com` / `guishuji.com` 正常放行。

---

## 三、种子站体系

### 3.1 当前站点

**启用 10 个**：笔趣阁tw、速读谷、一心文学、天空书屋、思兔阅读、笔趣阁bqquge、爱丽丝书屋、52书库、禁忌书屋、新笔趣阁

**停用 4 个**（`disabled: true` + `disabled_reason`，JSON 无注释，用字段代替）：
| 站点 | 停用原因 |
|---|---|
| 平凡文学 | 域名失效，单站耗时 45s（占总时长 94%） |
| 大米小说 | 搜索被重定向回首页，产不出结果 |
| 山雨阅读 | 搜索页被 Cloudflare 挑战，cloudscraper 也过不去 |
| 28看书网 | 无站内搜索，页内「搜索」跳转外部 sososhu.com |

### 3.2 繁简归一（繁体站能用的前提）

笔趣阁tw 的书名简繁混排（「遮天」与「萬世武神」并存）。不做转换的话，简体关键词搜到的繁体书名会被相关度判为「不相关」而整批丢弃。

`engine/search.py` 新增 `_fold_cjk()`（zhconv 繁→简，带 8192 上限缓存，未装库自动降级为不转换），`_relevance()` 比较前先归一。

**验证**：`大日金乌纵横诸天` 命中繁体书名《大日金烏縱橫諸天》。

### 3.3 书源仓库批量导入

**来源**：`https://www.yck2026.fun/yuedu/shuyuan/`，每个书源有标准 JSON 直链：
```
https://www.yck2026.fun/yuedu/shuyuan/json/id/{id}.json
```

**转换规则**（阅读 3.0 → seeds.json）：
1. `{{java.encodeURI(key)}}` / `{{key}}` → `%s`；`{{page}}` → 1；其余模板变量放弃
2. `@js:` / `<js>` 开头的规则无法静态转换（占失败原因一半）
3. `ruleSearch` 的 CSS 简写转 XPath：`//tag[contains(concat(' ',normalize-space(@class),' '),' cls ')]`
4. `bookList` 有多个 `||` 候选时，**优先选不含 `#id` 的**——搜索页通常没有首页那个 ID 容器

**转化漏斗**（`--pages 10 --limit 200`）：列表 1000 → 候选 911 → 处理 200 → 可转换 70 → 实测通过 7 → 新增 4

### 3.4 固化脚本

**`scripts/import_book_sources.py`**

```bash
python scripts/import_book_sources.py                          # 试跑，只报告不写配置
python scripts/import_book_sources.py --merge                  # 通过实测的写入 seeds.json
python scripts/import_book_sources.py --pages 20 --limit 500 --merge
python scripts/import_book_sources.py --keyword 诡秘之主 --min-results 5
```

流程：抓列表 → 下载书源 JSON → 静态转换 → 去重 → 实测搜索 → 按主域名去重合并。

**内容策略**：不做成人向筛选；仅排除 `bookSourceType != 0` 的非文本书源（漫画/听书/视频），那类书源没有可用于正文的文本。

### 3.5 结果数量上限

`engine/search.py` 的 `uniq[:60]` 是硬上限。`遮天` 已达到 60 条，**继续加站只会让来源更分散，不会让总数变多**。要突破需改这个数字。

---

## 四、书籍解析

### 4.1 只解析出 12 章

**根因**：书籍详情页上有「章节目录」（class=`chapterlist`）和「查看全部章节列表」两个链接指向完整目录，但 `HOP_TEXTS` 白名单里**没有这两个词**，`hop_links()` 匹配不到，于是「章节数 < 30 就跳转完整目录」的分支永不触发。

**修复**：补词，并把匹配从「精确相等」放宽为「短文本（≤16 字）含目录类词」——各站写法差异太大，只靠列举必漏。

### 4.2 同一个站的三种 URL

| URL | 实际结果 |
|---|---|
| `/book/9421323`（无斜杠） | 302 到首页 → 首页推荐位被当成章节列表，**解析出 20 章别的书**（比报错更危险） |
| `/book/9421323.html` | 只有 12 条「最新章节」 |
| `/book/9421323/`（带斜杠） | **473 章完整目录** |

**修复**：URL 规范化——纯数字结尾的 URL 先试补 `/`，失败再回退。

### 4.3 正文抽样过于脆弱

原来首章和中间章任一抓取失败就整本报废。实测中间章偶发 403 就中招（单独抓它又是 200）。

**修复**：首章重试 3 次必须成功（后续正文分页以它为基准），中间章 2 次可选，不足 2 个样本再补 1/4 处——避免单样本判不出正文容器。

### 4.4 目录顺序错乱（首章显示成「第20章」）

**根因**：shyixin.cn 一类站点把「最新章节倒序块」和「正文正序列表」放在**同一个 `ul` 容器**里（前 10 条倒序 + 后 20 条正序）。`drop_latest_block` 的形态判定要求 `min(head) > max(tailn)`，但第一页正文只到第 20 章、预览块恰好也是第 10~20 章——**章号范围重叠**，条件不成立就跳过了。

**修复**：去掉这条过强约束，只保留「前缀递减（≥90%）+ 尾部递增（≥95%）」。

**验证**：一心文学 20 → 430 章，日志「已去除顶部最新章节镜像块 10 条」，首章变回「第1章」。

**注意**：已入库的书不会自动更新，需在书架点「刷新目录(追更)」。

### 4.5 章节内容不全（只有半章）

**根因（三层叠加）**：
1. ddkshu 把每章拆成 `9.html`（第1页）/ `9_2.html`（第2页），页面 `<title>` 里写着「(1)页」「(2)页」
2. 而 `9.html` 上指向 `9_2.html` 的链接，**文案写的是「下一章」**，不是「下一页」
3. `NEXT_TEXTS` 只有「下一页/下页」，续页从未被发现，`enable_content_next` 恒为 0

**修复**：
- 新增 `find_content_next_anchor()`：文案上额外接受「下一章/下章/下一节」，但**跳过 URL 等于真正下一章的链接**——靠这一点区分「同章续页」与「下一章」
- `unique_xpath` 的 class 分支改为**必须恰好匹配 1 个**才采用，否则退回绝对路径（原来 `//a[@class="btn btn-default"]` 会匹配 4 个，第一个还是「目录」）
- `gateway.fetch_chapter` 改用新函数并传入下一章地址——**这步是关键**，否则规则对了、抓取时仍只看「下一页」

**效果**：

| 章节 | 修复前 | 修复后 |
|---|---|---|
| 第 100 章 | ~700 字 | **1385 字** |
| 第 501 章 | 1017 字 | **2185 字** |
| 第 1859 章 | 548 字 | **1137 字** |

**注意**：已缓存的前 22 章仍是半章文本，需在书架点「清缓存」后重读。

---

## 五、Reader 网关

### 5.1 「http 状态码错误」= 400 Bad Request

**现象**：Reader 搜索能出结果，点进书籍时报「http 状态码错误。可能书源网站已更新。需要更新书源配置。」，进度 `0.00% (9378/0)`。

**根因**：`render_search_page()` 生成的书籍链接带 `?src=中文站点名`，中文未做 percent-encode。Reader 把 href 原样保存（`.online/*.ol` 里可见原始中文），请求时直接写进 HTTP 请求行 → uvicorn/h11 拒绝非 ASCII 请求行 → **400**。

**证据**：
1. Reader.exe 内三类错误文案：`网络请求失败` / `http状态码错误` / `xpath提取失败`，后者专指非 200
2. 原始 socket 对同一路径四种写法：编码后 200、未编码 UTF-8 400、未编码 GBK 400、不带参数 200
3. 交叉验证：失败的两次请求对应的书在服务端均为**首次解析**，证明请求从未进入服务端——400 在协议层即被拒
4. 决定性对照：Reader 已存的 5 条记录中，`?src=ddkshu.com`、`?src=shyixin.cn`（ASCII）全部成功，`?src=笔趣阁tw`、`?src=思兔阅读`（中文）全部失败

**为什么搜索正常、点进去失败**：`query_url` 的 `%s` 由 Reader 自己替换并编码；而书籍链接是服务端返回的 href，Reader 原样使用、不做二次编码。

**修复**：`href='%s/g/book/%s'`，不再拼 `?src=`。站点名仍由页面里的 `bh-site` 单独展示，信息不丢。

**兼容性**：旧链接无法在服务端兼容——400 发生在 h11 解析请求行阶段，进不了路由。Reader 里带中文 `?src=` 的旧记录必须删掉重新搜索；`?src=域名` 的旧记录不受影响。

---

## 六、健康体检

### 6.1 中/末章显示 0 字

**根因**：`if text is None and idx == 0:` —— 只有首章没缓存才真抓，中/末章纯读缓存。实测这三本书的中/末章其实都能抓（0.2~0.7s/章），纯粹是没去抓。

**修复**：去掉 `and idx == 0`，三章都实抓；中/末章抓失败仍不参与整体判定（避免一次网络抖动把整本判红）。前端 `len === 0` 时显示灰色「未取到」而不是「0字 ✓」。

**验证**：体检 3/3 健康，九次抽检全部有字数。

### 6.2 站点误判「不可达」+ 三章全未取到

**根因**：`_req_quick` 原来只做一次 L1 请求，失败直接返回 None，**完全不升级 L2**。而笔趣阁tw 的 L1 对 Cloudflare 是稳定 403。

| 模式 | 目录页 | 章节页 | 层统计 |
|---|---|---|---|
| quick（体检用） | None | None | L1/L2/L3 全 0 |
| 完整（搜索/解析用） | 200 | 200 | **L2=2** |

**修复**：`_req_quick` 的 L1 失败后再用 cloudscraper 试一次（仅 GET，不走 L3）。

**验证**：体检 1/1 健康，站点可达 True，三章 1678 / 1334 / 1825 字。

**教训**：既要快又要能过防护的场景，至少要保留一次 L2。

---

## 七、可观测性

`server.log` 原先相关代码是**死代码**——有 1MB 轮转逻辑，但从没写过文件。

### 三层埋点
1. `server._log_search()`：一行摘要 + 一行阶段耗时，`source=api|reader|stream` 区分入口
2. `engine/search.aggregate_search(..., stats=dict)`：回填 `seed` / `sites` / `total`
3. `engine/websearch.web_discover(..., stats=dict)`：回填 `bing` / `baidu` / `verify`

`stats` 全是可选参数，不传则零开销。

### 日志落地
`server._log_line()`：带时间戳双写控制台 + `server.log`（`_LOG_LOCK` 保护，>1MB 轮转到 `.old`）。`_job_log` 也改走它，所以解析、txt 导出、体检日志现在同样落盘。

### 埋点上线即抓到的真凶
```
[search:api] '遮天' -> 18 条 / 0 错误 / 总 48.4s
[search:api]   └ 种子站 45.2s (平凡文学 45.2s, 笔趣阁tw 1.4s, 大米小说 1.0s) | Bing 0.2s | 百度 0.8s | 预验 2.2s
```
**平凡文学单站 45.16s，占总耗时 94%**。该站域名已失效且是 POST，必须走完整重试链才放弃。停掉它之后 48.4s → 3.3s。

---

## 八、流式搜索（SSE）

### 后端 `GET /api/search/stream`
- `StreamingResponse(media_type="text/event-stream")` + 同步生成器，Starlette 会在线程池里迭代
- 种子站与发现层放进同一个 `ThreadPoolExecutor`，用 `as_completed` 按完成顺序 yield——Bing 先返回就先推送
- 事件协议：`batch`（site/src/items/elapsed）、`status`、`done`（total/elapsed）
- `as_completed(timeout=45)` + `shutdown(wait=False)`：慢站不阻塞响应
- 旧端点 `/api/search` 保留（Reader 网关需要完整 HTML 页面，不能流式）

### 前端 `static/index.html`
`doSearch()` 从 `fetch` 改为 `EventSource`：建三个容器，收到 batch 就 `insertAdjacentHTML` 追加（不整屏重绘，避免闪烁）；`seen` Set 按 id 去重；`searchES` 全局变量，重复搜索先 close 上一条防止流泄漏。

### 验证
```
 1.1s batch  笔趣阁tw   17 条  累计 17
 3.3s batch  全网发现    5 条  累计 22
 3.3s done   合计 22 条
```

---

## 九、性能优化：26s → 11s

### 结果
| 关键词 | 改动前 | 改动后 |
|---|---|---|
| 大日金乌纵横诸天 | 26.2s | **11.7 / 10.8 / 11.2s**（连测三次），6 条 |
| 遮天 | — | **6.0s**，60 条 |

### 9.1 预验关闭 L2 + 超时 8→5（预验阶段 15.0s → 5.0s）

**关键发现**：把预验并发从 4 提到 8，耗时**一点没变**（仍 14.9s）。逐条计时后发现：每个候选只需 0.0s 返回，整批却等 14.6s——说明全卡在同一点上。

真正的瓶颈在 `_req_quick`：L1 失败后调 `cloudscraper.create_scraper()`，而 `self._scraper` 是共享实例且**创建过程无锁**，9 个候选并发进来各建一次，互相争抢。

**对照实验**（同一批 9 个百度候选）：

| 配置 | 通过数 | 耗时 |
|---|---|---|
| 关闭 L2 | 2/9 | **9.8s** |
| 保留 L2 | 2/9 | 22.2s |

**通过集合差集为 0——一条结果都没丢。**

`_req_quick` / `_req` 新增 `allow_l2=True, timeout=8` 两个可选参数，体检等其它 quick 调用保持默认（仍走 L2）。

### 9.2 Bing 变体间隔 2.5 → 1.2 秒（省 2.6s）

冷门书三个变体要全跑，原来光等待就花 5 秒。实测 1.2 秒不会触发限流。L3 保持 1.5。

### 9.3 发现层与种子站并行（省约 3s）

原来总时长 = 种子 + 发现层。现在用独立单线程池先提交 `_web_discover_safe`，再跑种子站，最后 `web_fut.result()`，总时长变成两者较大值。异常收敛在 `_web_discover_safe` 内部返回 `(结果, 错误串)`，不外抛。

### 剩余空间
整次搜索由发现层决定（Bing 5.4 + 百度 1.2 + 预验 5.0 = 11.6s）。预验那 5 秒已是百度跳转链 L1 请求的下限——请求本身要重定向到真实书站才能拿到标题，再降 timeout 就会开始丢结果。

---

## 十、数据维护

### 清理 Reader 空壳记录
`.online/*.ol` 只是书籍数据文件，**是否显示由 `.settings.json` 的 `books[]` 决定**。两条失败记录从未登记进 `books[]`，属于孤儿文件，界面上看不到。

清理方式：Python `ctypes` 调 `shell32.SHFileOperationW`（FO_DELETE + FOF_ALLOWUNDO）移入回收站。
> PowerShell 的 `Add-Type` / `Reflection.Assembly` 在本机被安全策略拦截，`Remove-Item` 又不进回收站。
> 返回值 rc=2 但文件确实已移走，不要据此判失败，以 `os.path.exists` 为准。

### 清理测试入库的书
`POST /api/delete/{bid}` 会同步清理内存、`data/books/{bid}.json` 与 `data/cache/{bid}`。

### 备份位置
`D:\日常软件\摸鱼电子书\_bookhub_backup_20261103\`（5 个 json + 4 个 ol）

---

## 附录 A：改动文件清单

| 文件 | 改动内容 |
|---|---|
| `server.py` | 端口配置与双监听、`gw_base()` 动态地址、书源同步、`_log_line`/`_log_search`、SSE 端点、体检三章实抓 |
| `engine/search.py` | `load_seeds` 过滤 disabled、`_fold_cjk` 繁简归一、`_relevance` 归一、`aggregate_search` 耗时埋点与并行发现层、`_search_site` 计时 |
| `engine/websearch.py` | `baidu_search`、`_looks_mojibake` 配套、黑名单扩充、`_verify` 快速模式、预验并发与 L2 策略、Bing 变体间隔 |
| `engine/fetcher.py` | `_req_quick` 补 L2、新增 `allow_l2` / `timeout` 参数 |
| `engine/gateway.py` | `render_search_page` 去 `?src=`、`fetch_chapter` 用 `find_content_next_anchor` |
| `autobs/autobs.py` | `parse_doc` 编码兜底、`HOP_TEXTS` 补词与放宽、URL 规范化、正文抽样容错、`drop_latest_block` 放宽、`find_content_next_anchor`、`unique_xpath` 唯一性 |
| `engine/seeds.json` | 10 个启用 + 4 个停用（含 `disabled_reason`） |
| `scripts/import_book_sources.py` | 新增，书源仓库批量导入 |
| `static/index.html` | EventSource 流式渲染、体检「未取到」显示 |
| `config.example.json` / `bookhub.bat` / `README.md` / `.gitignore` / `requirements.txt` | 端口与依赖同步 |

---

## 附录 B：已知限制与待办

1. ~~**结果上限 60 条**（`uniq[:60]`）：站点再多也不会增加总数，只会分散来源。~~
   已解决（2026-11-05）：`max_results` 可配，端点支持 `?limit=`（≤300），网页台打满上限时给「显示更多」。
2. **多页章节合并上限**：速读谷有章标题写「1 / 9」，但 `verify_content_pagination` 与 `fetch_chapter` 的 hops 上限是 6/8 页，9 页的章尾部可能仍缺。
3. **禁忌书屋价值有限**：cool18.com/bbs4 是论坛结构，单独测试能返回 16 条，完整搜索里贡献 0 条。
4. **已入库的书不会自动更新**：解析规则改进后，需手动「刷新目录(追更)」或「清缓存」。
5. **Reader 旧记录需重搜**：带中文 `?src=` 的书必须删掉重新搜索，服务端无法兼容。
6. **端口探测竞争窗口**：探测与实际 bind 之间有毫秒级间隙，失败只记日志不重试。

---

## 附录 C：排查经验

1. **端口类验证先确认无监听**。测试进程 `terminate` 在 Windows 下不够，端口被旧进程占着会让新进程起不来，HTTP 请求全打到旧代码上——这个坑卡了整整一轮排查。收尾用 `taskkill /PID <pid> /T /F`，再 bind 验证已释放。
2. **本机有系统代理** `HTTP_PROXY=127.0.0.1:2427`，测本地接口需显式 `proxies=None`。
3. **「请求成功但结果为空」优先怀疑编码**。errors 为空恰恰说明请求本身没问题，问题在解析后的过滤环节。
4. **性能问题先分段计时，不要猜**。这次预验的瓶颈就与直觉相反：加并发无效，真凶是一个无锁的共享对象初始化。

---

# 2026-11-05：优化批次（OPTIMIZATION.md 14 项落地）

> 对应 `OPTIMIZATION.md`（14 项风险清单）与 `FIXES.md`（逐项修复记录 FIX-01~FIX-16）
> 记录原则同上：问题 → 根因 → 改动 → 验证

## 摘要

| 批次 | 项 | 核心问题 | 结果 |
|---|---|---|---|
| A 数据正确性 | §1-§3 | 种子沉淀死代码、缓存非原子写、半章被当完整章 | Reader 打开的书开始沉淀种子；半章不再永久化 |
| B 吞吐 | §4-§6 | 全局 0.6s 节流、L3 单实例、体检串行 | 10 站搜索 6.0s → 约 1.6s；体检 4 路并发 |
| C 容量 | §7-§10 | 缓存无上限、JOBS 不回收、缓存键两处各算 | 配额 500MB + 启动清理；JOBS TTL/上限；键统一 |
| D 行为 | §11-§14 | 水印误杀数字行、60 条硬截断、SSE 超时耦合 | 数字行按位置判；结果可配 + 显示更多；预算解耦 |

## 一、数据正确性（P0）

1. **种子沉淀是死代码**（`engine/gateway.py:238-250`）：`from search import ...` 在包导入下必然 `ImportError`，又被 `except Exception: pass` 吞掉。改为先按 `engine.*` 包名导入、失败回退裸名，外层改为打印异常。后果修正：**通过 Reader 打开的书现在也会沉淀种子**，种子库恢复自动增长。
2. **章节缓存非原子写**（`engine/gateway.py:335-345`）：`open(...,"w").write()` 中途被杀就留半截文件，而读取端「存在即返回」。改为 `.tmp` + `os.replace`。
3. **半章被当完整章缓存**（`engine/gateway.py:301-334`）：正文分页任一页失败仍拼接已抓部分并落缓存。改为 `interrupted` 标志——照常返回给读者，但**不落缓存**，下次重读自愈；读取端另加 <50 字脏数据兜底删除。
4. **`parse_doc` 只探 title 判乱码**（`autobs/autobs.py:231-245`）：无 title 或 title 为 ASCII 的中文页整体按 Latin-1 误解，「下一页」文案变 mojibake → 正文分页静默失效，每章只抓第一页。改为追加正文前 5 个文本节点再判。
5. **`drop_latest_block` 多删 1~2 章**（`autobs/autobs.py:1580-1607`）：从大到小扫边界 + 0.9 递减容差，k 越过真实边界仍成立。改为从小到大扫 + 前缀严格递减 + 边界掉头 + 尾部递增 0.98。

## 二、吞吐（P1）

6. **全局 0.6s 节流**（`engine/fetcher.py:156-170`）：10 站并发被同一道闸门压成 6.0s，与「遮天 6.0s / 60 条」完全吻合。改为按 host 计时 + 先占位再睡（同站仍 ≥0.6s，跨站互不拖累）。
7. **L3 单实例 + 全局锁**（`engine/fetcher.py:40-131`）：单次最坏 35s，串行化所有浏览器请求。改为 2 个标签页的池 + 等挑战硬上限 6s，取不到 tab 直接失败交给熔断。
8. **体检逐本串行**（`server.py:589-654`）：10 本 = 40 个串行请求。改为 `ThreadPoolExecutor(4)` + 单本异常隔离 + 结果按书架顺序重排。

## 三、容量与可观测（P2）

9. **缓存无上限**：实测 8.5KB/章，1859 章 ≈ 16MB/本。新增 `cache_stats()` / `prune_cache()`、配置 `cache_max_mb`（默认 500）、启动时清理、接口 `GET /api/cache/stats`。
10. **JOBS 永不回收**：加 `JOBS_TTL=1800` / `JOBS_MAX=20` / `_finish()` 统一打 `finished_at`。注意 `_prune_jobs(reserve=1)`——插入前裁到 19，插入后正好 20。
11. **缓存键两处各算一遍**：抽出 `gateway.cache_path(bid, url)`，体检与 `fetch_chapter` 共用。
12. **autobs 不是包**：新增 `autobs/__init__.py`，删除 `server.py` 指向不存在目录的死 `sys.path`。

## 四、行为修正（P3）

13. **水印误杀**（`autobs/autobs.py:1404-1450`）：`^\d{1,4}$` 与 `#.*#.*#` 会删掉正文里的数字行与含 # 的对白。改为裸数字仅首尾 3 行删、`#` 行需命中分隔线形态或水印语义。
14. **结果 60 条硬截断**：`MAX_RESULTS` + `max_results` 参数 + 端点 `limit`（≤300）+ 网页台「显示更多」（打满上限才出现）。
15. **SSE 超时耦合**（`server.py:400-440`）：`as_completed(timeout=45)` 会连已算完的结果一起丢。改为 `wait(FIRST_COMPLETED)` 轮询 + `SSE_TOTAL_BUDGET=60`，谁先回谁先推。

## 验证

`python -m pytest tests/ -q` → **81 passed, 3 skipped**。新增测试：
`tests/test_gateway_cache.py`(4)、`tests/test_fetcher_throttle.py`(3)、`tests/test_browser_pool.py`(3)、`tests/test_cache_quota.py`(3)、`tests/test_server_jobs.py`(4)，`tests/test_units.py` 追加 4 条水印用例。

---

## 2026-11-05 补丁：体检诊断与 `_refreshing` 残留

> 触发：用户实测体检，《从鹰人进化为天使后，我荡平诸神》显示「站点不可达」，但同时亮绿灯。

### 一、体检只会说「站点不可达」（FIX-17）

**问题**：探活只有一个布尔（`fetcher.get(url, quick=True)` 返回 None 即不可达）。四种完全不同的病在界面上长得一模一样：Cloudflare 硬拦截、Cloudflare 人机挑战、连接超时、HTTP 404。

**实测**：`https://www.ddkshu.com/dk-416895.html` 裸请求 403 / 4545 字节，响应头 `server: cloudflare`、`cf-ray: ...-LAX`，正文 `<title>Attention Required! | Cloudflare</title>` —— 是**本机出口 IP 被 CF 挡**，站点本身没挂。对照组 `biquge.tw` 同样 L1 恒 403，但 L2 稳定 200。只看布尔值，这两本书会被判成同一种病。

**改动**
- `engine/fetcher.py:274-325` 新增 `diagnose(url)`，返回 `{"ok", "code", "reason"}`：裸 L1 先拿状态码；`requests.Timeout` 直接判「连接超时」不走 L2（省 20s）；其余给 L2 一次机会，过了报「L1 被挡，L2 兜底通过」；仍不过按特征页分类为 CF 硬拦截 / CF 人机挑战 / 403 / 404 / 防护页。
- `server.py:589-660`：`entry["alive_reason"]` 存原因；`alive=False` 时中/末章不再实抓（没缓存标 `skipped: "站点不可达，未抽查"`），避免把站点故障误报成章节故障，也省掉 3 次超时。
- 三态灯 `entry["level"]`：`ok` 绿 / `warn` 黄（站点当前不可达，但目录与缓存还在，已缓存章仍可读）/ `bad` 红（目录或首章坏了）。
- `static/index.html`：🟡 黄灯 + 「站点不可达：Cloudflare 硬拦截（403，本机 IP 被挡）」；汇总改为「N/M 本健康，K 本站点暂不可达（缓存仍可读）」。

### 二、`_refreshing` 被写进磁盘（FIX-18）

**问题**：`get_catalog` 触发后台刷新前会置 `b["_refreshing"] = True`，`finally` 里复位。刷新途中进程被杀（关 Reader、Ctrl+C）时 `_persist` 已把这个字段写进 `data/books/{id}.json`，重启后被当成真的读回来 —— 于是这本书的目录**再也不会自动追更**，界面上看不出任何异常。实测 `6e31a7d02c.json` 里就残留着 `"_refreshing": true`。

**改动**：`_persist` 落盘前过滤下划线开头的运行时键；加载时 `b.pop("_refreshing", None)`，已经写脏的旧文件也能自愈。

### 验证

`python -m pytest tests/ -q` → **89 passed, 3 skipped**。新增 `tests/test_health_diagnose.py`(6)，`tests/test_gateway_cache.py` 追加 2 条。

真网复跑体检（2 本）：

| 书 | alive | 原因 | 灯 | 目录 | 抽查 |
|---|---|---|---|---|---|
| 大日金乌纵横诸天 | True | L1 被挡，L2 兜底通过 | 🟢 ok | 473 | 1678 / 1334 / 1825 字 |
| 从鹰人进化为天使后，我荡平诸神 | False | Cloudflare 硬拦截（403，本机 IP 被挡） | 🟡 warn | 1859 | 首章 2492 字；中/末章「站点不可达，未抽查」 |

> 注：`ddkshu.com` 需要换出口 IP 或换源，不是解析问题。缓存里已有的章节照常可读。
