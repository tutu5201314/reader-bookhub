# -*- coding: utf-8 -*-
"""bookhub 主服务：找书 Web UI + 聚合搜索 + 解析 + txt 导出 + Reader 网关。

启动: python server.py   （默认 http://127.0.0.1:8787）
"""
import json
import os
import re
import socket
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                wait, FIRST_COMPLETED)
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from engine.fetcher import LayeredFetcher
from engine.search import aggregate_search, load_seeds, _search_site, _relevance
from engine import gateway as gw
from autobs import resolve_book

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------- 配置 ----------------
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DEF_CONFIG = {
    "host": "127.0.0.1",
    # 主端口：网页台 + 全部 API。默认 8787，被占用时自动向后顺延。
    "port": 8787,
    # 网关端口：优先给 Reader 用（Reader 的网络库对带端口的 URL 兼容性差）。
    # 该端口空闲则额外起一个监听；被占用则自动降级，Reader 改走主端口地址。
    "gateway_port": 80,
    "reader_dir": os.path.join(BASE_DIR, "books"),  # txt 导出目录，可在 config.json 改
    "headless": True,
    # 搜索结果条数上限。10 个种子站已能把常见书名打满 60 条，需要更全时调高
    # （网页台还可以用 ?limit= 临时覆盖，Reader 网关那条路仍按 60 渲染）。
    "max_results": 60,
    # 章节缓存总配额（MB）。实测约 8.5KB/章，1859 章的书全缓存 ≈ 16MB。
    # 超了就在启动时按「最久没读过」整本清掉（书架和阅读进度不动）。
    "cache_max_mb": 500,
}
config = dict(DEF_CONFIG)
if os.path.exists(CONFIG_PATH):
    try:
        config.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
    except Exception:
        pass
else:
    # 首次运行生成配置（已存在则不覆盖用户配置）
    with open(CONFIG_PATH, "w", encoding="utf-8") as fp:
        json.dump(config, fp, ensure_ascii=False, indent=2)

# SSE 流式搜索的总时长预算（秒）。首批结果靠「谁先回谁先推」保证（通常 1~3s），
# 这个预算只管收尾：慢站（历史最慢 45s）不再一刀切掉已经算完的结果。
SSE_TOTAL_BUDGET = 60

# 运行日志轮转：超 1MB 截断，防止无限增长
LOG_PATH = os.path.join(BASE_DIR, "server.log")
try:
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
        os.replace(LOG_PATH, LOG_PATH + ".old")
except Exception:
    pass

_LOG_LOCK = threading.Lock()


