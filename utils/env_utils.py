import os

# 优先读取系统环境变量；若项目根目录存在 .env 文件，则从中补齐缺失的项。
# override=False（默认）表示 .env 不会覆盖已存在的系统环境变量，
# 因此「自己配系统环境变量」和「用 .env 文件」两种方式都能正常工作。
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # 未安装 python-dotenv 时静默跳过，仅依赖系统环境变量
    pass

# 复制粘贴环境变量时很容易带入首尾的空格、制表符或换行（尤其在 CI 的
# Secrets 面板里粘贴），这些不可见字符会一路传到 httpx，最终抛
# InvalidURL: Invalid non-printable ASCII character in URL。
# 因此这里统一 strip()，并为 BASE_URL 提供默认值。
_DEFAULT_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'

DASHSCOPE_API_KEY = (os.getenv('DASHSCOPE_API_KEY') or '').strip()
DASHSCOPE_BASE_URL = (os.getenv('DASHSCOPE_BASE_URL') or '').strip() or _DEFAULT_BASE_URL
