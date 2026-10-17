'''
为整个工程提供统一的绝对路径
'''
import os
import sys
'''
sys.path：当前 Python 进程模块的路径搜索列表。
一次 Python 运行 = 一个当前进程 = 一份属于这个进程的 sys.path。
'''
def get_project_root() -> str:
    '''
    获取工程所在的根目录
    :return:字符串根目录
    '''
    #获取当前运行的这个 .py 文件的绝对路径（包含文件名）。
    current_file = os.path.abspath(__file__)
    #获取工程的根目录，先获取文件的所在文件夹绝对路径(向上跳一级)
    current_dir = os.path.dirname(current_file)
    #获取工程根目录
    project_root = os.path.dirname(current_dir)
    return project_root
'''
`ensure_project_root_in_syspath()` 主要用于解决项目根目录不在当前 Python 进程模块搜索路径中的问题。
在从项目根目录正常启动程序时，项目根目录通常已经存在于 `sys.path`，因此一般不会出现导包问题；
但当从项目外部目录启动、使用不同的启动方式（如 FastAPI/Uvicorn）、通过 IDE 或脚本启动，或者项目存在多个入口文件时，
`sys.path` 的内容可能发生变化，导致 Python 无法找到项目内部模块并出现 `ModuleNotFoundError`。
因此，该函数的必要性主要在于提供一种路径保险机制，通过将项目根目录主动加入 `sys.path` 并放在首位，保
证项目无论从何处、以何种入口启动，都能更稳定地进行项目内部模块导入。
'''
'''
假设项目目录为 `AgentProject/`，下面有 `main.py` 和 `utils/tool.py`，如果进入 `utils` 文件夹后直接启动 `tool.py`，
此时 Python 通常会将 `tool.py` 所在的 `utils/` 目录加入当前进程的 `sys.path`，例如 `D:/AgentProject/utils/`，
而项目根目录 `D:/AgentProject/` 并不会因为它是上一级目录就自动加入搜索路径。当 `tool.py` 执行 `from AgentProject import xxx` 时，
Python 会根据 `sys.path` 中的路径寻找 `AgentProject`，如果搜索路径中只有 `D:/AgentProject/utils/`，
它就会尝试寻找 `D:/AgentProject/utils/AgentProject/`，但实际的 `AgentProject/` 位于 `D:/AgentProject/`，
因此找不到对应模块，可能出现 `ModuleNotFoundError`。这说明 Python 导包并不会自动从当前目录向上级目录搜索，
而是依赖当前进程的 `sys.path`；因此在这种情况下，可以通过 `ensure_project_root_in_syspath()` 
将 `D:/AgentProject/` 主动加入 `sys.path`，从而让 Python 能够找到项目根目录下的模块。

直接运行 .py 文件时，Python 通常会把入口脚本所在目录放入当前进程的模块搜索路径 sys.path；
因此从项目根目录启动和从子目录启动，模块搜索路径可能不同。
'''
def ensure_project_root_in_syspath():
    if get_project_root() not in sys.path:
        sys.path.insert(0, get_project_root())

def get_abs_path(relative_path : str) -> str:
    '''
    传递相对路径，得到绝对路径
    :param relative_path:
    :return: 绝对路径
    '''
    project_root = get_project_root()
    return os.path.join(project_root, relative_path)

if __name__ == '__main__':
    print(get_abs_path('config/config.txt'))


