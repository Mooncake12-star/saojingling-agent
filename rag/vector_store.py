import os
from utils.path_tool import ensure_project_root_in_syspath
ensure_project_root_in_syspath()

from langchain_chroma import Chroma
from langchain_core.documents import Document

from utils.config_handler import chroma_conf
from model.factory import get_embedding_model
from langchain_text_splitters import RecursiveCharacterTextSplitter
from utils.path_tool import get_abs_path
from utils.file_handler import pdf_loader,txt_loader,listdir_with_allowed_type,get_file_mds_hex
from utils.logger_handler import logger


class VectorStoreService:
    def __init__(self):
        self.vector_store = Chroma(
            collection_name=chroma_conf['collection_name'],
            embedding_function=get_embedding_model(),
            persist_directory=get_abs_path(chroma_conf['persist_directory'])
        )
        '''
        文本分割器流程，首先通过length_function计算传入的文本长度，按字符数量计算，
        并按照<=chunk_size的数量切分，切分过程中按照separators的优先级切分，切分的
        内容会尽量保证语意完整，但不一定全是完整句子，每下一次切分都会复制前一次切分文本末尾
        overlap数量的字符，来作为下一次切分的开头，保证上下文尽量不丢失
        
        '''
        self.splitter = RecursiveCharacterTextSplitter(
            #切分的字符大小<=chunk_size
            chunk_size=chroma_conf['chunk_size'],
            #每次切分与上一此切分的重叠数量，保证上下文不丢失
            chunk_overlap=chroma_conf['chunk_overlap'],
            #切分优先级参数
            separators=chroma_conf['separators'],
            #按照字符数计算长度
            length_function=len
        )
    def get_retriever(self):
        return self.vector_store.as_retriever(search_kwargs={'k': chroma_conf['k']})

    def load_document(self):
        '''
        从数据文件夹内读取文件，转为向量存入向量库
        要计算文件的MD5做去重
        :return: None
        '''

        def check_md5_hex(md5_for_check: str):
            if not os.path.exists(get_abs_path(chroma_conf['md5_hex_store'])):
                #创建文件
                 open(get_abs_path(chroma_conf['md5_hex_store']),'w',encoding='utf-8').close()
                 return False    #MD5没处理过

            with open(get_abs_path(chroma_conf['md5_hex_store']),'r',encoding='utf-8') as f:
                for line in f.readlines():
                    line = line.strip()
                    if line == md5_for_check:
                        return True #md5处理过
                return False        #md5没处理过

        def save_md5_hex(md5_for_save: str):
            #w：写入覆盖原内容   a：追加不覆盖原内容
            with open(get_abs_path(chroma_conf['md5_hex_store']),'a',encoding='utf-8') as f:
                f.write(md5_for_save + '\n')

        def get_file_documents(read_path: str):
            if read_path.endswith('txt'):
                return txt_loader(read_path)

            if read_path.endswith('pdf'):
                return pdf_loader(read_path)

            return []     #这里不写【空】，会自动返回None，后续遍历document时会报错

        allowed_files_path: tuple[str] = listdir_with_allowed_type(
            get_abs_path(chroma_conf['data_path']),
            tuple(chroma_conf['allow_knowledge_file_type'])
        )
        for path in allowed_files_path:
            #获取文件的md5
            md5_hex = get_file_mds_hex(path)

            if check_md5_hex(md5_hex):
                logger.info(f"[加载知识库]{path}内容已存在知识库，跳过")
                continue
            try:
                documents:list[Document] = get_file_documents(path)

                if not documents:
                    logger.warning(f'[加载知识库]{path}内没有有效文本内容，跳过')
                    continue
                #split_document是一次性把所有的Docuemnt切分成多个chunk大小的Document
                split_document:list[Document] = self.splitter.split_documents(documents)

                if not split_document:
                    logger.warning(f'[加载知识库]{path}分片后内没有有效文本内容，跳过')
                    continue
                #将内容存入向量库，add_documents接收多个 Document，并负责把这些 Document 批量处理后加入向量数据库。
                self.vector_store.add_documents(split_document)

                #记录这个已经处理过的文件的MD5，避免下次重复加载
                save_md5_hex(md5_hex)

                logger.info(f"[加载知识库]{path}内容加载成功")
            except  Exception as e:
                #exc_info为True会记录详细的报错堆栈，如果为False仅记录报错信息本身
                logger.error(f"[加载知识库]{path}内容加载失败，{str(e)}",exc_info=True)
                continue

if __name__ == '__main__':
    vs = VectorStoreService()
    vs.load_document()
    retriever = vs.get_retriever()
    res = retriever.invoke('迷路')
    for r in res:
        print(r.page_content)
        print('-'*20)
