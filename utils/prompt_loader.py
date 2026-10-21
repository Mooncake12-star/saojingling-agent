"""提示词加载。

为什么要加 lru_cache：
    原实现每次调用都 open().read() 一次。Agent 每个请求都要 load_main_prompts()，
    RAG 服务每个 scope 都要 load_rag_prompts()。文件不大，但这是**同步磁盘 I/O
    跑在事件循环或工作线程里**：100 并发时就是 100 次 open/read/close，
    在机械盘或容器 overlayfs 上会变成可观的等待，而且完全没必要——
    提示词在进程生命周期内不会变。

    缓存的是「文件内容」这一个字符串，不可变对象，多线程读安全。
    需要热更新时调用 reload_prompts()（测试里也用它清缓存）。
"""
from functools import lru_cache

from utils.config_handler import prompts_conf
from utils.path_tool import get_abs_path
from utils.logger_handler import logger


@lru_cache(maxsize=None)
def load_main_prompts() -> str:
    try:
       main_prompt_path = get_abs_path(prompts_conf['main_prompt_path'])
    except KeyError as e:
        logger.error(f"[load_main_prompts]在yaml配置项中没有main_prompt_path配置项")
        raise e
    try:
        #.read()拿出文件里的内容
        with open(main_prompt_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"[load_main_prompts]解析提示此错误，{str(e)}")
        raise e


@lru_cache(maxsize=None)
def load_rag_prompts() -> str:
    try:
        rag_prompt_path = get_abs_path(prompts_conf['rag_summarize_prompt_path'])
    except KeyError as e:
        logger.error(f"[load_rag_prompts]在yaml配置项中没有rag_summarize_prompt_path配置项")
        raise e
    try:
        with open(rag_prompt_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"[load_rag_prompts]解析提示此错误，{str(e)}")
        raise e


def reload_prompts() -> None:
    """清缓存，下次调用重新读盘（改完 prompt 不想重启进程时用）。"""
    load_main_prompts.cache_clear()
    load_rag_prompts.cache_clear()


if __name__ == '__main__':
    print(load_main_prompts())
