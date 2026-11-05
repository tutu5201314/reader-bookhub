# -*- coding: utf-8 -*-
"""体检探活诊断：把「站点不可达」拆成能排查的原因。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

from engine.fetcher import LayeredFetcher  # noqa: E402


class _Raw:
    """裸 L1 响应替身。"""

    def __init__(self, code=403, body="", headers=None):
        self.status_code = code
        self.text = body
        self.content = body.encode("utf-8")
        self.headers = headers or {}


class _Session:
    def __init__(self, raw=None, exc=None):
        self.raw = raw
        self.exc = exc

    def get(self, url, timeout=None, **kw):
        if self.exc is not None:
            raise self.exc
        return self.raw


def _fetcher(raw=None, exc=None, l2=None):
    f = LayeredFetcher(headless=True)
    f.s = _Session(raw, exc)
    f._req_quick = lambda *a, **k: l2
    return f


CF_BLOCK = "<html><head><title>Attention Required! | Cloudflare</title></head></html>"
CF_CHALLENGE = "<html><title>Just a moment...</title></html>"


def test_ok_when_l1_returns_clean_page():
    f = _fetcher(_Raw(200, "<html><body>" + "正文" * 400 + "</body></html>"))
    d = f.diagnose("http://x.test/")
    assert d["ok"] is True and d["code"] == 200


def test_l2_rescue_is_not_reported_as_down():
    """笔趣阁tw 这类站 L1 恒 403、L2 稳定 200：不能误报成站点挂了。"""
    f = _fetcher(_Raw(403, CF_BLOCK, {"cf-ray": "x"}), l2=_Raw(200, "ok"))
    d = f.diagnose("http://x.test/")
    assert d["ok"] is True
    assert "L2" in d["reason"]


def test_cloudflare_hard_block_is_named():
    f = _fetcher(_Raw(403, CF_BLOCK, {"cf-ray": "x"}), l2=None)
    d = f.diagnose("http://x.test/")
    assert d["ok"] is False and d["code"] == 403
    assert "Cloudflare 硬拦截" in d["reason"]


def test_cloudflare_challenge_is_distinguished():
    f = _fetcher(_Raw(403, CF_CHALLENGE, {"cf-ray": "x"}), l2=None)
    d = f.diagnose("http://x.test/")
    assert "人机挑战" in d["reason"], "挑战页与硬拦截要分开，前者 L3 可能过"


def test_timeout_is_not_retried_through_l2():
    called = {"l2": 0}
    f = _fetcher(exc=requests.Timeout())
    f._req_quick = lambda *a, **k: called.__setitem__("l2", called["l2"] + 1)
    d = f.diagnose("http://x.test/")
    assert d["reason"] == "连接超时"
    assert called["l2"] == 0, "超时是网络层问题，L2 救不了，别再等 20s"


def test_404_is_named():
    f = _fetcher(_Raw(404, "<html>not found</html>"), l2=None)
    d = f.diagnose("http://x.test/")
    assert d["code"] == 404 and "404" in d["reason"]
