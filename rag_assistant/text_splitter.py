"""文本分块模块 —— 将长文档切分成适合检索的小块，并为每个块标注文档结构位置"""

import re
from typing import List

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from config import CHUNK_SIZE, CHUNK_OVERLAP


def _detect_structure(text: str) -> list:
    """扫描文本，识别 CET-6 等标准化文档的结构标记，返回 [(位置, 标签), ...]"""
    markers = []
    # Part I-IV
    for m in re.finditer(r'Part\s+(I{1,3}V?)\b', text):
        after = text[m.end():m.end()+40].strip()
        if 'writing' in after.lower():
            markers.append((m.start(), 'Part I Writing'))
        elif 'listen' in after.lower():
            markers.append((m.start(), 'Part II Listening'))
        elif 'read' in after.lower():
            markers.append((m.start(), 'Part III Reading'))
        elif 'translat' in after.lower():
            markers.append((m.start(), 'Part IV Translation'))
        else:
            markers.append((m.start(), f'Part {m.group(1)}'))
    # Section A/B/C
    for m in re.finditer(r'Section\s+([A-C])\b', text):
        markers.append((m.start(), f'Section {m.group(1)}'))
    # Passage One/Two/Three
    for m in re.finditer(r'Passage\s+(One|Two|Three)\b', text):
        markers.append((m.start(), f'Passage {m.group(1)}'))
    markers.sort(key=lambda x: x[0])
    return markers


def _get_section_context(pos: int, markers: list) -> str:
    """根据字符位置确定当前所属的 Part → Section → Passage"""
    current_part = ''
    current_section = ''
    current_passage = ''
    for m_pos, label in markers:
        if m_pos > pos:
            break
        if label.startswith('Part '):
            current_part = label
        elif label.startswith('Section '):
            current_section = label
        elif label.startswith('Passage '):
            current_passage = label
    parts = [p for p in [current_part, current_section, current_passage] if p]
    return ' > '.join(parts)


def split_documents(docs: List[Document]) -> List[Document]:
    """将文档列表切分成小块，并为每块标注所属的文档结构位置"""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )

    enriched_chunks = []
    for doc in docs:
        # 1. 检测整篇文档的结构标记
        markers = _detect_structure(doc.page_content)
        # 2. 获取来源文件名和页码
        source = doc.metadata.get('source', '')
        page_label = doc.metadata.get('page_label', doc.metadata.get('page', ''))
        # 3. 对文档分块
        chunks = text_splitter.split_documents([doc])
        # 4. 给每块标注：页码 + 文档名 + 检测到的结构上下文
        for chunk in chunks:
            pos = doc.page_content.find(chunk.page_content[:80])
            # 结构上下文
            struct = _get_section_context(pos, markers) if pos >= 0 else ''
            # 页码信息（PyPDF 的 page_label 是页码，从1开始）
            tag = ''
            if page_label:
                tag = f'第{page_label}页'
                if struct:
                    tag += f' - {struct}'
            elif struct:
                tag = struct

            if tag:
                enriched_text = f"[{tag}] {chunk.page_content}"
                chunk = Document(page_content=enriched_text, metadata=chunk.metadata)
            enriched_chunks.append(chunk)

    print(f"   📦 分块完成（含页码+结构标注）: {len(docs)} 个文档 → {len(enriched_chunks)} 个文本块")
    return enriched_chunks


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    from document_loader import load_file
    import os

    # 创建测试文件
    test_file = "test_split_sample.txt"
    content = (
        "第一章：入门指南\n\n"
        "这是一个很长的测试文档。" * 50 +
        "第二章：进阶教程\n\n"
        "这部分包含了更深入的内容。" * 50
    )
    with open(test_file, "w", encoding="utf-8") as f:
        f.write(content)

    # 加载 → 分块
    docs = load_file(test_file)
    chunks = split_documents(docs)

    print(f"\n🎉 分块成功！共 {len(chunks)} 个文本块")
    print(f"第1块预览（前80字）: {chunks[0].page_content[:80]}...")
    print(f"第1块字数: {len(chunks[0].page_content)}")

    os.remove(test_file)
