"""日志装配：一行一条、可 grep、可被日志系统解析。

结构化的本质不是「用 JSON 打印」，而是**每条日志都带同一组可枚举的字段**，
让 `grep <request_id>` 能捞出一次请求的完整链路，让 ELK/Loki 能按字段过滤。
所以这里做三件事：

1. `ContextFilter` 把 ContextVar 里的 request_id / user_id 塞进每条 record
   —— 业务代码一个参数都不用传，`logger.info("xxx")` 自动带上。
2. `JsonFormatter` 输出单行 JSON（`msg` 保持一句话，堆栈放 `exc` 字段）。
3. `TextFormatter` 保留人眼友好的本地格式，但同样带 rid/uid 字段。

什么时候用哪个：本地开发要人眼看（`ZST_LOG_FORMAT=text`，默认），
容器里要机器收（compose 里设 `json`）。

一个坑：JSON 里的 `msg` 如果本身是多行堆栈，会把「一行一条」破坏掉。
所以异常统一放 `exc` 字段并由 JSON 转义，`msg` 只留一句话。
"""
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any

from utils.log_context import get_request_id, get_user_id
from utils.path_tool import get_abs_path

# 日志保存的根目录
LOG_ROOT = get_abs_path("logs")
os.makedirs(LOG_ROOT, exist_ok=True)

# text = 人眼友好（本地默认）；json = 一行一个 JSON 对象（容器环境）
LOG_FORMAT = os.getenv("ZST_LOG_FORMAT", "text").strip().lower()
LOG_LEVEL = os.getenv("ZST_LOG_LEVEL", "DEBUG").strip().upper()

_LEVEL_NUM = getattr(logging, LOG_LEVEL, logging.DEBUG)

# 第三方库的噪音：每发一次请求打一行 DEBUG，会把业务日志埋掉
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "chromadb", "sentence_transformers")


class ContextFilter(logging.Filter):
    """给每条 record 补上请求级字段。

    用 Filter 而不是在 Formatter 里直接读 ContextVar，是为了让**任何** handler
    （包括别人加的、或第三方库的 handler）都能拿到 `record.request_id`——
    字段挂到 record 上是一次性的，读的地方有多少都不关心。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 已经显式设过就不覆盖：允许某条日志手工标注别的 request_id（如后台任务）
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        if not hasattr(record, "user_id"):
            record.user_id = get_user_id()
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON。字段名固定，方便下游按字段索引。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "line": record.lineno,
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", None),
        }
        # 结构化附件的入口：logger.info("...", extra={"context": {"kb_id": 3}})
        # 这些字段平铺进 JSON，不用把 kv 拼进 msg 字符串里再解析回来
        for key, value in (getattr(record, "context", None) or {}).items():
            payload[key] = value

        payload["msg"] = record.getMessage()
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # ensure_ascii=False：中文按原样输出，否则日志里全是 \uXXXX 没法看
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """本地开发用：和改造前的格式几乎一致，只是多了 rid/uid。"""

    def format(self, record: logging.LogRecord) -> str:
        rid = getattr(record, "request_id", "-")
        uid = getattr(record, "user_id", None)
        # 没登录的请求（健康检查、登录本身）不显示 uid，避免满屏 uid=None
        uid_part = f" uid={uid}" if uid is not None else ""
        base = (
            f"{datetime.fromtimestamp(record.created):%Y-%m-%d %H:%M:%S}"
            f" - {record.name} - {record.levelname} - {record.filename}:{record.lineno}"
            f" - [rid={rid}{uid_part}] - {record.getMessage()}"
        )
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def _build_formatter() -> logging.Formatter:
    return JsonFormatter() if LOG_FORMAT == "json" else TextFormatter()


# 兼容旧名字：外部若引用了它，行为不变（现在自带 rid/uid）
DEFAULT_LOG_FORMAT = _build_formatter()


def get_logger(
        name: str = "agent",
        console_level: int | None = None,
        file_level: int = logging.DEBUG,
        log_file=None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # 避免重复添加Handler
    if logger.handlers:
        return logger

    formatter = _build_formatter()
    context_filter = ContextFilter()

    # 控制台Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level if console_level is not None else _LEVEL_NUM)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)
    logger.addHandler(console_handler)

    # 文件Handler。容器里通常让 stdout 走日志系统、不落盘（根文件系统可只读），
    # 所以留一个开关；默认仍然写文件，本地排查方便。
    if os.getenv("ZST_LOG_TO_FILE", "1") != "0":
        if not log_file:        # 日志文件的存放路径
            log_file = os.path.join(LOG_ROOT, f"{name}_{datetime.now().strftime('%Y%m%d')}.log")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        logger.addHandler(file_handler)

    return logger


def quiet_noisy_loggers() -> None:
    """压掉第三方库的 DEBUG 噪音。启动时调用一次即可。"""
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# 快捷获取日志器
logger = get_logger()


if __name__ == '__main__':
    logger.info("信息日志")
    logger.error("错误日志")
    logger.warning("警告日志")
    logger.debug("调试日志")
