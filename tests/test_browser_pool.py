# -*- coding: utf-8 -*-
"""L3 浏览器层：标签页池 + 硬上限等待（不启动真浏览器，用假页面替身）。"""
import threading
import time

from engine.fetcher import BrowserLayer


class _FakeTab:
    """DrissionPage 标签页的最小替身：get() 睡一会儿，html 直接给正常页面。"""

    def __init__(self, delay=0.3):
        self.delay = delay
        self.url = ""
        self.html = ""
        self.closed = False

    def get(self, url, timeout=None):
        time.sleep(self.delay)
        self.url = url
        self.html = "<html><body><div id='c'>正文内容</div></body></html>"

    def new_tab(self, url=None):
        return _FakeTab(self.delay)

    def close(self):
        self.closed = True

    def quit(self):
        self.closed = True


def _layer(pool=2, delay=0.3):
    lyr = BrowserLayer(headless=True, pool_size=pool)
    # 跳过真浏览器启动：直接把假页面塞进去
    lyr._page = _FakeTab(delay)
    return lyr


def test_pool_runs_two_tabs_concurrently():
    lyr = _layer(pool=2, delay=0.4)
    t0 = time.time()
    th = [threading.Thread(target=lyr.get, args=("http://x.test/%d" % i,))
          for i in range(2)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    spent = time.time() - t0
    # 串行要 0.8s+，两 tab 并发约 0.4s
    assert spent < 0.75, "两个 tab 应当并行，实际 %.2fs" % spent


def test_pool_size_one_still_serializes():
    lyr = _layer(pool=1, delay=0.25)
    t0 = time.time()
    th = [threading.Thread(target=lyr.get, args=("http://x.test/%d" % i,))
          for i in range(2)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    assert time.time() - t0 >= 0.45, "池子只有 1 个 tab 时必须排队"


def test_get_returns_browser_response():
    lyr = _layer()
    r = lyr.get("http://x.test/1")
    assert r is not None and r.browser and r.status_code == 200
    assert "正文内容" in r.text
