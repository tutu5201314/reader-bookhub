# -*- coding: utf-8 -*-
"""聚合搜索：并发搜种子书站，返回统一结果列表。"""
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "autobs"))
from autobs import parse_doc, norm_space  # noqa: E402

# 简繁转换（可选依赖）：繁体书站（如 biquge.tw）的书名与简体关键词
# 字形不同，不做归一会被相关度过滤掉。未安装 zhconv 时自动降级为不转换。
try:
    from zhconv import convert as _zh_convert
except ImportError:
    _zh_convert = None

SEEDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seeds.json")

# 结果条数上限：10 个站已经能把常见书名打满，再往上加只会让来源更分散。
# 需要更多时由 server 的 limit 参数 / config.json 的 max_results 覆盖。
MAX_RESULTS = 60

_FOLD_CACHE = {}


def _fold_cjk(s):
    """简繁归一化（繁体 -> 简体），带缓存。无 zhconv 时原样返回。"""
    if not s:
        return s
    hit = _FOLD_CACHE.get(s)
    if hit is not None:
        return hit
    out = s
    if _zh_convert is not None:
        try:
            out = _zh_convert(s, "zh-cn")
        except Exception:
            out = s
    if len(_FOLD_CACHE) < 8192:   # 防止长时间运行无限增长
        _FOLD_CACHE[s] = out
    return out


def load_seeds():
    """读取种子站。带 disabled 标记的保留在文件里（便于恢复），但不参与搜索。"""
    with open(SEEDS_PATH, encoding="utf-8") as fp:
        seeds = json.load(fp)
    return [s for s in seeds if not s.get("disabled")]


def _search_site(fetcher, seed, keyword, site_ms=None):
    """单站搜索（计时包装）。site_ms 可选：回填该站耗时（秒）供日志统计。"""
    t0 = time.time()
    try:
        return _search_site_impl(fetcher, seed, keyword)
    finally:
        if site_ms is not None:
            site_ms[seed["name"]] = time.time() - t0


def _search_site_impl(fetcher, seed, keyword):
    """单站搜索，返回 [{'title','url','site'}] 或 []（失败）。"""
    charset = "gbk" if seed.get("charset") == "gbk" else "utf-8"
    kw = quote(keyword.encode(charset, errors="ignore"))
    url_tpl = seed["url"]
    if seed.get("method", "GET") == "POST":
        params = (seed.get("params") or "").replace("%s", kw)
        r = fetcher.post(url_tpl, data=params.encode("ascii"), referer=seed["host"])
    else:
        r = fetcher.get(url_tpl.replace("%s", kw), referer=seed["host"])
    if r is None:
        return []
    doc = parse_doc(r)
    if doc is None:
        return []
    try:
        names = doc.xpath(seed["name_xpath"])
        urls = doc.xpath(seed["mainpage_xpath"])
    except Exception:
        return []
    out = []
    from autobs import norm_space as _ns
    import re as _re
    rows = _pair_rows(names)
    for n, u in zip(names, urls):
        title = norm_space(n.text_content() if hasattr(n, "text_content") else str(n))
        if not title:
            continue
        out.append({
            "title": title[:60],
            "url": urljoin(r.url, u),
            "site": seed["name"],
            "author": _row_author(_row_of(n), title, _ns),
        })
        if len(out) >= 30:
            break
    return out


def _relevance(keyword, title):
    """精确匹配 > 包含关键词 > 关键词包含标题 > 其他。
    比较前做简繁归一：繁体站（biquge.tw）的书名与简体关键词字形不同，
    不归一的话会被判为「不相关」而在噪音治理阶段整批丢弃。"""
    key = _fold_cjk(re.sub(r"\s+", "", keyword))
    t = _fold_cjk(re.sub(r"\s+", "", title))
    if not key:
        return 3
    if t == key:
        return 0
    if key in t:
        return 1
    if t in key:
        return 2
    return 3


def split_query(q):
    """『书名 作者』/『书名 by 作者』拆分：第二段短且非书名特征时视为作者。
    返回 (书名, 作者或'')。"""
    q = (q or "").strip()
    m = re.split(r"\s+by\s+|[\s　]+", q, maxsplit=1, flags=re.I)
    if len(m) == 2 and m[0] and m[1]:
        title, author = m[0].strip(), m[1].strip()
        author = re.sub(r"^作者[：:\s]*", "", author)
        if 1 < len(author) <= 12 and not re.search(r"小说|全集|番外|阅读", author):
            return title, author
    return q, ""


