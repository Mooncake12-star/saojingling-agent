import sys, os
from utils.path_tool import ensure_project_root_in_syspath
ensure_project_root_in_syspath()

from abc import ABC,abstractmethod
from typing import Optional, Union
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.chat_models.tongyi import BaseModel
from utils.config_handler import rag_conf, agent_conf
from utils.env_utils import DASHSCOPE_API_KEY,DASHSCOPE_BASE_URL

# 运行时护栏配置（见 config/agent.yml 的 guard 段），缺配置时用保守默认值，
# 避免"配置文件手改坏了 → 整个 agent 起不来"这种放大故障。
_GUARD = agent_conf.get("guard") or {}

class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Optional[Union[Embeddings, BaseModel]]:
        pass


class ChatModelFactory(BaseModelFactory):
    def generator(self) -> Optional[Union[Embeddings, BaseModel]]:
        # timeout 必须显式给：openai SDK 默认 600s，模型侧卡住时一轮对话会挂十分钟，
        # 实测就是这样把整批评测拖死的（420s 零输出、进程 CPU 归零）。
        # max_retries 从默认 2 降到 1：失败要快点暴露，不要在一个坏请求上重试三轮。
        return ChatOpenAI(model=rag_conf['chat_model_name'],
                          api_key = DASHSCOPE_API_KEY,
                          base_url = DASHSCOPE_BASE_URL,
                          timeout = float(_GUARD.get("model_timeout", 90)),
                          max_retries = 1)

class EmbeddingModelFactory(BaseModelFactory):
    def generator(self) -> Optional[Union[Embeddings, BaseModel]]:
        return DashScopeEmbeddings(model=rag_conf['embedding_model_name'])

# ──────────────────────────────────────────────
# 惰性单例：首次调用时才创建模型客户端，之后全程复用同一实例
#
# 为什么不在模块顶层直接实例化：
#   顶层实例化是 import 副作用——任何模块只要 from model.factory import ...，
#   就会立刻创建 HTTP 客户端并校验 API Key。这导致：
#   ① 单元测试只想 import 类结构，却被强制初始化真实客户端；
#   ② 缺少 API Key 时连"纯导入"都会失败，CI 无法验证代码结构。
#   改为惰性后，客户端只在真正发起调用的链路上（VectorStoreService /
#   ReactAgent / RagSummarizeService 实例化时）才创建。
# ──────────────────────────────────────────────
_chat_model = None
_embedding_model = None

def get_chat_model():
    global _chat_model
    if _chat_model is None:
        _chat_model = ChatModelFactory().generator()
    return _chat_model

def get_embedding_model():
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = EmbeddingModelFactory().generator()
    return _embedding_model

if __name__ == '__main__':
    res = get_chat_model().invoke("你好")
    print(res.content)
