# -*- coding: utf-8 -*-
"""搜索引擎发现层：Bing 搜「"书名" 小说/全文阅读」，提取小说站书籍页候选，
黑名单过滤 + 并行轻量预验（标题含书名），输出与种子站结果同构的列表。"""
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "autobs"))
from autobs import parse_doc, norm_space  # noqa: E402

# 非小说站黑名单（按主域名/关键词匹配）
DOMAIN_BL = (
    "jjwxc.net", "jjwxc.com",  # 用户不需要晋江（种子与发现层全部排除，含 wap/m 子域）
    "baidu.com", "bing.com", "google.com", "sogou.com", "so.com", "sm.cn",
    "zhihu.com", "weibo.com", "bilibili.com", "douban.com", "qq.com",
    "weixin.qq.com", "sohu.com", "163.com", "csdn.net", "jianshu.com",
    "toutiao.com", "douyin.com", "xiaohongshu.com", "taobao.com", "jd.com",
    "tmall.com",
    "zdic.net", "chagushici.com", "yuwenmi.com", "hanyuguoxue.com",
    "kmcha.com", "wanjuanguoxue.com", "moe.edu.tw", "guoxue",
    "wikipedia.org", "gov.cn", "edu.cn", "youtube.com", "facebook.com",
    "github.com", "cloud.tencent.com", "cnblogs.com", "cnstar",
    "music.163", "y.qq.com",
    # 漫画/动画站：搜「遮天」这类有动画改编的书名时会命中
    # 「遮天（4K）【国语】免费高清动漫在线看」这种页面，解析不出正文。
    "xgcartoon.com", "cartoon", "comic", "anime",
)

# 实测结论（2026-10）：Bing 上「纯引号短语」多数书命中最稳；
# 但部分书名（短词开头的长书名）引号查询会触发 Bing 的
# 单字字典模式，反而裸关键词能命中小说站——三种变体按序尝试、够数即停。
QUERY_VARIANTS = ('"{kw}"', '{kw}', '{kw} 小说')
MAX_VERIFY = 8          # 最多预验的候选数
VERIFY_TIMEOUT = 12


def _norm_title(s):
    return re.sub(r"[\s《》〈〉「」『』_:：\-—|·.。!！?？·,，()（）\[\]]+", "", s or "")


def _blacked(host):
    h = (host or "").lower()
    return any(b in h for b in DOMAIN_BL)


def _root_domain(host):
    parts = (host or "").lower().lstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def _bing_fetch(fetcher, query):
    """抓一页 Bing 结果，返回 doc 或 None。"""
    r = fetcher.get("https://cn.bing.com/search?q=" + query,
                    referer="https://cn.bing.com/")
    if r is None:
        return None
    return parse_doc(r)


def bing_search(fetcher, keyword, force_l3=False, author=""):
    """Bing 搜索一轮。force_l3=False 走普通 HTTP（快但易被降级），
    True 用真浏览器（慢 ~5s 但结果完整）。author 非空时启用联合查询
    变体（『"书名" 作者』——作者词能大幅过滤同名噪音，仅 L1 使用）。
    变体够数即停：L1 拿到 6 条可读链接后不再试后续变体（省间隔），
    L3 每次抓取 ~5s，拿到即停。返回 [(title, url)]。"""
    import time
    seen, results = set(), []

    # 变体之间的间隔：Bing 对连续请求有限流，但实测 1.2s 仍稳定；
    # 原值 2.5s 在"三个变体全跑"的冷门书上白白多花 5s。
    VARIANT_GAP_L1 = 1.2
    VARIANT_GAP_L3 = 1.5

    def _collect(doc):
        for a in doc.xpath("//li[contains(@class,'b_algo')]//h2/a"):
            href = a.get("href") or ""
            title = norm_space(a.text_content())
            if not href.startswith("http") or not title:
                continue
            host = urlparse(href).netloc
            if not host or _blacked(host):
                continue
            if _norm_title(keyword) not in _norm_title(title):
                continue
            # 章节页直接丢弃（解析不出目录，用户点了必失败）：
            # 特征 = /read/书id/章节id 两层数字路径，或 _2.html 分页后缀。
            # 单层数字文件名（如 /yimo/3208398.html）是书页形态，保留。
            path = urlparse(href).path.lower()
            if (re.search(r"/reads?/\d+/\d+", path)
                    or re.search(r"/\d+/\d+(_\d+)?\.html?$", path)  # 任意 数字/数字.html 两层形态
                    or re.search(r"_\d+(\.html?)?$", path)):
                continue
            k = href.split("#")[0].rstrip("/")
            if k in seen:
                continue
            seen.add(k)
            results.append((title, href))

    variants = list(QUERY_VARIANTS)
    if author:
        variants = ['"{kw}" {au}'.format(kw=keyword, au=author)] + variants
    if force_l3:
        # L3 不用作者变体（『"书名" 作者』即使真浏览器 Bing 也返回百科/
        # 知乎类结果）；纯引号最稳，但部分书名会触发字典模式，
        # 用裸关键词兜底，拿到即停。
        variants = ['"{kw}"', '{kw}']
    enough = 1 if force_l3 else 6
    for i, tpl in enumerate(variants):
        if i:
            time.sleep(VARIANT_GAP_L3 if force_l3 else VARIANT_GAP_L1)
        q = quote(tpl.format(kw=keyword, au=author))
        try:
            if force_l3:
                r = fetcher._req("GET", "https://cn.bing.com/search?q=" + q,
                                 referer="https://cn.bing.com/", force_layer="L3")
            else:
                r = fetcher.get("https://cn.bing.com/search?q=" + q,
                                referer="https://cn.bing.com/")
        except Exception:
            r = None
        if r is None:
            continue
        doc = parse_doc(r)
        if doc is not None:
            _collect(doc)
            if len(results) >= enough:
                break
    return results


