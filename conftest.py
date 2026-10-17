"""pytest 全局配置。

作用：把项目根目录加入 sys.path，使 tests/ 下的测试能够 import 项目内的
utils、rag、agent 等模块。

为什么需要这个文件：
    直接用 `pytest` 命令运行时，sys.path[0] 是 pytest 可执行文件所在目录，
    而不是当前工作目录，因此 `from utils.path_tool import ...` 会抛
    ModuleNotFoundError。用 `python -m pytest` 时，当前目录才会被加入
    sys.path —— 这导致本地和 CI 行为不一致。

    放在项目根的 conftest.py 由 pytest 在收集测试前自动导入，
    无论用哪种方式调用都能生效，不依赖 pytest 的版本或 ini 配置。
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
