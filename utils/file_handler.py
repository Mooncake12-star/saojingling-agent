import os,hashlib
from utils.logger_handler import logger
from langchain_core.documents import Document
from langchain_community.document_loaders import PyPDFLoader,TextLoader

def get_file_mds_hex(filepath: str):#获取文件的md5的十六进制字符串
     #exists:只要这个路径指向的东西（无论是文件还是文件夹）存在，它就返回 True。
     if not os.path.exists(filepath):
         logger.error(f"[md5计算]文件{filepath}不存在")
         return
     #isfile:判断是否是文件,必须是文件才会返回ture
     if not os.path.isfile(filepath):
         logger.error(f"[md5计算]路径{filepath}不是文件")
         return

     md5_obj = hashlib.md5()
     chunk_size = 4096  #4KB分片，避免文件过大爆内存
     try:
         with open(filepath,'rb') as f:   #必须使用二进制读取
             #(:=)称为海象操作
             while chunk:=f.read(chunk_size):
                 md5_obj.update(chunk)
             '''
             while chunk:=f.read(chunk_size)
             等价于 
             while True:
                 chunk = f.read(chunk_size) 
             if not chunk: 
             break    
             '''

             md5_hex = md5_obj.hexdigest()
             return md5_hex
     except Exception as e:
         logger.error(f"计算文件{filepath}md5失败，{str(e)}")
         return  None



def listdir_with_allowed_type(path:str,allowed_types:tuple[str]):#返回文件夹内的文件列表（允许的文件后缀）
    files = []
    #isfile:指的是文件里的具体内容
    #isdir：指的是文件夹这个容器
    if not os.path.isdir(path):
        logger.error(f"[listdir_with_allowed_type]{path}不是文件夹")
        return allowed_types
    #os.listdir:列出文件夹里的所有清单,f返回值收到的是一个个文件名而不是路径
    for f in os.listdir(path):
        if f.endswith(allowed_types):
            files.append(os.path.join(path,f))

    return tuple(files)

def pdf_loader(filepath:str,passwd=None) -> list[Document]:
    '''
    Document(
        page_content="这是文档的具体内容",
        metadata={"source": "test.txt"}
    )
    '''
    #流程：读取每一页内容，将每一页内容封装成一个Document对象，然后再存入列表里
    return PyPDFLoader(filepath,passwd).load()

def txt_loader(filepath:str) -> list[Document]:
    return TextLoader(filepath, encoding='utf-8').load()


if __name__ == '__main__':
    from utils.path_tool import get_project_root
    docs = txt_loader(os.path.join(get_project_root(), 'data', '扫地机器人100问2.txt'))
    print(docs[0])