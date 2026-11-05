# -*- coding: utf-8 -*-
"""章节缓存配额：统计 + 按最久未读整本清理（OPTIMIZATION.md §7）。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import gateway as gw  # noqa: E402


def _write(cache_dir, bid, name, size, age=0.0):
    d = os.path.join(cache_dir, bid)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as fp:
        fp.write(b"x" * size)
    if age:
        t = time.time() - age
        os.utime(p, (t, t))
    return p


def _setup(tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    monkeypatch.setattr(gw, "CACHE_DIR", cache)
    return cache


def test_cache_stats_sums_per_book(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    _write(cache, "b1", "a.txt", 100, age=100)
    _write(cache, "b1", "b.txt", 50, age=100)
    _write(cache, "b2", "c.txt", 30)

    st = gw.cache_stats()

    assert st["total"] == 180
    by_id = {b["id"]: b for b in st["books"]}
    assert by_id["b1"]["size"] == 150 and by_id["b1"]["files"] == 2
    assert by_id["b2"]["size"] == 30
    # 排序：最久没读的在前，prune 依赖这个顺序
    assert st["books"][0]["id"] == "b1"


def test_prune_removes_oldest_books_only(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    _write(cache, "old", "a.txt", 100, age=10000)
    _write(cache, "new", "b.txt", 100)

    r = gw.prune_cache(max_bytes=150)

    assert r["removed"] == ["old"]
    assert r["freed"] == 100
    assert not os.path.isdir(os.path.join(cache, "old"))
    assert os.path.isdir(os.path.join(cache, "new")), "新书不该被清"


def test_prune_noop_under_quota(tmp_path, monkeypatch):
    cache = _setup(tmp_path, monkeypatch)
    _write(cache, "b1", "a.txt", 10)

    r = gw.prune_cache(max_bytes=1024)

    assert r["removed"] == [] and r["freed"] == 0
    assert os.path.isdir(os.path.join(cache, "b1"))
