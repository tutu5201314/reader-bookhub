#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从「阅读」书源仓库（yck2026.fun）批量导入可用书源到 engine/seeds.json。

书源仓库每个条目都有标准 JSON 直链，内含 searchUrl 与 ruleSearch 规则。
本脚本把「阅读 3.0」语法静态转换成 seeds.json 需要的 xpath/URL 模板，
再逐站实测能否真正搜出结果，通过的才写入配置。

用法：
    python scripts/import_book_sources.py                  # 试跑：抓 6 页、转 100 个、实测并报告
    python scripts/import_book_sources.py --limit 300      # 扩大转换数量
    python scripts/import_book_sources.py --merge          # 把通过实测的写入 seeds.json
    python scripts/import_book_sources.py --pages 20 --limit 500 --merge
    python scripts/import_book_sources.py --keyword 诡秘之主 --min-results 5

说明：
- 只做静态转换。以 @js: / <js> 开头的规则（自定义 JS 搜索）无法转换，直接跳过。
- 不做内容类型筛选（成人向站点照样纳入），仅排除非文本类书源
  （bookSourceType != 0，如漫画/听书/视频）——那种书源没有可用于正文的文本。
- --merge 会按主域名去重，已存在的站点不会重复添加。
"""
import argparse
import json
import os
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

SEEDS_PATH = os.path.join(BASE_DIR, "engine", "seeds.json")
SRC_BASE = "https://www.yck2026.fun"
LIST_URL = SRC_BASE + "/yuedu/shuyuan/index.html"
JSON_URL = SRC_BASE + "/yuedu/shuyuan/json/id/%s.json"


# ---------------- 阅读语法 -> seeds 配置 ----------------

def css_to_xpath(sel):
    """极简 CSS -> XPath：支持 tag / .class / tag.class / #id / 空格后代。"""
    sel = (sel or "").strip()
    if not sel:
        return None
    if sel.startswith("/"):
        return sel                      # 本身就是 xpath
    out = ""
    for p in sel.split():
        if p.startswith("#"):
            out += "//*[@id='%s']" % p[1:]
        elif p.startswith("."):
            out += ("//*[contains(concat(' ',normalize-space(@class),' '),' %s ')]"
                    % p[1:])
        elif "." in p:
            tag, _, cls = p.partition(".")
            if tag in ("class", ""):    # 有的书源把 class 当标签名写
                tag = "*"
            out += ("//%s[contains(concat(' ',normalize-space(@class),' '),' %s ')]"
                    % (tag, cls))
        else:
            out += "//" + p
    return out


def clean_rule(rule):
    """剥掉阅读语法的修饰，返回可转 CSS 的选择器；不可静态转换返回 None。"""
    r = (rule or "").strip()
    if not r:
        return None
    if r.startswith(("@js:", "<js>", "$.")):   # JS / JSONPath，静态转换不了
        return None
    if r.startswith("@css:"):
        r = r[5:]
    r = r.split("@")[0].split("##")[0].strip()  # 去掉 @text / @href 与 ##正则##
    if not r or r.startswith(("(", "{")):
        return None
    return r


def norm_search_url(su):
    """把 searchUrl 的模板变量统一成 %s。无法处理的返回 (None, 原因)。"""
    su = (su or "").strip()
    if not su:
        return None, "无 searchUrl"
    if su.startswith(("@js:", "<js>")):
        return None, "搜索走 JS"
    su = re.sub(r"\{\{\s*page\s*==\s*1\s*\?\s*''\s*:\s*'/' \+ page\s*\}\}", "", su)
    su = re.sub(r"\{\{\s*page\s*\}\}", "1", su)
    su = re.sub(r"\{\{\s*java\.encodeURI\(key\)\s*\}\}", "%s", su)
    su = re.sub(r"\{\{\s*key\s*\}\}", "%s", su)
    if "{{" in su:
        return None, "含其他模板变量"
    if "%s" not in su:
        return None, "无关键词占位"
    return su, ""


def pick_candidate(rule):
    """rule 可能用 || 给多个候选：优先选不含 #id 的（搜索页通常没有首页容器）。"""
    cands = [x.strip() for x in (rule or "").split("||") if x.strip()]
    if not cands:
        return ""
    return next((x for x in cands if "#" not in x), cands[-1])


def to_seed(js):
    """阅读书源 dict -> seeds.json 条目。返回 (seed, 失败原因)。"""
    name = (js.get("bookSourceName") or "").strip()
    host = (js.get("bookSourceUrl") or "").strip().split("#")[0].rstrip("/")
    rs = js.get("ruleSearch") or {}

    if js.get("bookSourceType") not in (0, None):
        return None, "非文本类书源"
    if not (name and host.startswith("http") and rs):
        return None, "字段不全"

    url, err = norm_search_url(js.get("searchUrl"))
    if not url:
        return None, err

    bl = clean_rule(pick_candidate(rs.get("bookList")))
    nm = clean_rule(pick_candidate(rs.get("name")))
    bu = clean_rule(pick_candidate(rs.get("bookUrl")))
    if not (bl and nm and bu):
        return None, "搜索规则含 JS/JSONPath"

    base = css_to_xpath(bl)
    nx, ux = css_to_xpath(nm), css_to_xpath(bu)
    if not (base and nx and ux):
        return None, "选择器无法转换"

    return {
        "name": name[:20],
        "host": host,
        "method": "GET",
        "url": url,
        "charset": "utf-8",
        "name_xpath": base + nx,
        "mainpage_xpath": base + ux + "/@href",
    }, ""


# ---------------- 抓取 ----------------

