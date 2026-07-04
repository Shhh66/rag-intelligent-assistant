"""文本分块模块 —— MarkdownHeaderTextSplitter（一级） + RecursiveCharacterTextSplitter（二级）

上游 document_loader 已统一输出结构化 Markdown，本模块利用标题层级做语义切分。
"""

import re
import os
from typing import List

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from langchain_core.documents import Document
from config import CHUNK_SIZE, CHUNK_OVERLAP, CHUNK_MERGE_RATIO, SPLIT_HEADERS


# ═══════════════════════════════════════════════════════════════
# CET-6 专项结构检测（兼容旧逻辑）
# ═══════════════════════════════════════════════════════════════

def _detect_structure(text: str) -> list:
    """扫描文本，识别 CET-6 等标准化文档的结构标记"""
    markers = []
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
    for m in re.finditer(r'Section\s+([A-C])\b', text):
        markers.append((m.start(), f'Section {m.group(1)}'))
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


# ═══════════════════════════════════════════════════════════════
# 主入口：两级 Markdown 切分
# ═══════════════════════════════════════════════════════════════

def split_documents(docs: List[Document]) -> List[Document]:
    """MarkdownHeaderTextSplitter（一级） + RecursiveCharacterTextSplitter（二级）

    坑位修补：
      1. 标题路径拼入正文 `【章节：H1 > H2 > H3】`
      2. 孤儿块兜底 metadata
      3. 层级断层向上继承
      4. 二级保留段落优先 separators 保护表格/代码块
      5. 小章节合并（同级+同父级，优先向前）
      6. 原始元数据透传 + 标题元数据增量追加
      7. 空标题块过滤（< 10 字符）
    """
    # 一级：Markdown 标题拆分
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=SPLIT_HEADERS,
        strip_headers=True,  # 移除正文中的原生 # 标题行，避免和拼接的【章节】重复
    )

    # 二级：语义段落拆分（保护表格/代码块边界）
    secondary_splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=[
            "\n\n",       # 段落边界（表格/代码块前后有双换行，靠此不被拆散）
            "\n",         # 普通换行
            "。",         # 中文句号
            "！", "？",   # 感叹号/问号
            "；",         # 分号
            "，",         # 逗号
            " ",          # 最后手段
            "",
        ],
        add_start_index=True,
    )

    merge_threshold = int(CHUNK_SIZE * CHUNK_MERGE_RATIO)

    all_chunks = []
    for doc in docs:
        text = doc.page_content
        if not text.strip():
            continue

        # ── 原始元数据（透传，不覆盖） ──
        source = doc.metadata.get('source', '')
        filename = os.path.basename(source) if source else ''
        page_label = doc.metadata.get('page_label', doc.metadata.get('page', ''))

        # ── 专项结构检测前置（在原文上执行，避免偏移失效） ──
        structure_markers = _detect_structure(text)

        # ── 一级拆分：按 Markdown 标题 ──
        sections = header_splitter.split_text(text)

        # ── 坑2: 孤儿块兜底 ──
        for sec in sections:
            md = sec.metadata
            if not md.get('h1') and not md.get('h2') and not md.get('h3'):
                md['h1'] = '文档前言'

        # ── 坑3: 层级断层继承 ──
        _fill_header_gaps(sections)

        # ── 坑5: 小章节合并（相邻 + 同级 + 同父级） ──
        sections = _merge_tiny_sections(sections, merge_threshold)

        # ── 生成最终 chunk ──
        for sec in sections:
            sec_text = sec.page_content.strip()
            if not sec_text:
                continue

            # 构建标题路径
            header_path = _build_header_path(sec.metadata)

            # 获取专项结构上下文（取内容开头位置匹配）
            struct_ctx = ''
            if structure_markers:
                try:
                    pos = text.index(sec_text[:60])
                    struct_ctx = _get_section_context(pos, structure_markers)
                except ValueError:
                    pass

            # 二级拆分（章节 > chunk_size）
            if len(sec_text) > CHUNK_SIZE:
                sub_doc = Document(page_content=sec_text)
                sub_chunks = secondary_splitter.split_documents([sub_doc])

                for sc in sub_chunks:
                    # 拼接标题路径到正文开头
                    enriched = f"{header_path}\n{sc.page_content.strip()}" if header_path else sc.page_content.strip()
                    all_chunks.append(_make_chunk(
                        enriched, sc.metadata, sec.metadata,
                        filename, page_label, struct_ctx,
                    ))
            else:
                # 章节 < chunk_size，直接作为一块
                enriched = f"{header_path}\n{sec_text}" if header_path else sec_text
                all_chunks.append(_make_chunk(
                    enriched, {}, sec.metadata,
                    filename, page_label, struct_ctx,
                ))

    # ── 坑7: 空标题块过滤 ──
    all_chunks = [c for c in all_chunks if len(c.page_content.strip()) >= 10]

    print(f"   📦 分块完成: {len(docs)} 个文档 → {len(all_chunks)} 个文本块")
    return all_chunks


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _build_header_path(metadata: dict) -> str:
    """构建标题路径：`【章节：H1 > H2 > H3】`

    不使用 ## 格式，避免被 MarkdownHeaderTextSplitter 二次识别为标题。
    """
    parts = []
    for key in ('h1', 'h2', 'h3'):
        val = metadata.get(key, '')
        if val and val != '文档前言':
            parts.append(val)
    if not parts and metadata.get('h1') == '文档前言':
        parts.append('文档前言')
    return f"【章节：{' > '.join(parts)}】" if parts else ""


