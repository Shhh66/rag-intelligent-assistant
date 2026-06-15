"""文档加载模块 —— 支持 PDF、Word、TXT 文件"""

import os
from typing import List

from langchain_core.documents import Document


def _load_txt(file_path: str) -> List[Document]:
    """加载纯文本文件"""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return [Document(page_content=text, metadata={"source": file_path})]


def _load_docx(file_path: str) -> List[Document]:
    """加载 Word 文件（用轻量 docx2txt，避免拉入 torchvision 等重依赖）"""
    import docx2txt
    text = docx2txt.process(file_path)
    return [Document(page_content=text, metadata={"source": file_path})]


def _load_pdf(file_path: str) -> List[Document]:
    """加载 PDF 文件"""
    from langchain_community.document_loaders import PyPDFLoader
    return PyPDFLoader(file_path).load()


def load_file(file_path: str) -> List[Document]:
    """根据文件类型，加载单个文件并返回文档列表"""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        docs = _load_pdf(file_path)
    elif ext == ".docx":
        docs = _load_docx(file_path)
    elif ext == ".txt":
        docs = _load_txt(file_path)
    else:
        raise ValueError(f"不支持的文件类型: {ext}（仅支持 PDF、DOCX、TXT）")

    print(f"   ✅ 已加载: {os.path.basename(file_path)}（{len(docs)} 页/段）")
    return docs


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    # 创建测试用的 TXT 文件
    test_file = "test_sample.txt"
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("这是第一段测试内容。\n\n这是第二段测试内容。\n\n这是第三段测试内容。")

    docs = load_file(test_file)
    print(f"\n🎉 加载成功！共 {len(docs)} 个文档")
    print(f"预览前100字: {docs[0].page_content[:100]}...")

    # 清理测试文件
    os.remove(test_file)
