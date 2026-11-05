# -*- coding: utf-8 -*-
"""L3 依赖自检：缺包 / 缺浏览器 / 两者齐全三种情况。"""
import os
import sys

from engine import fetcher as ft


def _no_drission(monkeypatch):
    monkeypatch.setitem(sys.modules, "DrissionPage", None)


def test_missing_package(monkeypatch):
    _no_drission(monkeypatch)
    ok, why = ft.l3_available()
    assert ok is False
    assert "DrissionPage" in why


def test_missing_browser(monkeypatch):
    monkeypatch.setitem(sys.modules, "DrissionPage", type(sys)("DrissionPage"))
    monkeypatch.setattr(os.path, "exists", lambda p: False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ok, why = ft.l3_available()
    assert ok is False
    assert "Chrome" in why or "Edge" in why


def test_ok_when_both_present(monkeypatch):
    monkeypatch.setitem(sys.modules, "DrissionPage", type(sys)("DrissionPage"))
    monkeypatch.setattr(os.path, "exists", lambda p: p.endswith("chrome.exe"))
    ok, why = ft.l3_available()
    assert ok is True
    assert why.endswith("chrome.exe")