def _web_discover_safe(fetcher, keyword, author, stats):
    """在线程里跑发现层：import 与异常都收敛在内部，返回 (结果, 错误串)。

    供 aggregate_search 与种子站并行调用——异常不能往外抛，
    否则一个线程的失败会拖垮整次搜索。"""
    try:
        try:
            from engine.websearch import web_discover
        except ImportError:
            from websearch import web_discover
        return web_discover(fetcher, keyword, author=author, stats=stats), ""
    except Exception as e:
        return [], str(e)[:80]


def aggregate_search(fetcher, keyword, seeds=None, timeout_per_site=40, web=True,
                     stats=None, max_results=None):
    """三层搜索：种子站直搜（并发）+ 搜索引擎发现层（Bing / 百度兜底）。
    输入支持『书名 作者』联合查询：作者用于结果过滤排序，搜索用书名。
    返回扁平列表：作者匹配的最前，其余按相关度。

    stats：可选 dict，回填各阶段耗时（秒）——seed / sites / bing / baidu /
    verify / total，由调用方决定是否落日志（不传则零开销）。
    max_results：结果条数上限，默认 MAX_RESULTS（配置项可覆盖）。"""
    t_start = time.time()
    keyword, want_author = split_query(keyword)
    seeds = seeds or load_seeds()
    results = []
    errors = []
    site_ms = {}
    # 发现层与种子站并行发起：两层互不依赖，串行会让总时长变成两者之和。
    # 冷门书（Bing 无结果、要靠百度兜底）发现层要 10s+，种子的 3s 完全被掩盖。
    web_pool = web_fut = None
    if web:
        web_pool = ThreadPoolExecutor(max_workers=1)
        web_fut = web_pool.submit(_web_discover_safe, fetcher, keyword,
                                  want_author, stats)
    t_seed = time.time()
    with ThreadPoolExecutor(max_workers=len(seeds) or 1) as ex:
        futs = {ex.submit(_search_site, fetcher, s, keyword, site_ms): s for s in seeds}
        try:
            for fut in as_completed(futs, timeout=timeout_per_site * 2):
                seed = futs[fut]
                try:
                    results.extend(fut.result(timeout=5))
                except Exception as e:
                    errors.append({"site": seed["name"], "error": str(e)[:80]})
        except TimeoutError:
            # 慢站直接放弃，用已完成的结果（futures 会在 ex.shutdown 时被等掉）
            for fut, seed in futs.items():
                if fut.done() and not fut.cancelled():
                    try:
                        results.extend(fut.result(timeout=1))
                    except Exception:
                        pass
                elif not fut.done():
                    errors.append({"site": seed["name"], "error": "超时"})
    if stats is not None:
        stats["seed"] = time.time() - t_seed
        stats["sites"] = site_ms
    for r_ in results:
        r_["src"] = "seed"
    def _rank(x):
        rel = _relevance(keyword, x["title"])
        au = (x.get("author") or "")
        if want_author:
            if au and (want_author in au or au in want_author):
                rel -= 1   # 作者匹配的书前置一级
            elif au:
                rel += 1   # 作者明确不符的同名书降一级（空作者视为未知，不降）
        return rel

    results.sort(key=_rank)
    # 噪音治理：完全不含关键词的结果直接丢；"标题是关键词子串"的拆词噪音
    # （如搜「测试长书名」返回《交易》）每站最多 4 条；正常结果每站 12 条
    from collections import Counter
    per_site = Counter()
    capped = []
    for r_ in results:
        rel = _relevance(keyword, r_["title"])
        if rel >= 3:
            continue
        site = r_.get("site", "")
        limit = 4 if rel == 2 else 12
        if per_site[site] >= limit:
            continue
        per_site[site] += 1
        capped.append(r_)
    results = capped

    # 发现层：Bing 全网找新站（种子站没有的书靠这层覆盖）。
    # 结果在上面已并发开始，这里只等它返回。
    web_results = []
    if web_fut is not None:
        try:
            web_results, web_err = web_fut.result()
            if web_err:
                errors.append({"site": "web", "error": web_err})
        except Exception as e:
            errors.append({"site": "web", "error": str(e)[:80]})
        finally:
            web_pool.shutdown(wait=False)

    # 联合查询（带作者）时，发现层结果与种子结果统一按作者匹配度重排，
    # 否则目标书（常来自发现层）会整体沉底
    if want_author and web_results:
        merged0 = results + web_results
        merged0.sort(key=_rank)
        results, web_results = merged0[:len(results)], merged0[len(results):]

    # 全局重排：种子站与发现层结果同台按相关度排序——
    # 发现层的精确匹配结果不应被种子站的拆词噪音（如搜长书名返回的单字书）压制
    if web_results:
        results = results + web_results
        results.sort(key=_rank)
        web_results = []

    # 同 URL / 同主域去重：种子站结果优先于网页发现
    seen_url, seen_root, uniq = set(), set(), []
    for r_ in results + web_results:
        from urllib.parse import urlparse as _up
        k = r_["url"].rstrip("/")
        if k in seen_url:
            continue
        root = ".".join((_up(r_["url"]).netloc.lower().lstrip(".").split(".")[-2:]))
        # 种子结果允许保留（它们本来就不同站）；web 结果若与已有主域重复则丢弃
        if r_.get("src") == "web" and root in seen_root:
            continue
        seen_url.add(k)
        seen_root.add(root)
        uniq.append(r_)
    if stats is not None:
        stats["total"] = time.time() - t_start
    limit = MAX_RESULTS if not max_results else int(max_results)
    return uniq[:limit], errors