def _is_redirect(url):
    """百度结果链接：域名恒定是 baidu.com，真实地址要跟随重定向才拿到。"""
    return "baidu.com/link" in (url or "").lower()


def baidu_search(fetcher, keyword, author=""):
    """百度发现层。返回 [(title, url)]，url 为百度跳转链——
    真实地址在 _verify 跟随重定向时取回，故此处无法做域名黑名单，
    域名过滤统一放到验证阶段（那时才有真实 host）。

    存在意义：Bing 对中文网络小说（尤其番茄系新书）收录很差，
    长书名常退化成首字字典检索；同一本书百度能直接命中多个书站。"""
    import time
    variants = []
    if author:
        variants.append('"%s" %s' % (keyword, author))
    variants += ['"%s" 小说' % keyword, '%s 小说' % keyword, keyword]

    seen, results = set(), []
    for i, q in enumerate(variants):
        if i:
            time.sleep(1.5)
        url = "https://www.baidu.com/s?wd=" + quote(q)
        try:
            r = fetcher.get(url, referer="https://www.baidu.com/")
        except Exception:
            r = None
        if r is None:
            continue
        # 百度反爬拦截页：直接放弃本轮，交给上层降级
        try:
            if "wappass.baidu.com" in (r.url or "") or "百度安全验证" in r.text[:3000]:
                continue
        except Exception:
            pass
        doc = parse_doc(r)
        if doc is None:
            continue
        for h in doc.xpath("//h3"):
            links = h.xpath(".//a")
            if not links:
                continue
            href = links[0].get("href") or ""
            title = norm_space(h.text_content())
            if not href.startswith("http") or not title:
                continue
            if _norm_title(keyword) not in _norm_title(title):
                continue
            k = href.split("#")[0]
            if k in seen:
                continue
            seen.add(k)
            results.append((title, href))
        if len(results) >= 8:
            break
    return results


def _verify(fetcher, keyword, url):
    """打开候选页验证标题仍含书名。

    通过时返回 (作者名, 最终URL)，否则 (None, None)。返回最终 URL 是为了
    把百度跳转链换成真实书页地址——顺带也能做域名黑名单判定。

    走 quick 模式且**关闭 L2**（单次 5s、不重试、不升级）：预验只判断标题
    对不对，死站跳过即可。cloudscraper 首次初始化有秒级开销且实例共享无锁，
    并发预验时会互相争抢——实测 9 个候选的预验因此从 5s 拖到 14.6s，
    占到整次搜索耗时的 55%。"""
    try:
        try:
            r = fetcher.get(url, quick=True, allow_l2=False, timeout=5)
        except TypeError:      # 兼容不支持这些参数的 fetcher
            try:
                r = fetcher.get(url, quick=True)
            except TypeError:
                r = fetcher.get(url)
    except Exception:
        return None, None
    if r is None:
        return None, None
    doc = parse_doc(r)
    if doc is None:
        return None, None
    final = getattr(r, "url", None) or url
    if _is_redirect(url) or _is_redirect(final):
        try:
            if _blacked(urlparse(final).netloc):
                return None, None
        except Exception:
            pass
    text = _norm_title(" ".join(
        t for t in [e.text_content() for e in doc.xpath("//title | //h1")] if t))
    if _norm_title(keyword) not in text:
        return None, None
    try:
        from autobs import extract_book_info
        _, author = extract_book_info(doc, final)
        return author or "", final
    except Exception:
        return "", final


