import sys, os
from utils.path_tool import ensure_project_root_in_syspath
ensure_project_root_in_syspath()

from abc import ABC,abstractmethod
from typing import Optional
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.chat_models.tongyi import BaseModel
from utils.config_handler import rag_conf
from utils.env_utils import DASHSCOPE_API_KEY,DASHSCOPE_BASE_URL
class BaseModelFactory(ABC):
    @abstractmethod
    def generator(self) -> Optional[Embeddings | BaseModel]:
        pass


class ChatModelFactory(BaseModelFactory):
    def generator(self) -> Optional[Embeddings | BaseModel]:
        return ChatOpenAI(model=rag_conf['chat_model_name'],
                          api_key = DASHSCOPE_API_KEY,
                          base_url = DASHSCOPE_BASE_URL)

class EmbeddingModelFactory(BaseModelFactory):
    def generator(self) -> Optional[Embeddings | BaseModel]:
        return DashScopeEmbeddings(model=rag_conf['embedding_model_name'])
chat_model = ChatModelFactory().generator()
embedding_model = EmbeddingModelFactory().generator()

if __name__ == '__main__':
    # res = chat_model.invoke("你好")
    # print(res)


    res = chat_model.invoke("你好")

    print(res.content)