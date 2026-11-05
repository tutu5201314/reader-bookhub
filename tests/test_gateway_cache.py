# -*- coding: utf-8 -*-
"""网关缓存链路测试（不联网）。

覆盖 OPTIMIZATION.md 里的三条 P0：
  1. 种子沉淀的导入路径（engine.* 与扁平导入都要能走通）
  2. 章节缓存原子写 + 损坏缓存自愈
  3. 正文分页中断时不落缓存（避免半章永久化）

跑法：python -m pytest tests/test_gateway_cache.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from engine import gateway as gw  # noqa: E402

BODY = "这是一段足够长的正文内容，用来模拟真实章节。" * 4   # > 50 字


class _Resp:
    """最小响应桩：parse_doc 只用到 content/text/url/browser。"""
    browser = False
    status_code = 200

    def __init__(self, html, url="http://x.test/ch/1"):
        self.content = html.encode("utf-8")
        self.text = html
        self.url = url


def _page(body):
    return ("<html><head><title>t</title></head><body>"
            "<div id='c'>%s</div></body></html>" % body)


def _page_with_next(body, href):
    """带「下一页」锚点的页面：正文分页循环会顺着它继续抓。"""
    return ("<html><head><title>t</title></head><body>"
            "<div id='c'>%s</div><a href='%s'>下一页</a></body></html>"
            % (body, href))


class _Fetcher:
    """按顺序吐出预置页面（整页 HTML）；None 表示这一页抓取失败。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if not self.pages:
            return None
        item = self.pages.pop(0)
        return None if item is None else _Resp(item, url)


def _make_store(tmp_path, monkeypatch, chapters=2, enable_next=False):
    monkeypatch.setattr(gw, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gw, "BOOKS_DIR", str(tmp_path / "books"))
    monkeypatch.setattr(gw, "CACHE_DIR", str(tmp_path / "cache"))
    store = gw.BookStore()
    store._books["bid1"] = {
        "id": "bid1",
        "url": "http://x.test/book/1",
        "title": "测试书",
        "author": "作者",
        "src": {"content_xpath": "//div[@id='c']",
                "enable_content_next": enable_next},
        "chapters": [{"title": "第%d章" % i, "url": "http://x.test/ch/%d" % i}
                     for i in range(1, chapters + 1)],
        "resolved": True,
        "resolved_at": 9e18,   # 永远新鲜，避免触发重新解析
        "shelf": True,
    }
    # 预取会开后台线程，测试里关掉，避免和断言抢时间
    store._spawn_prefetch = lambda *a, **k: None
    return store


def _cache_file(tmp_path, bid, url):
    import hashlib
    return os.path.join(str(tmp_path / "cache"), bid,
                        hashlib.md5(url.encode()).hexdigest()[:16] + ".txt")


def test_normal_fetch_writes_cache_atomically(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    fetcher = _Fetcher([_page(BODY)])
    text = store.fetch_chapter("bid1", 1, fetcher)

    assert text and len(text) >= 50
    path = _cache_file(tmp_path, "bid1", "http://x.test/ch/1")
    assert os.path.exists(path)
    assert open(path, encoding="utf-8").read() == text
    assert not os.path.exists(path + ".tmp"), "临时文件应已被 os.replace 消费"


def test_interrupted_pagination_is_not_cached(tmp_path, monkeypatch):
    """第 2 页失败：正文照常返回，但绝不落缓存（否则半章永久化）。"""
    store = _make_store(tmp_path, monkeypatch, enable_next=True)
    fetcher = _Fetcher([_page_with_next(BODY, "/ch/1_2"), None])
    text = store.fetch_chapter("bid1", 1, fetcher)

    assert len(fetcher.calls) == 2, "应当顺着「下一页」抓到第 2 页并失败"
    assert text and len(text) >= 50, "已抓到的部分应当返回给用户"
    assert not os.path.exists(_cache_file(tmp_path, "bid1", "http://x.test/ch/1"))


def test_corrupt_cache_is_removed_and_refetched(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    path = _cache_file(tmp_path, "bid1", "http://x.test/ch/1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write("半截")          # 模拟写到一半被打断

    text = store.fetch_chapter("bid1", 1, _Fetcher([_page(BODY)]))

    assert text and len(text) >= 50
    assert open(path, encoding="utf-8").read() == text


def test_try_sediment_imports_and_calls(tmp_path, monkeypatch):
    """_try_sediment 必须能导入到 engine.search（原来裸 from search 直接 ImportError）。"""
    store = _make_store(tmp_path, monkeypatch)
    called = {}

    def _fake_add_seed(src, fetcher):
        called["src"] = src
        return {"name": "假站点"}

    from engine import search as engine_search
    monkeypatch.setattr(engine_search, "maybe_add_seed", _fake_add_seed)

    store._try_sediment("bid1")

    assert called.get("src"), "maybe_add_seed 没有被调用，说明导入路径仍然不通"