def _log_line(msg):
    """统一日志出口：带时间戳，同时写控制台与 server.log。
    搜索是长耗时链路，落到文件才能事后复盘，光打控制台窗口一关就没了。"""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with _LOG_LOCK:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
                os.replace(LOG_PATH, LOG_PATH + ".old")
            with open(LOG_PATH, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
    except Exception:
        pass


def _log_search(keyword, n_results, n_errors, total, stats, source="api"):
    """搜索日志：一行摘要 + 一行阶段耗时。

    摘要回答「这次搜到了什么」，阶段耗时回答「时间花在哪」——
    种子站并发 / Bing / 百度兜底 / 候选预验，哪一段慢一眼能看到。"""
    _log_line("[search:%s] %r -> %d 条 / %d 错误 / 总 %.1fs"
              % (source, keyword, n_results, n_errors, total))
    sites = stats.get("sites") or {}
    site_txt = ""
    if sites:
        parts = ["%s %.1fs" % (k, v)
                 for k, v in sorted(sites.items(), key=lambda kv: -kv[1])
                 if v >= 0.05]
        if parts:
            site_txt = " (%s)" % ", ".join(parts)
    # 流式搜索里种子站与发现层并发，"种子站总耗时"没有意义，缺省显示 -
    seed_txt = ("%.1fs" % stats["seed"]) if "seed" in stats else "-"
    _log_line("[search:%s]   └ 种子站 %s%s | Bing %.1fs | 百度 %.1fs | 预验 %.1fs"
              % (source, seed_txt, site_txt,
                 stats.get("bing", 0.0), stats.get("baidu", 0.0),
                 stats.get("verify", 0.0)))

def _fmt_base(port):
    """端口为 80 时省略端口号（Reader 对无端口地址兼容性最好），否则显式带上。"""
    return ("http://%s" % config["host"]) if port == 80 \
        else ("http://%s:%d" % (config["host"], port))


# 网关对外地址：启动时按 gateway_port 的实际可用性确定（见文件末尾 __main__）
GW_PRIMARY = _fmt_base(config["port"])


def gw_base(req=None):
    """生成网关绝对地址。

    有请求上下文时以请求实际 Host 为准——同一进程可能同时监听 80 与 8787，
    按请求生成才能保证页面里的链接指回用户正在访问的那个端口。"""
    if req is not None:
        try:
            return str(req.base_url).rstrip("/")
        except Exception:
            pass
    return GW_PRIMARY


def _port_available(host, port):
    """探测端口能否本地绑定。不设 SO_REUSEADDR：Windows 下该选项会
    让已被占用的端口仍返回绑定成功，导致误判。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pick_port(host, start, tries=20):
    """从 start 起找一个可绑定的端口，避免端口被占就整个服务起不来。"""
    for p in range(start, start + tries):
        if _port_available(host, p):
            return p
    return start  # 全不可用则交回原值，让 uvicorn 报出真实错误


# 网关书源兜底模板：仅当 bs_gateway.json 缺失或损坏时使用
_BS_TEMPLATE = {
    "book_sources": [{
        "title": "bookhub网关",
        "host": "",
        "query_url": "",
        "query_method": 0,
        "query_params": "",
        "query_charset": 1,
        "book_name_xpath": "//a[@class='bh-name']",
        "book_mainpage_xpath": "//a[@class='bh-name']/@href",
        "book_author_xpath": "//span[@class='bh-author']",
        "enable_chapter_page": 0,
        "chapter_page_xpath": "",
        "chapter_title_xpath": "//a[@class='bh-chapter']",
        "chapter_url_xpath": "//a[@class='bh-chapter']/@href",
        "enable_chapter_next": 0,
        "chapter_next_url_xpath": "",
        "chapter_next_keyword_xpath": "",
        "chapter_next_keyword": "",
        "content_xpath": "//div[@id='bh-content']",
        "enable_content_next": 0,
        "content_next_url_xpath": "",
        "content_next_keyword_xpath": "",
        "content_next_keyword": "",
        "content_filter_type": 0,
        "content_filter_keyword": "",
    }]
}


def _sync_gateway_source(base):
    """让 bs_gateway.json 的地址跟随实际端口。

    只改写 host / query_url 两个字段，用户调整过的 xpath 等其余配置原样保留；
    端口没变就不触碰文件。改写前把原文件留一份 .bak。"""
    path = os.path.join(BASE_DIR, "bs_gateway.json")
    data = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception:
            data = None
    if not isinstance(data, dict) or not data.get("book_sources"):
        data = json.loads(json.dumps(_BS_TEMPLATE))
    want_query = base + "/g/search?q=%s"
    changed = False
    for src in data["book_sources"]:
        if src.get("host") != base:
            src["host"] = base
            changed = True
        if src.get("query_url") != want_query:
            src["query_url"] = want_query
            changed = True
    if changed:
        try:
            if os.path.exists(path):
                os.replace(path, path + ".bak")
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            print("[bookhub] 书源文件更新失败：%s" % e)
    return path

app = FastAPI(title="bookhub")
fetcher = LayeredFetcher(debug=False, headless=config.get("headless", True))
store = gw.BookStore()

# ---------------- 后台任务 ----------------
JOBS = {}
JOBS_LOCK = threading.Lock()
JOBS_TTL = 1800      # 已完成的任务保留 30 分钟
JOBS_MAX = 20        # 最多同时留 20 条记录（挂一整天也不会涨）


def _prune_jobs(now=None, reserve=0):
    """回收已结束的任务记录：先按时间过期，再按条数裁到 JOBS_MAX。

    reserve：调用方马上要新增几条，先给它留出位置（否则插完会多出 reserve 条）。
    跑着的任务（status == "running"）一律不动——那是正在进行的工作。
    """
    now = now or time.time()
    cap = max(1, JOBS_MAX - int(reserve))
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items()
                    if v.get("status") != "running"
                    and now - v.get("finished_at", v.get("created", now)) > JOBS_TTL]:
            JOBS.pop(jid, None)
        if len(JOBS) > cap:
            old = sorted((v.get("finished_at", v.get("created", 0)), j)
                         for j, v in JOBS.items() if v.get("status") != "running")
            for _, jid in old[:len(JOBS) - cap]:
                JOBS.pop(jid, None)


def _new_job(kind):
    _prune_jobs(reserve=1)
    jid = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "status": "running",
                     "progress": 0, "log": [], "result": None, "created": time.time()}
    return JOBS[jid]


def _finish(job, status):
    """标记任务结束（顺带记完成时间，供 _prune_jobs 回收）。"""
    job["status"] = status
    job["finished_at"] = time.time()


def _job_log(job, msg):
    if job is not None:
        job["log"].append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))
    _log_line("[bookhub] %s" % msg)


def _run_resolve_job(job, bid, url, want_txt):
    try:
        b, err = store.ensure_resolved(bid, fetcher, force=True)
        if err:
            _finish(job, "failed")
            job["result"] = {"error": err}
            return
        job["progress"] = 60
        _job_log(job, "解析完成：%d 章，Reader直连：%s" % (
            b["chapter_count"], "可" if b.get("reader_ok") else "不可（走网关/txt）"))
        out = {"book": _book_info(b), "gateway_url": "%s/g/book/%s" % (gw_base(), bid),
               "reader_ok": b.get("reader_ok")}
        # 生成独立书源文件（可直连站直接导入 Reader 用）
        bs_path = os.path.join(BASE_DIR, "out", "bs_%s.json" % re.sub(r"[^\w]+", "_", b.get("site") or bid))
        os.makedirs(os.path.dirname(bs_path), exist_ok=True)
        with open(bs_path, "w", encoding="utf-8") as fp:
            json.dump({"book_sources": [b["src"]]}, fp, ensure_ascii=False, indent=2)
        out["bs_file"] = bs_path
        job["result"] = out
        job["progress"] = 80
        # 动态沉淀：新站站内搜索可用则加入种子，下次直搜覆盖
        try:
            from engine.search import maybe_add_seed
            new_seed = maybe_add_seed(b.get("src"), fetcher)
            if new_seed:
                _job_log(job, "已把「%s」加入搜索种子（下次可直接搜到）" % new_seed["name"])
        except Exception:
            pass
        if want_txt:
            txt_path = _export_txt(b, job)
            out["txt_file"] = txt_path
        _finish(job, "done")
        job["progress"] = 100
    except Exception as e:
        _finish(job, "failed")
        job["result"] = {"error": "%s: %s" % (type(e).__name__, e)}


def _export_txt(b, job=None):
    """把已解析的书整本导出 txt（跳过已有缓存，断点续抓）。"""
    safe = re.sub(r'[\\/:*?"<>|]', "_", b["title"] or b["id"])[:60]
    out_path = os.path.join(config["reader_dir"], safe + ".txt")
    os.makedirs(config["reader_dir"], exist_ok=True)
    total = len(b["chapters"])
    done = 0
    with open(out_path, "w", encoding="utf-8-sig") as fp:
        fp.write((b["title"] or "") + ("\n作者：%s" % b["author"] if b.get("author") else "") + "\n\n")
        for i in range(1, total + 1):
            text = store.fetch_chapter(b["id"], i, fetcher)
            ch = b["chapters"][i - 1]
            if not text:
                _job_log(job, "第%d章《%s》抓取失败，跳过" % (i, ch["title"]))
                continue
            fp.write(ch["title"] + "\n" + text + "\n\n")
            done += 1
            if job and (done % 25 == 0 or done == total):
                job["progress"] = 80 + int(20 * done / max(1, total))
                _job_log(job, "txt 进度 %d/%d" % (done, total))
    _job_log(job, "txt 完成：%d/%d 章 -> %s" % (done, total, out_path))
    return out_path


# ---------------- API ----------------
def _book_info(b):
    return {"id": b["id"], "title": b.get("title") or "", "author": b.get("author") or "",
            "site": b.get("site") or "", "url": b.get("url"),
            "chapter_count": b.get("chapter_count", len(b.get("chapters") or [])),
            "resolved": bool(b.get("resolved")), "reader_ok": b.get("reader_ok"),
            "error": b.get("error", "")}


@app.get("/api/config")
def api_config():
    return {"base": gw_base(), "reader_dir": config["reader_dir"],
            "xpaths": gw.GW_XPATHS, "layer_stats": fetcher.layer_stats,
            "max_results": int(config.get("max_results") or 60),
            "cache_max_mb": int(config.get("cache_max_mb") or 500)}


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1),
               limit: int = Query(0, ge=0, le=300)):
    t0 = time.time()
    stats = {}
    results, errors = aggregate_search(fetcher, q, stats=stats,
                                       max_results=limit or config.get("max_results"))
    results = _merge_shelf(q, results)
    out = []
    for r in results:
        if r.get("src") != "shelf":
            bid = store.register(r["url"], title=r["title"], site=r["site"])
            r = {"id": bid, **r}
        out.append(r)
    _log_search(q, len(out), len(errors), time.time() - t0, stats, source="api")
    return {"results": out, "errors": errors}


@app.get("/api/search/stream")
def api_search_stream(q: str = Query(..., min_length=1),
                      limit: int = Query(0, ge=0, le=300)):
    """流式搜索（SSE）：种子站与发现层并发，谁先出结果就先推给前端。

    前端用 EventSource 接收，逐批 append，不必等最慢的那个源。
    事件：{"type":"batch","site":..,"src":..,"items":[..],"elapsed":..}
          {"type":"status","msg":..}
          {"type":"done","total":..,"elapsed":..}"""
    cap = limit or config.get("max_results") or 0
    def _sse(obj):
        return "data: %s\n\n" % json.dumps(obj, ensure_ascii=False)

    def gen():
        t0 = time.time()
        seen = set()
        total = 0
        site_ms = {}
        stats = {}

        def _pack(raw, src):
            """按相关度过滤 + 同 URL 去重 + 注册书籍 id，前端拿到即可渲染。"""
            out = []
            for r in raw or []:
                url = (r.get("url") or "").rstrip("/")
                if not url or url in seen:
                    continue
                if _relevance(q, r.get("title") or "") >= 3:
                    continue
                seen.add(url)
                bid = store.register(r["url"], title=r.get("title") or "",
                                     site=r.get("site") or "")
                out.append({"id": bid, "title": r.get("title") or "",
                            "author": r.get("author") or "",
                            "site": r.get("site") or "", "url": r["url"],
                            "src": src})
            return out

        try:
            from engine.websearch import web_discover
        except ImportError:
            from websearch import web_discover

        seeds = load_seeds()
        ex = ThreadPoolExecutor(max_workers=len(seeds) + 1)
        try:
            # 种子站与发现层同池并发：慢站不再拖住整体，先完成的先推
            seed_futs = {ex.submit(_search_site, fetcher, s, q, site_ms): s
                         for s in seeds}
            web_fut = ex.submit(web_discover, fetcher, q, stats=stats)
            pending = set(seed_futs) | {web_fut}
            # 两段式预算：流式本身保证首批结果几秒内就推出去（谁先回谁先推），
            # 这里管的是总时长 —— 原来 as_completed(timeout=45) 一刀切，45s 时
            # 已算完但还没轮到的 future 会连同结果一起被丢掉；改成手动轮询后
            # 只要总预算没到，拿到一个推一个。
            deadline = t0 + SSE_TOTAL_BUDGET
            while pending:
                left = deadline - time.time()
                if left <= 0:
                    yield _sse({"type": "status", "msg": "部分来源超时，已返回现有结果"})
                    break
                done, pending = wait(pending, timeout=min(left, 5),
                                     return_when=FIRST_COMPLETED)
                if not done:
                    continue        # 只是这一轮没等到，继续等剩下的
                for fut in done:
                    if fut is web_fut:
                        label, src = "全网发现", "web"
                    else:
                        label, src = seed_futs[fut]["name"], "seed"
                    try:
                        raw = fut.result()
                    except Exception as e:
                        yield _sse({"type": "status",
                                    "msg": "%s 失败：%s" % (label, str(e)[:40])})
                        continue
                    if cap and total >= cap:
                        continue    # 已经够数，剩下的只 drain 不推
                    items = _pack(raw, src)
                    if cap:
                        items = items[:max(0, cap - total)]
                    if items:
                        total += len(items)
                        yield _sse({"type": "batch", "site": label, "src": src,
                                    "items": items,
                                    "elapsed": round(time.time() - t0, 1)})
        finally:
            ex.shutdown(wait=False)   # 不等卡死的源，避免拖住本次请求

        stats["sites"] = site_ms
        stats["total"] = time.time() - t0
        _log_search(q, total, 0, stats["total"], stats, source="stream")
        yield _sse({"type": "done", "total": total,
                    "elapsed": round(stats["total"], 1)})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/resolve")
async def api_resolve(req: Request):
    body = await req.json()
    url = body.get("url", "").strip()
    want_txt = bool(body.get("txt"))
    title = body.get("title") or None
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    bid = store.register(url, title=title or "")
    job = _new_job("resolve")
    t = threading.Thread(target=_run_resolve_job, args=(job, bid, url, want_txt), daemon=True)
    t.start()
    return {"job_id": job["id"], "book_id": bid}


@app.get("/api/job/{jid}")
def api_job(jid: str):
    with JOBS_LOCK:
        job = JOBS.get(jid)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return job


@app.get("/api/books")
def api_books():
    return {"books": [_book_info(b) for b in store.all(shelf_only=True)]}


@app.post("/api/delete/{bid}")
def api_delete(bid: str):
    if not store.delete(bid):
        return JSONResponse({"error": "no such book"}, status_code=404)
    return {"ok": True}


@app.post("/api/cache/{bid}")
def api_clear_cache(bid: str):
    """清空本书章节缓存（书架保留，重新阅读时重抓）。"""
    if store.get(bid) is None:
        return JSONResponse({"error": "no such book"}, status_code=404)
    store.clear_cache(bid)
    return {"ok": True}


@app.get("/api/cache/stats")
def api_cache_stats():
    """章节缓存总览：总大小 / 配额 / 每本占用与最后访问时间（最旧在前）。"""
    st = gw.cache_stats()
    limit = int(config.get("cache_max_mb") or 500) * 1024 * 1024
    titles = {b["id"]: (b.get("title") or "")[:30] for b in store.all(shelf_only=False)}
    for b in st["books"]:
        b["title"] = titles.get(b["id"], "")
    return {"total": st["total"], "limit": limit, "books": st["books"]}


@app.post("/api/txt/{bid}")
def api_txt(bid: str):
    b = store.get(bid)
    if b is None:
        return JSONResponse({"error": "no such book"}, status_code=404)
    job = _new_job("txt")

    def _run():
        try:
            b2, err = store.ensure_resolved(bid, fetcher)
            if err:
                _finish(job, "failed"); job["result"] = {"error": err}; return
            path = _export_txt(b2, job)
            job["result"] = {"txt_file": path}
            _finish(job, "done"); job["progress"] = 100
        except Exception as e:
            _finish(job, "failed"); job["result"] = {"error": str(e)}

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job["id"]}


@app.post("/api/refresh/{bid}")
def api_refresh(bid: str):
    b = store.get(bid)
    if b is None:
        return JSONResponse({"error": "no such book"}, status_code=404)
    job = _new_job("refresh")

    def _run():
        b2, err = store.ensure_resolved(bid, fetcher, force=True)
        if err:
            _finish(job, "failed"); job["result"] = {"error": err}; return
        job["result"] = _book_info(b2)
        _finish(job, "done"); job["progress"] = 100

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job["id"]}


def _quick_chapter(b, ch, fetcher):
    """体检用快速抓单章：单次请求 + xpath + 提取（无重试无分页跟随）。"""
    from autobs import parse_doc, extract_content_text
    r = fetcher.get(ch["url"], referer=b.get("list_url"), quick=True)
    if r is None:
        return None
    doc = parse_doc(r)
    if doc is None:
        return None
    got = doc.xpath((b.get("src") or {}).get("content_xpath", ""))
    if len(got) != 1:
        return None
    return extract_content_text(got[0])


@app.post("/api/healthcheck")
def api_healthcheck():
    """一键体检：书架每本书 -> 站点探活 + 目录抽查 + 首/中/末章正文质量断言。
    报告同时落盘 data/health.json（书架页显示最近体检结果）。"""
    job = _new_job("healthcheck")

    def _run():
        import sys as _sys
        _sys.path.insert(0, BASE_DIR)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from tests.quality import check_chapter_text

        def _check_one(b):
            """单本体检：1 次探活 + 首/中/末章各一次快抓（有缓存则读缓存）。"""
            entry = {"id": b["id"], "title": (b.get("title") or "")[:30],
                     "site": b.get("site") or "", "alive": False,
                     "alive_reason": "", "catalog": 0, "chapters": [], "ok": False}
            # 站点探活：顺手问清「为什么不通」（CF 硬拦截 / 超时 / 404 分得开）
            probe = fetcher.diagnose(b.get("list_url") or b.get("url"))
            entry["alive"] = bool(probe.get("ok"))
            entry["alive_reason"] = probe.get("reason") or ""
            # 目录（用已有解析结果，不强制重解析）
            b2, err = store.get_catalog(b["id"], fetcher)
            entry["catalog"] = len(b2.get("chapters") or []) if b2 else 0
            # 抽查：首章 quick 抓取断言；中/末章只读已有缓存
            if b2 and entry["catalog"]:
                n = entry["catalog"]
                for idx in sorted({0, n // 2, n - 1}):
                    ch = b2["chapters"][idx]
                    text = None
                    # 缓存键算法统一走网关，别在这儿重算 md5
                    cpath = gw.cache_path(b["id"], ch["url"])
                    if os.path.exists(cpath):
                        try:
                            text = open(cpath, encoding="utf-8").read()
                        except Exception:
                            text = None
                    checked = True
                    # 站点探活已经失败时不再实抓：既省掉 3 次超时，也避免把
                    # 「站点挂了」误报成「章节抓不到」。没缓存就标成跳过。
                    if text is None and entry["alive"]:
                        text = _quick_chapter(b2, ch, fetcher)
                    if text is None:
                        if not entry["alive"]:
                            # 站点不可达：中/末章没缓存属正常，不参与判定
                            entry["chapters"].append(
                                {"idx": idx + 1, "title": ch["title"][:20],
                                 "len": 0, "problems": [],
                                 "skipped": "站点不可达，未抽查"})
                            continue
                        # 首章抓不到说明这本书可能不可用；中/末章失败多为站点瞬时波动，
                        # 不参与整体判定，避免一次网络抖动就把整本判红
                        checked = idx == 0
                        item = {"idx": idx + 1, "title": ch["title"][:20],
                                "len": 0,
                                "problems": ["首章快抓失败"] if idx == 0 else []}
                    else:
                        item = {"idx": idx + 1, "title": ch["title"][:20],
                                "len": len(text),
                                "problems": check_chapter_text(text) if checked else []}
                    entry["chapters"].append(item)
            # 可用性判定：目录在 + 首章质量过关（站点暂不可达只降级为警告，
            # 已缓存章节仍可读）
            entry["ok"] = bool(entry["catalog"]) and all(
                not c["problems"] for c in entry["chapters"])
            # 三态灯：绿=能用；黄=站点当前不可达但目录/缓存还在（还能读已缓存的章）；
            # 红=目录或首章坏了（真读不了）
            if not entry["catalog"]:
                entry["level"] = "bad"
            elif not entry["ok"]:
                entry["level"] = "bad"
            elif not entry["alive"]:
                entry["level"] = "warn"
            else:
                entry["level"] = "ok"
            return entry

        books = store.all(shelf_only=True)
        # 逐本串行时 5 本书最坏 5 × (8s + 3×8s) = 160s，网页台看着像卡死。
        # quick=True 不走节流，4 并发对目标站仍是可接受的压力。
        by_id = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(_check_one, b) for b in books]
            for fut in as_completed(futs):
                try:
                    entry = fut.result()
                except Exception as e:      # 单本异常不能拖垮整轮体检
                    _job_log(job, "体检跳过一本：%r" % (e,))
                    continue
                by_id[entry["id"]] = entry
                job["progress"] = int(100 * len(by_id) / max(1, len(books)))
                _job_log(job, "体检 %d/%d：%s %s" % (
                    len(by_id), len(books), entry["title"],
                    "✓" if entry["ok"] else "发现问题"))
        # 按书架顺序输出，结果稳定可比对
        report = [by_id[b["id"]] for b in books if b["id"] in by_id]
        # 落盘 + 完成报告
        try:
            import json as _json
            _json.dump({"time": time.time(), "books": report},
                       open(os.path.join(BASE_DIR, "data", "health.json"), "w",
                            encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass
        job["result"] = {"books": report,
                         "n_ok": sum(1 for e in report if e["ok"]),
                         "n_total": len(report)}
        _finish(job, "done"); job["progress"] = 100

    threading.Thread(target=_run, daemon=True).start()
    return {"job_id": job["id"]}


@app.get("/api/health")
def api_health():
    """最近一次体检报告（无则空）。"""
    p = os.path.join(BASE_DIR, "data", "health.json")
    if not os.path.exists(p):
        return {"books": []}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {"books": []}


# ---------------- 网关（Reader 书源指向这里） ----------------
def _merge_shelf(keyword, results):
    """把书架上书名匹配的书置顶并入搜索结果——解析过的书在 Reader
    搜索框必须始终可搜到（网关 id 直达，无需重新解析）。
    输入支持『书名 作者』：按书名部分匹配（作者也参与匹配加分）。"""
    import re as _re
    from engine.search import split_query
    title_q, author_q = split_query(keyword)
    key = _re.sub(r"\s+", "", title_q)
    merged = []
    seen = set()
    for b in store.all(shelf_only=True):
        t_match = key and key in _re.sub(r"\s+", "", b.get("title") or "")
        a_match = author_q and author_q in (b.get("author") or "")
        if t_match or a_match:
            site = b.get("site")
            if not site:
                from urllib.parse import urlparse as _up
                site = _up(b["url"]).netloc.replace("www.", "")
            merged.append({"id": b["id"], "title": b["title"], "url": b["url"],
                           "site": site, "src": "shelf",
                           "author": b.get("author") or ""})
            seen.add(b["url"].rstrip("/"))
    for r in results:
        if (r.get("url") or "").rstrip("/") in seen:
            continue  # 书架已有同 URL，避免重复条目
        merged.append(r)
        seen.add((r.get("url") or "").rstrip("/"))
    return merged


@app.get("/g/search", response_class=HTMLResponse)
def g_search(request: Request, q: str = Query(...)):
    t0 = time.time()
    stats = {}
    results, errs = aggregate_search(fetcher, q, stats=stats)
    results = _merge_shelf(q, results)
    items = []
    for r in results:
        if r.get("src") == "shelf":
            bid = r["id"]
        else:
            bid = store.register(r["url"], title=r["title"], site=r["site"])
        items.append({"id": bid, "title": r["title"],
                      "author": r.get("author") or "", "site": r["site"]})
    _log_search(q, len(items), len(errs), time.time() - t0, stats, source="reader")
    return gw.render_search_page(items, gw_base(request))


@app.get("/g/book/{bid}", response_class=HTMLResponse)
def g_book(request: Request, bid: str, refresh: int = 0):
    b, err = (store.ensure_resolved(bid, fetcher, force=True) if refresh
              else store.get_catalog(bid, fetcher))
    if err:
        return HTMLResponse("<html><body><p>解析失败: %s</p></body></html>" % err,
                            status_code=200)
    return gw.render_catalog_page(b, gw_base(request))


@app.get("/g/chapter/{bid}/{n}", response_class=HTMLResponse)
def g_chapter(bid: str, n: int):
    b = store.get(bid)
    title = b["chapters"][n - 1]["title"] if b and b.get("chapters") and 1 <= n <= len(b["chapters"]) else ""
    text = store.fetch_chapter(bid, n, fetcher)
    if text is None:
        return HTMLResponse("<html><body><div id='bh-content'>本章抓取失败，"
                            "请稍后重试或刷新目录。</div></body></html>")
    return gw.render_chapter_page(title, text)


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(BASE_DIR, "static", "index.html"), encoding="utf-8").read()


if __name__ == "__main__":
    import uvicorn

    host = config["host"]

    # ① 主端口：被占用则自动顺延，保证网页台一定能起来
    want_port = int(config["port"])
    web_port = _pick_port(host, want_port)
    if web_port != want_port:
        print("[bookhub] 主端口 %d 已被占用，网页台改用 %d" % (want_port, web_port))
    config["port"] = web_port

    # ② 网关端口：优先用 gateway_port（默认 80），Reader 对无端口地址兼容性最好
    want_gw = int(config.get("gateway_port") or 0)
    gw_port = want_gw if (want_gw and want_gw != web_port
                          and _port_available(host, want_gw)) else 0

    # ③ 确定网关对外地址，并把 bs_gateway.json 同步成这个地址
    GW_PRIMARY = _fmt_base(gw_port or web_port)
    bs_path = _sync_gateway_source(GW_PRIMARY)

    print("bookhub 网页台: %s  (浏览器打开开始找书)" % _fmt_base(web_port))
    if gw_port:
        print("Reader 网关:   %s  (端口 %d)" % (GW_PRIMARY, gw_port))
    else:
        reason = ("端口 %d 不可用" % want_gw) if want_gw else "未启用独立网关端口"
        print("Reader 网关:   %s  (%s，网关已并入主端口；"
              "此前导入过书源的需在 Reader 里重新导入一次)" % (GW_PRIMARY, reason))
    print("网关书源文件: %s （导入 Reader 一次，之后 Reader 搜索框直搜所有站）" % bs_path)

    # ④ 章节缓存配额：超了就按最久未读整本清掉，避免无限长胖
    try:
        _limit = int(config.get("cache_max_mb") or 500) * 1024 * 1024
        _r = gw.prune_cache(_limit)
        if _r["removed"]:
            print("[bookhub] 章节缓存 %.1fMB 超配额 %.0fMB，已清理 %d 本（释放 %.1fMB）"
                  % (_r["before"] / 1048576.0, _limit / 1048576.0,
                     len(_r["removed"]), _r["freed"] / 1048576.0))
    except Exception as e:
        print("[bookhub] 缓存配额检查跳过：%s" % e)

    # ④ 起监听：网关在子线程（失败不影响网页台），主端口在主线程
    def _serve_gateway(port):
        try:
            uvicorn.run(app, host=host, port=port, log_level="warning")
        except Exception as e:
            print("[bookhub] 网关端口 %d 启动失败：%s" % (port, e))

    if gw_port:
        threading.Thread(target=_serve_gateway, args=(gw_port,), daemon=True).start()
    uvicorn.run(app, host=host, port=web_port, log_level="warning")
