# bookhub — Reader 的全能找书后台

给 [Reader](https://github.com/binbyu/Reader)（binbyu 版，Windows 开源阅读器）配的本地找书服务：
**像网页搜索一样找书，任何网站（包括有 Cloudflare 防护的）都能在 Reader 里直接读**，
不再手工配置书源。

| 你以前的痛 | bookhub 之后 |
| --- | --- |
| 每个站要手工配置 xpath 书源 | 全自动解析，点一下就行 |
| 书站挂了书源全废 | 每次搜索实时全网找（Bing 发现层） |
| 部分站有防护，Reader 打开报错 | 防护在后台被消化，Reader 无感知 |
| 新书要去新网站，又要配一遍 | 用得越久种子站越多（自动沉淀） |

## 工作原理

```
浏览器 / Reader 搜索框
        │
        ▼
┌─ bookhub（本地服务，127.0.0.1）──────────────┐
│ ① 种子站直搜（并发）                          │
│ ② Bing 发现层：全网搜"书名"，验证真实链接      │
│ ③ 自动解析：章节/正文/分页全自动识别           │
│    抓取分层：浏览器UA → cloudscraper →        │
│              真浏览器（过 Cloudflare 等防护）  │
└──────────────┬───────────────────────────────┘
               ▼
   Reader（网关书源，搜索框直搜直读）
   或 txt 整本导出（完全离线）
```

Reader 不需要任何修改——只需导入一次本目录的 `bs_gateway.json` 书源，
Reader 的搜索框从此搜到的是 bookhub 聚合的全部来源。

## 环境要求

- Windows + [Reader](https://github.com/binbyu/Reader)（binbyu 版）
- Python 3.8+（安装时勾选 "Add to PATH"）
- 本机 8787 端口（网页台，被占用会自动顺延）
- 80 端口空闲更省事：网关优先抢 80，Reader 对无端口地址兼容性最好。
  80 被占用时网关自动并入主端口，在线搜书需重新导入书源，txt 导出不受影响
- 可选：Chrome 或 Edge（少数强防护站点需要真浏览器兜底）

## 安装

```bash
git clone https://github.com/<你的用户名>/reader-bookhub.git
cd reader-bookhub
pip install -r requirements.txt
```
或直接下载 ZIP 解压后进入目录，效果相同。

## 快速开始（首次 5 分钟）

1. **启动服务**：双击 `bookhub.bat`（再双击一次 = 停止，完全由你控制）
2. **导入网关书源**：打开 Reader → 设置 → 在线小说 → 书源管理 → 导入 →
   选择 `bs_gateway.json`（只需这一次）。建议顺手删掉其他旧书源，
   Reader 重启后默认选中第一个源
3. **开读**：Reader 在线小说 → 搜索框输入书名 → 双击结果

> 网页管理台：服务启动后浏览器打开 http://127.0.0.1:8787
> （找书、看解析进度、书架管理、导出 txt、清理缓存）
> 启动日志会同时打印 Reader 网关的实际地址（80 端口可用时为
> http://127.0.0.1 ，被占用时会变成带端口的地址）。

## 日常使用

| 场景 | 操作 |
| --- | --- |
| 找新书 | Reader 搜索框直接搜（或网页台搜索，可看解析进度） |
| 追更 | Reader 里按 F5，或网页台书架点「刷新目录」 |
| 离线读 | 书架点「导出/更新 txt」→ 自动落盘到 `config.json` 配置的目录 |
| 缓存太大 | 书架点「清缓存」（书架和进度保留） |
| 不想用了 | 再双击 `bookhub.bat` 停止；已缓存章节和 txt 不受影响 |

## 配置（config.json，首次运行自动生成）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `port` | 8787 | 网页台主端口；被占用时自动顺延到 8788、8789… |
| `gateway_port` | 80 | Reader 网关优先端口；被占用则网关并入 `port`，书源自动改写 |
| `reader_dir` | ./books | txt 导出目录，改成你的 Reader 书目录 |
| `host` | 127.0.0.1 | 只监听本机，外部无法访问 |
| `headless` | true | L3 兜底浏览器是否无头 |
| `max_results` | 60 | 搜索聚合结果上限；网页台可用 `?limit=` 临时覆盖（≤300），打满时底部给「显示更多」 |
| `cache_max_mb` | 500 | 章节缓存总配额（MB）；超了启动时按最久未读整本清（书架与进度不动） |

模板见 `config.example.json`。

`bs_gateway.json` 每次启动都会按实际端口自动对齐：只改写 `host` 与
`query_url`，xpath 等其余字段原样保留，改写前留一份 `.bak`。
因此**改过端口后需要在 Reader 里重新导入一次书源**。

## 数据存储

全部在本目录内、人可读格式、零外部数据库：

```
data/
├── books/{书id}.json   书架（一书一文件，原子写入）
├── cache/{书id}/       章节正文缓存（按章节 URL 哈希命名，
│                       目录变化不会串章；可按书清理）
└── books.json.migrated 旧版数据迁移备份
engine/seeds.json        搜索种子站（发现新站自动沉淀，原子写入）
server.log               运行日志（>1MB 自动轮转）
```

以上运行数据均在 `.gitignore` 中。

## 常见问题

**Reader 提示"网络请求失败"**
→ bookhub 没启动（双击 bat）；或启动日志显示网关已并入主端口（80 被占），
而 Reader 里还是旧书源——重新导入一次 `bs_gateway.json` 即可。

**搜索结果里没有某本书**
→ 搜索引擎尚未收录（新书常见），过段时间再试；或换关键词（去掉序号/
符号）；或在网页台直接粘贴书籍目录页网址解析。

**某本书读着读着失败**
→ 源站临时波动或改版。网页台书架点「刷新目录」重解析；不行就重新搜索
换一个来源。

**网页发现层总是"无结果"**
→ Bing 反爬波动，工具会自动用真浏览器重试；偶尔彻底失败属正常，
种子站结果不受影响。

**和系统代理的关系**
→ 工具对书站请求强制直连（绕过系统代理），代理软件开着也不影响。

## 已知限制

- Cloudflare Turnstile（人机点选验证）站点无法自动通过，换源即可（罕见）
- 晋江等站的 VIP 付费章节无法获取——请支持正版
- 搜索引擎索引有滞后：刚发布几天的书可能全网都搜不到
- 仅支持 Windows（跟随 Reader）；书站结构千变万化，个别站可能解析失败

## 目录结构

```
bookhub/
├── server.py            主服务（FastAPI）
├── bookhub.bat          启动/停止开关（GBK 编码，改动请保持）
├── bs_gateway.json      网关书源（导入 Reader 一次）
├── requirements.txt     依赖
├── engine/
│   ├── fetcher.py       分层抓取（requests→cloudscraper→真浏览器）
│   ├── search.py        种子站聚合 + 噪音治理 + 自动沉淀
│   ├── websearch.py     Bing 发现层（黑名单过滤+预验）
│   ├── gateway.py       书库存储 + Reader 网关页面
│   └── seeds.json       种子站配置
├── static/index.html    网页管理台
└── data/                运行数据（gitignore）
```

解析引擎来自同仓库的 `autobs/`（独立 CLI 工具，可单独使用：
`python autobs.py "书籍目录页URL"` 一条命令生成 Reader 书源）。

## 免责声明

本工具只做网页解析与个人阅读用途的数据转发，不存储、不分发任何作品
内容；所有内容来自第三方公开网站，版权归原网站及作者所有。请支持正版。
