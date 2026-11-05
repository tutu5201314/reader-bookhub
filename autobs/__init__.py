# -*- coding: utf-8 -*-
"""autobs 包入口。

历史原因：`autobs/` 里只有一个 `autobs.py`，各处靠 `sys.path.insert(0, "autobs")`
再 `from autobs import ...` 当模块导入 —— 谁先导入谁就把路径塞好，导入顺序成了
隐式契约（`python -c "from autobs import resolve_book"` 曾直接 ImportError）。
补上这个 `__init__.py` 后，仓库根在 sys.path 上时 `import autobs` 命中包，
`from .autobs import *` 把公开符号（resolve_book / parse_doc / is_waf_page …）
原样导出；那条 sys.path hack 也仍然有效（目录在前，命中的是 autobs.py 文件），
两条路径都通，不再依赖导入顺序。
"""

from .autobs import *          # noqa: F401,F403
from .autobs import UA, READER_UA, is_waf_page  # noqa: F401  常用符号显式列出
