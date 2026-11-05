# -*- coding: utf-8 -*-
"""engine/fetcher.py 节流：按站限速，跨站不互相排队。"""
import time

from engine.fetcher import LayeredFetcher


def _f():
    return LayeredFetcher(debug=False, headless=True)


def test_throttle_is_per_host():
    f = _f()
    t0 = time.time()
    f._throttle("http://a.test/x")
    f._throttle("http://b.test/y")
    f._throttle("http://c.test/z")
    f._throttle("http://d.test/w")
    assert time.time() - t0 < 0.3, "不同站点不该挤同一个闸门"


def test_throttle_same_host_still_waits():
    f = _f()
    f._throttle("http://a.test/1")
    t1 = time.time()
    f._throttle("http://a.test/2")
    assert time.time() - t1 >= 0.5, "同一站点仍要保持 ≥0.6s 间隔"


def test_throttle_concurrent_same_host_serialized():
    """两个线程同时冲同一站：第二次必须等到 ≥0.6s 之后。"""
    import threading
    f = _f()
    stamps = []
    lock = threading.Lock()

    def hit(path):
        f._throttle("http://a.test/" + path)
        with lock:
            stamps.append(time.time())

    th = [threading.Thread(target=hit, args=(str(i),)) for i in range(2)]
    for t in th:
        t.start()
    for t in th:
        t.join()
    stamps.sort()
    assert stamps[1] - stamps[0] >= 0.5