def _score_url(url):
    """书页特征加分、章节页降权（章节页解析不出目录）。"""
    s = 0
    path = urlparse(url).path.lower()
    if re.search(r"/book(\d*)/|/infos/|onebook\.php|/novel/\d+/?$", path):
        s += 2  # 书页/目录页特征
    if re.search(r"/reads?/\d+/\d+", path) or re.search(r"[_/]\d{5,}(_\d+)?\.html?$", path):
        s -= 1  # 章节页特征
    return s


def _verify_candidates(fetcher, keyword, cands, max_verify=MAX_VERIFY):
    """排序 + 并行预验，返回通过的书目 dict 列表。"""
    if not cands:
        return []
    cands.sort(key=lambda x: -_score_url(x[1]))
    by_root, ordered = {}, []
    for title, url in cands:
        # 跳转链的域名恒定是 baidu.com，若参与预去重会把所有候选压成一条；
        # 这类候选跳过预去重，等验证拿到真实域名后再按站合并。
        if not _is_redirect(url):
            rd = _root_domain(urlparse(url).netloc)
            if rd in by_root:
                continue
            by_root[rd] = url
        ordered.append((title, url))
    ordered = ordered[:max_verify]

    out, seen_root = [], set()
    # 并发数与候选数对齐：原先固定 4 个 worker 时，8 个候选要跑两轮，
    # 耗时由"最慢的两个候选"叠加决定（实测预验占搜索总时长的 55%）。
    # quick 模式不走全局 _throttle，各候选又是不同域名，提高并发无副作用。
    with ThreadPoolExecutor(max_workers=min(8, len(ordered) or 1)) as ex:
        futs = {ex.submit(_verify, fetcher, keyword, url): (title, url)
                for title, url in ordered}
        try:
            for fut in as_completed(futs, timeout=VERIFY_TIMEOUT * 3):
                title, url = futs[fut]
                try:
                    author, final = fut.result(timeout=VERIFY_TIMEOUT)
                    if author is None:
                        continue
                    host = urlparse(final or url).netloc
                    rd = _root_domain(host)
                    if rd in seen_root:  # 同站只留一条，避免整屏同源
                        continue
                    seen_root.add(rd)
                    # 只去前缀，不能用 replace：域名中段的 "m." 会被误删
                    # （zhenganfm.com 曾被改成 zhenganfcom）
                    site = host[4:] if host.startswith("www.") else host
                    site = site[2:] if site.startswith("m.") else site
                    out.append({"title": title[:60], "url": final or url,
                                "site": site, "src": "web", "author": author})
                except Exception:
                    pass
        except Exception:
            pass
    return out


def web_discover(fetcher, keyword, max_results=6, author="", stats=None):
    """发现层主入口（三级降级）：
    1) 普通 HTTP 抓 Bing（快）→ 预验
    2) 结果为空 → 真浏览器再抓一轮 Bing（Bing 对无 JS 请求常降级）
    3) 结果仍不足 → 百度兜底（Bing 对中文新书/长书名收录明显弱于百度）
    返回 [{'title','url','site','src':'web'}]。

    stats：可选 dict，回填 bing / baidu / verify 三阶段耗时（秒）。"""
    import time

    def _add(key, dt):
        if stats is not None:
            stats[key] = stats.get(key, 0.0) + dt

    t0 = time.time()
    cands = bing_search(fetcher, keyword, author=author)
    _add("bing", time.time() - t0)

    t0 = time.time()
    out = _verify_candidates(fetcher, keyword, cands)
    _add("verify", time.time() - t0)

    if not out:
        t0 = time.time()
        try:
            cands3 = bing_search(fetcher, keyword, force_l3=True, author=author)
        except Exception:
            cands3 = []
        _add("bing", time.time() - t0)
        if cands3:
            t0 = time.time()
            out = _verify_candidates(fetcher, keyword, cands3)
            _add("verify", time.time() - t0)

    if len(out) < max_results:
        t0 = time.time()
        try:
            b_cands = baidu_search(fetcher, keyword, author=author)
        except Exception:
            b_cands = []
        _add("baidu", time.time() - t0)
        if b_cands:
            t0 = time.time()
            try:
                more = _verify_candidates(fetcher, keyword, b_cands)
            except Exception:
                more = []
            _add("verify", time.time() - t0)
            have = {_root_domain(urlparse(x["url"]).netloc) for x in out}
            for m in more:
                rd = _root_domain(urlparse(m["url"]).netloc)
                if rd in have:
                    continue
                have.add(rd)
                out.append(m)
    return out[:max_results]
