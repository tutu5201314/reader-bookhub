# -*- coding: utf-8 -*-
"""FIX-21：目录分页上限、章号容错、合并后重排。

注：engine/* 会把 autobs/ 目录塞进 sys.path，于是 `autobs` 这个名字在
sys.modules 里可能指向 autobs/autobs.py 这个文件而不是包。这里直接按文件路径
加载，避免受导入顺序影响。
"""
import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(ROOT, "autobs", "autobs.py")

_spec = importlib.util.spec_from_file_location("autobs_core_for_test", _PATH)
autobs = importlib.util.module_from_spec(_spec)
sys.modules["autobs_core_for_test"] = autobs
_spec.loader.exec_module(autobs)


def _ch(title):
    return {"title": title, "url": "https://x/%s" % abs(hash(title))}


def test_catalog_page_cap_is_large_enough():
    # 20 页 × 20 章只够 400 章，1800+ 章的长篇会被腰斩
    assert autobs.MAX_CATALOG_PAGES >= 100
    assert autobs.CATALOG_MERGE_BUDGET >= 120


def test_chap_num_tolerates_parenthesised_sub_number():
    assert autobs._chap_num("第1 章 高蛮山鹰人部落") == 1
    assert autobs._chap_num("第1849章落去旧时代的残余") == 1849
    assert autobs._chap_num("第268（2） 章 法凯伦世界") == 268
    assert autobs._chap_num("第 268（3）章 光明与智慧女神") == 268
    assert autobs._chap_num("12. 斗破") == 12
    assert autobs._chap_num("番外 一") is None


def test_sort_chapters_by_num_fixes_descending_head():
    # 首页倒序块 100..96 拼在 1..95 之前 → 应被重排成 1..100
    chs = [_ch("第%d章 x" % i) for i in range(100, 95, -1)]
    chs += [_ch("第%d章 x" % i) for i in range(1, 96)]
    out, changed = autobs.sort_chapters_by_num(chs)
    assert changed is True
    assert [c["title"] for c in out] == ["第%d章 x" % i for i in range(1, 101)]


def test_sort_chapters_by_num_keeps_clean_list_and_unparsable():
    clean = [_ch("第%d章 x" % i) for i in range(1, 41)]
    out, changed = autobs.sort_chapters_by_num(clean)
    assert changed is False and out is clean

    with_extra = clean + [_ch("番外 一")]
    out2, changed2 = autobs.sort_chapters_by_num(with_extra)
    assert changed2 is False and out2 is with_extra


def test_sort_chapters_by_num_ignores_tiny_lists():
    tiny = [_ch("第2章 b"), _ch("第1章 a")]
    out, changed = autobs.sort_chapters_by_num(tiny)
    assert changed is False and out is tiny


def test_sort_chapters_by_num_leaves_renumbered_volumes_alone():
    # 分卷重编号：1..210 之后又从 24 开始，按号排序会毁掉正文顺序
    chs = [_ch("第%d章 x" % i) for i in range(1, 211)]
    chs += [_ch("第%d章 x" % i) for i in range(24, 211)]
    chs += [_ch("第%d章 x" % i) for i in range(15, 36)]
    out, changed = autobs.sort_chapters_by_num(chs)
    assert changed is False
    assert out is chs