def _fill_header_gaps(sections: list):
    """修复层级断层：H1→H3 跳级时，H3 继承最近的非空上级标题"""
    last_h1, last_h2, last_h3 = '', '', ''
    for sec in sections:
        md = sec.metadata
        if md.get('h1'):
            last_h1 = md['h1']
        if md.get('h2'):
            last_h2 = md['h2']
        if not md.get('h1'):
            md['h1'] = last_h1
        if not md.get('h2') and md.get('h3'):
            md['h2'] = last_h2


def _merge_tiny_sections(sections: list, threshold: int) -> list:
    """合并过小章节（< threshold）到相邻同级+同父级章节

    优先向前合并，确保不跨语义层级。
    """
    if len(sections) <= 1:
        return sections

    merged = []
    i = 0
    while i < len(sections):
        sec = sections[i]
        text_len = len(sec.page_content.strip())

        if text_len >= threshold or text_len == 0:
            merged.append(sec)
            i += 1
            continue

        # 查找可合并的目标（优先向前，其次向后）
        target = None

        # 先看前一个
        if i > 0 and _same_level(sec.metadata, sections[i - 1].metadata):
            target = sections[i - 1]
        # 再看后一个
        elif i + 1 < len(sections) and _same_level(sec.metadata, sections[i + 1].metadata):
            # 向前合并当前到前一个，如果前一个不符合条件，才往后合
            if i > 0:
                target = sections[i - 1]

        if target is not None:
            combined_text = target.page_content + "\n\n" + sec.page_content
            merged_doc = Document(page_content=combined_text, metadata=target.metadata)
            # 替换 merged 中最后一个元素
            if merged and merged[-1] is target:
                merged[-1] = merged_doc
            else:
                merged.append(merged_doc)
            i += 1
        else:
            merged.append(sec)
            i += 1

    return merged


def _same_level(md1: dict, md2: dict) -> bool:
    """判断两个块是否同级+同父级

    同级判断：两个块存在的最深标题层级相同
    同父级判断：共享标题层级的值相同
    """
    # 找到各自最深的标题层级
    h_path_1 = [md1.get(k, '') for k in ('h1', 'h2', 'h3')]
    h_path_2 = [md2.get(k, '') for k in ('h1', 'h2', 'h3')]

    # 最深层级：最后一个非空值的位置
    depth1 = max(i for i, v in enumerate(h_path_1) if v) if any(h_path_1) else -1
    depth2 = max(i for i, v in enumerate(h_path_2) if v) if any(h_path_2) else -1

    if depth1 != depth2 or depth1 < 0:
        return False

    # 父级路径必须完全一致
    for d in range(depth1 + 1):
        if h_path_1[d] != h_path_2[d]:
            return False
    return True


def _make_chunk(
    text: str,
    chunk_meta: dict,
    header_meta: dict,
    filename: str,
    page_label: str,
    struct_ctx: str,
) -> Document:
    """构建最终 Document：元数据透传 + 增量追加"""
    # 基础元数据透传
    meta = {}

    if filename:
        meta['source'] = filename
    if page_label:
        meta['page_label'] = page_label

    # 标题元数据增量追加
    for k in ('h1', 'h2', 'h3'):
        if header_meta.get(k):
            meta[k] = header_meta[k]

    # 专项结构标签
    if struct_ctx:
        meta['struct'] = struct_ctx

    # 二级切分的 start_index（如果有）
    if chunk_meta.get('start_index') is not None:
        meta['start_index'] = chunk_meta['start_index']

    return Document(page_content=text, metadata=meta)
