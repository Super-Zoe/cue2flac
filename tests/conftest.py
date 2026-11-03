"""pytest 公共配置：确保 ``tests/`` 目录内的辅助模块可被直接导入。

``tests/`` 不是 Python 包（没有 ``__init__.py``），pytest 会把每个测试
文件的所在目录加入 ``sys.path``，但显式声明更稳妥——尤其是当从其他
工作目录运行时。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