def maybe_add_seed(src, fetcher):
    """解析成功后自动沉淀种子站：该站站内搜索可用（query_url 含 %s）且
    真实搜一次能出结果，才写入 seeds.json。返回新增的 seed 或 None。"""
    try:
        if not src or "%s" not in (src.get("query_url") or ""):
            return None  # 单书模式/无搜索：不沉淀
        host = src.get("host") or ""
        seeds = load_seeds()
        if any(_root(s.get("host", "")) == _root(host) for s in seeds):
            return None  # 已有该站
        seed = {
            "name": (src.get("title") or host)[:20],
            "host": host,
            "method": "POST" if src.get("query_method") == 1 else "GET",
            "url": src["query_url"],
            "params": src.get("query_params") or "",
            "charset": "gbk" if src.get("query_charset") == 2 else "utf-8",
            "name_xpath": src.get("book_name_xpath") or "",
            "mainpage_xpath": src.get("book_mainpage_xpath") or "",
        }
        # 搜索端点真实性验证：搜本书名（前 2 字泛化）能出结果才收
        got = _search_site(fetcher, seed, seed["name"][:2] or "小说")
        if not got:
            return None
        seeds.append(seed)
        tmp = SEEDS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(seeds, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, SEEDS_PATH)  # 原子替换，避免写入中断损坏种子库
        return seed
    except Exception:
        return None


def _root(host):
    parts = (host or "").lower().lstrip(".").split(".")
    return ".".join(parts[-2:])


def _row_of(el, max_up=3):
    """书名元素向上找行容器（文本明显多于书名本身的最小祖先）。"""
    row = el
    for _ in range(max_up):
        p = row.getparent()
        if p is None:
            break
        row = p
        if len(norm_space(row.text_content() or "")) > 20:
            return row
    return row


def _pair_rows(names):
    return None  # 兼容占位（行容器在 _row_author 内部按需获取）


def _row_author(row, title, ns):
    """从搜索结果行提取作者：两种常见形态——
    1) 杰奇系行文本『书名 连载中作者：XX 最新章节』（正则）
    2) 列布局行内无链接短文本元素（晋江式 span）"""
    try:
        text = ns(row.text_content() or "")
        m = re.search(r"作者[：:\s]*([^\s　,，第（(]{1,20})", text)
        if m:
            return m.group(1)[:20]
        for el in row.iter("span", "td", "em", "p"):
            if list(el.iter("a")):
                continue
            txt = ns(el.text_content() or "")
            if 2 <= len(txt) <= 12 and txt != title                     and not re.search(r"\d|连载|完本|小说|阅读|章", txt):
                return txt[:20]
    except Exception:
        pass
    return ""