def fetch_catalog(fetcher, pages):
    """抓列表页，返回 [(书源id, 标题)]，按出现顺序去重。"""
    from autobs import parse_doc, norm_space

    items, seen = [], set()
    for page in range(1, pages + 1):
        url = LIST_URL + ("" if page == 1 else "?page=%d" % page)
        r = fetcher.get(url)
        if r is None:
            print("  第 %d 页抓取失败，跳过" % page)
            continue
        doc = parse_doc(r)
        if doc is None:
            continue
        added = 0
        for a in doc.xpath("//a[contains(@href,'/yuedu/shuyuan/content/id/')]"):
            m = re.search(r"/content/id/(\d+)\.html", a.get("href") or "")
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            items.append((m.group(1), norm_space(a.text_content())))
            added += 1
        print("  第 %d 页 +%d（累计 %d）" % (page, added, len(items)))
    return items


def fetch_source(fetcher, sid):
    r = fetcher.get(JSON_URL % sid)
    if r is None:
        return None
    try:
        data = json.loads(r.content.decode("utf-8"))
    except Exception:
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data


# ---------------- 合并 ----------------

def root_domain(host):
    h = re.sub(r"^https?://", "", host or "").split("/")[0].lower()
    parts = h.replace("www.", "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def merge_into_seeds(new_seeds, path=SEEDS_PATH):
    """按主域名去重后追加，原子写。返回 (新增数, 跳过数)。"""
    with open(path, encoding="utf-8") as fp:
        seeds = json.load(fp)
    have = {root_domain(s.get("host")) for s in seeds}
    added, skipped = 0, 0
    for s in new_seeds:
        rd = root_domain(s["host"])
        if rd in have:
            skipped += 1
            continue
        have.add(rd)
        seeds.append(s)
        added += 1
    if added:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(seeds, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    return added, skipped


# ---------------- 主流程 ----------------

def main():
    ap = argparse.ArgumentParser(description="从书源仓库批量导入可用书源")
    ap.add_argument("--pages", type=int, default=6, help="抓取列表页数，每页 100 条（默认 6）")
    ap.add_argument("--limit", type=int, default=100, help="最多转换并实测多少个（默认 100）")
    ap.add_argument("--keyword", default="遮天", help="实测用的搜索词（默认 遮天）")
    ap.add_argument("--min-results", type=int, default=3, help="判定可用的最少结果数（默认 3）")
    ap.add_argument("--merge", action="store_true", help="把通过实测的写入 engine/seeds.json")
    ap.add_argument("--no-test", action="store_true", help="只转换不实测（会写入未经检验的配置）")
    args = ap.parse_args()

    from engine.fetcher import LayeredFetcher
    from engine.search import _search_site

    fetcher = LayeredFetcher(debug=False, headless=True)

    print("① 抓取书源列表（%d 页）" % args.pages)
    catalog = fetch_catalog(fetcher, args.pages)
    # 排除非文本类（漫画/短剧/听书/视频）——按标题粗筛，省一次 JSON 下载
    NON_TEXT = re.compile(r"漫画|短剧|听书|视频|直播|漫剧|动漫|写真|图片|音乐|播客")
    catalog = [(sid, t) for sid, t in catalog if not NON_TEXT.search(t)]
    # 名称像小说站的排在前面，提高单位时间的命中率
    PREF = re.compile(r"小说|书屋|文学|书库|书城|阅读|阁|书院|书吧|笔趣|中文")
    catalog.sort(key=lambda x: 0 if PREF.search(x[1]) else 1)
    batch = catalog[:args.limit]
    print("   候选 %d，本轮处理 %d\n" % (len(catalog), len(batch)))

    print("② 下载并转换书源规则")
    pool, reasons = [], {}
    for i, (sid, title) in enumerate(batch, 1):
        js = fetch_source(fetcher, sid)
        if js is None:
            reasons["JSON 下载/解析失败"] = reasons.get("JSON 下载/解析失败", 0) + 1
            continue
        seed, err = to_seed(js)
        if seed:
            pool.append(seed)
        else:
            reasons[err] = reasons.get(err, 0) + 1
        if i % 25 == 0:
            print("   已处理 %d/%d，可转换 %d" % (i, len(batch), len(pool)))

    print("   可静态转换 %d / %d" % (len(pool), len(batch)))
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print("      %-18s %d" % (k, v))
    print()

    # 按主域名去重
    uniq, seen = [], set()
    for s in pool:
        rd = root_domain(s["host"])
        if rd in seen:
            continue
        seen.add(rd)
        uniq.append(s)

    if args.no_test:
        passed = uniq
        print("③ 跳过实测（--no-test）\n")
    else:
        print("③ 实测搜索（关键词「%s」，需 ≥%d 条）" % (args.keyword, args.min_results))
        passed = []
        for i, s in enumerate(uniq, 1):
            t0 = time.time()
            try:
                out = _search_site(fetcher, s, args.keyword)
            except Exception:
                out = []
            dt = time.time() - t0
            if len(out) >= args.min_results:
                passed.append(s)
                print("   [%2d/%2d] ✓ %-18s %2d 条 %4.1fs  %s"
                      % (i, len(uniq), s["name"][:16], len(out), dt, s["host"][:34]))
        print("   通过 %d / %d\n" % (len(passed), len(uniq)))

    if not passed:
        print("没有可用的新站点。")
        return

    print("④ 结果")
    for s in passed:
        print("   %-18s %-34s %s" % (s["name"], s["host"][:32], s["url"][:52]))

    if args.merge:
        added, skipped = merge_into_seeds(passed)
        print("\n已写入 %s：新增 %d 个，跳过重复 %d 个" % (SEEDS_PATH, added, skipped))
        print("提示：重启 bookhub 后生效。")
    else:
        print("\n（未写入配置。确认无误后加 --merge 再跑一次，或用 --merge 直接合并）")
        print("可用配置片段：")
        print(json.dumps(passed, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
