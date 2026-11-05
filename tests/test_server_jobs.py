# -*- coding: utf-8 -*-
"""后台任务回收（OPTIMIZATION.md §8）：TTL + 数量上限。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


def _reset(monkeypatch):
    monkeypatch.setattr(server, "JOBS", {})


def test_finished_job_expires(monkeypatch):
    _reset(monkeypatch)
    j = server._new_job("txt")
    server._finish(j, "done")
    assert j["finished_at"] > 0, "_finish 必须打时间戳，否则永远回收不掉"
    j["finished_at"] = time.time() - server.JOBS_TTL - 5

    server._prune_jobs()

    assert j["id"] not in server.JOBS


def test_running_job_is_kept(monkeypatch):
    _reset(monkeypatch)
    j = server._new_job("txt")          # 没 _finish，仍 running
    j["created"] = time.time() - server.JOBS_TTL * 3

    server._prune_jobs()

    assert j["id"] in server.JOBS, "跑着的任务不能因为老就被回收"


def test_job_count_is_capped(monkeypatch):
    """条数上限只裁「已结束」的记录：跑着的任务不能因为数量多就被扔掉。"""
    _reset(monkeypatch)
    for _ in range(server.JOBS_MAX + 10):
        server._finish(server._new_job("search"), "done")

    done = [j for j in server.JOBS.values() if j.get("status") != "running"]
    assert len(done) <= server.JOBS_MAX, "已结束的记录必须被裁到上限内"


def test_running_jobs_are_not_capped_away(monkeypatch):
    _reset(monkeypatch)
    for _ in range(server.JOBS_MAX + 5):
        server._new_job("search")

    running = [j for j in server.JOBS.values() if j.get("status") == "running"]
    assert len(running) == server.JOBS_MAX + 5, "跑着的任务一条都不能被裁掉"
