"""OCR 慢速通道 —— PaddleOCR/PP-Structure 动态懒加载

负责扫描件、图片 PDF、劣质文本层 PDF 的 OCR 识别与结构化提取。

架构原则：
  - 动态懒加载：模块顶部不 import paddleocr，首次调用时才加载（避免 500MB+ 依赖污染快速通道）
  - 全局单例：初始化后缓存在 _engine，避免重复冷启动
  - 错误隔离：OCR 失败不抛异常，返回 None + 错误原因
  - 按需安装：未安装时给出明确安装提示
"""

import logging
import sys
from typing import List, Optional, Tuple

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# ── 全局单例缓存 ──
_engine = None
_engine_available = None  # None=未检测, True/False


def _check_paddleocr() -> bool:
    """检查 PaddleOCR 是否已安装"""
    global _engine_available
    if _engine_available is not None:
        return _engine_available
    try:
        import paddleocr  # noqa: F401
        _engine_available = True
    except ImportError:
        _engine_available = False
    return _engine_available


def get_install_hint() -> str:
    """未安装 PaddleOCR 时的安装提示"""
    return (
        "PaddleOCR 未安装。\n"
        "安装命令（约 500MB）：\n"
        "  pip install paddlepaddle\n"
        "  pip install paddleocr\n"
        "或使用 CPU 精简版：\n"
        "  pip install paddlepaddle-cpu\n"
        "  pip install paddleocr\n"
    )


# ═══════════════════════════════════════════════════════════════
# 主入口：OCR 一页 PDF 图片
# ═══════════════════════════════════════════════════════════════

def ocr_page(
    image_bytes: bytes,
    page_num: int = 0,
    dpi: int = 200,
    source_path: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    """对单页 PDF 图片做 OCR 识别

    Args:
        image_bytes: PNG/JPEG 格式的页面图片
        page_num: 页码（用于日志）
        dpi: 渲染 DPI
        source_path: 源文件路径（用于公式缓存 Key）

    Returns:
        (markdown_text, error_msg)：成功时 error_msg=None，失败时 markdown_text=None
    """
    if not _check_paddleocr():
        return None, get_install_hint()

    global _engine
    if _engine is None:
        try:
            print(f"   ⏳ 首次加载 PaddleOCR 引擎（约 10-30 秒）...", file=sys.stderr, flush=True)
            _engine = _init_engine()
            print(f"   ✅ PaddleOCR 引擎就绪", file=sys.stderr, flush=True)
        except Exception as e:
            msg = f"PaddleOCR 初始化失败: {e}"
            logger.error(msg)
            return None, msg

    try:
        import numpy as np
        from PIL import Image
        import io
        image = Image.open(io.BytesIO(image_bytes))
        img_array = np.array(image)

        # PPStructure → list[dict] with type/table/formula
        # PaddleOCR fallback → [[[bbox, (text, conf)], ...]]
        if hasattr(_engine, 'ocr'):
            # 基础 PaddleOCR 模式
            result = _engine.ocr(img_array)
            if result and result[0]:
                lines = [item[1][0] for item in result[0] if len(item) >= 2]
                return "\n\n".join(lines), None
            return "", None
        else:
            # PP-Structure 模式
            result = _engine(img_array)
            markdown = _structure_result_to_markdown(
                result, img_array=img_array,
                source_path=source_path, page_num=page_num,
            )
            return markdown, None
    except Exception as e:
        msg = f"OCR 识别失败（第{page_num+1}页）: {e}"
        logger.warning(msg)
        return None, msg


def ocr_pdf_pages(
    page_images: list[bytes],
    source_path: str = "",
) -> Tuple[List[Document], list[str]]:
    """批量 OCR 多页 PDF

    Args:
        page_images: 每页的图片字节列表
        source_path: 源文件路径（写入 metadata）

    Returns:
        (documents, errors): documents 为成功的 Document 列表，errors 为失败记录
    """
    docs = []
    errors = []

    for i, img_bytes in enumerate(page_images):
        md_text, err = ocr_page(img_bytes, page_num=i, source_path=source_path)
        if md_text:
            docs.append(Document(
                page_content=md_text,
                metadata={
                    "source": source_path,
                    "page": i + 1,
                    "page_label": f"第{i+1}页",
                    "ocr": True,
                }
            ))
            print(f"   ✅ OCR: 第{i+1}页 完成 ({len(md_text)} 字符)", file=sys.stderr, flush=True)
        else:
            errors.append(f"第{i+1}页: {err}")
            print(f"   ⚠️ OCR: 第{i+1}页 失败 - {err}", file=sys.stderr, flush=True)

    return docs, errors


# ═══════════════════════════════════════════════════════════════
# 引擎初始化
# ═══════════════════════════════════════════════════════════════

def _init_engine():
    """初始化 PaddleOCR 引擎（全局单例）

    PaddleOCR >= 3.7 使用 PPStructureV3，旧版使用 PPStructure。
    """
    # PP-Structure V2：版面分析 + 表格识别 + OCR
    # 注意：formula=True 在 Windows PaddlePaddle 2.6.2 上有 Tensor 维度 Bug，暂不启用
    from paddleocr import PPStructure
    try:
        return PPStructure(
            lang='ch',
            table=True,
            layout=True,
            ocr=True,
            formula=False,  # Windows 2.6.2 兼容问题，PP-Structure V3 修复
            show_log=False,
        )
    except Exception as e:
        logger.warning(f"PP-Structure 初始化失败，降级到基础 PaddleOCR: {e}")
        from paddleocr import PaddleOCR
        return PaddleOCR(lang='ch', use_gpu=False, show_log=False)


# ═══════════════════════════════════════════════════════════════
# 结果 → Markdown
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 公式识别：SimpleTex API + 文本规则补召
# ═══════════════════════════════════════════════════════════════

# 公式缓存（内存 + JSON 文件双层）
_formula_cache: dict = {}
_cache_loaded = False
# 百度 access_token 缓存
_baidu_token = ""
_baidu_token_expiry = 0.0


def _load_formula_cache():
    """从 JSON 文件加载公式缓存"""
    global _formula_cache, _cache_loaded
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        from config import FORMULA_CACHE_PATH
        import json, os
        if os.path.exists(FORMULA_CACHE_PATH):
            with open(FORMULA_CACHE_PATH, 'r', encoding='utf-8') as f:
                _formula_cache = json.load(f)
    except Exception:
        pass


def _save_formula_cache():
    """保存公式缓存到 JSON 文件"""
    try:
        from config import FORMULA_CACHE_PATH
        import json
        with open(FORMULA_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(_formula_cache, f, ensure_ascii=False)
    except Exception:
        pass


def _get_cache_key(source_path: str, page_num: int, bbox: tuple) -> str:
    """生成公式缓存 Key：MD5 + 页码 + 坐标"""
    import hashlib
    raw = f"{source_path}|{page_num}|{bbox[0]:.0f},{bbox[1]:.0f},{bbox[2]:.0f},{bbox[3]:.0f}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _is_formula_text(text: str) -> bool:
    """文本规则补召：判断 OCR 文本是否疑似公式"""
    from config import FORMULA_SYMBOL_RATIO, MATH_SYMBOLS

    if not text or len(text) < 3:
        return False

    # 统计数学符号占比
    math_count = sum(1 for c in text if c in MATH_SYMBOLS)
    # 高数特征符号（强信号）
    high_math = {'∑', '∫', '∂', '∏', '∞', '√'}
    has_high_math = any(c in text for c in high_math)

    ratio = math_count / max(len(text), 1)
    return ratio > FORMULA_SYMBOL_RATIO or has_high_math


def _validate_latex(latex: str) -> bool:
    """基础 LaTeX 语法校验"""
    if not latex or len(latex) < 2:
        return False
    # $ 符号必须成对
    if latex.count('$') % 2 != 0:
        return False
    # 括号必须匹配
    brackets = {'{': '}', '[': ']', '(': ')'}
    stack = []
    for c in latex:
        if c in brackets:
            stack.append(brackets[c])
        elif c in brackets.values():
            if not stack or stack.pop() != c:
                return False
    return len(stack) == 0


def _call_baidu_formula_api(image_bytes: bytes) -> Optional[str]:
    """百度智能云・数学公式识别（免费额度）"""
    from config import BAIDU_OCR_API_KEY, BAIDU_OCR_SECRET_KEY
    from config import BAIDU_OCR_FORMULA_URL, BAIDU_OCR_TOKEN_URL
    from config import FORMULA_TIMEOUT

    if not BAIDU_OCR_API_KEY or not BAIDU_OCR_SECRET_KEY:
        return None

    import base64, requests

    # 1. 获取 access_token（缓存 30 天，每次请求前检查过期）
    global _baidu_token, _baidu_token_expiry
    import time
    now = time.time()
    if not _baidu_token or now >= _baidu_token_expiry:
        try:
            resp = requests.post(
                BAIDU_OCR_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": BAIDU_OCR_API_KEY,
                    "client_secret": BAIDU_OCR_SECRET_KEY,
                },
                timeout=10,
            )
            data = resp.json()
            _baidu_token = data.get('access_token', '')
            _baidu_token_expiry = now + data.get('expires_in', 2592000) - 300  # 提前5分钟刷新
        except Exception:
            return None

    if not _baidu_token:
        return None

    # 2. 调用公式识别
    img_b64 = base64.b64encode(image_bytes).decode('ascii')
    try:
        resp = requests.post(
            f"{BAIDU_OCR_FORMULA_URL}?access_token={_baidu_token}",
            data={"image": img_b64},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=FORMULA_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            # 百度返回格式: {"words_result": [{"latex": "..."}], ...}
            words = data.get('words_result', [])
            if words:
                latex = words[0].get('latex', '') or words[0].get('words', '')
                if latex:
                    return latex.strip()
        elif resp.status_code == 17 or 'quota' in str(resp.text).lower():
            print(f"   ⚠️ 百度 OCR 免费额度用尽，切换备选 API", file=sys.stderr)
            return None
    except Exception:
        pass

    return None


def _call_simpletex_api(image_bytes: bytes) -> Optional[str]:
    """SimpleTex API（备选）"""
    from config import SIMPLETEX_API_URL, SIMPLETEX_API_KEY
    from config import FORMULA_TIMEOUT

    if not SIMPLETEX_API_KEY:
        return None

    import base64, requests
    img_b64 = base64.b64encode(image_bytes).decode('ascii')
    try:
        resp = requests.post(
            SIMPLETEX_API_URL,
            json={"image": img_b64},
            headers={
                "Authorization": f"Bearer {SIMPLETEX_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=FORMULA_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            latex = data.get('data', {}).get('latex', '')
            if latex:
                return latex.strip()
    except Exception:
        pass

    return None


def _call_formula_api(image_bytes: bytes) -> Optional[str]:
    """多 API 容灾链：百度 → SimpleTex（带重试）"""
    from config import FORMULA_MAX_RETRIES

    for attempt in range(FORMULA_MAX_RETRIES):
        # 优先百度（免费额度）
        latex = _call_baidu_formula_api(image_bytes)
        if latex:
            return latex

        # 备选 SimpleTex
        latex = _call_simpletex_api(image_bytes)
        if latex:
            return latex

        if attempt < FORMULA_MAX_RETRIES - 1:
            continue

    return None


def _crop_formula_region(img_array, bbox: list, padding: int = 15):
    """从页面图片中裁剪公式区域（外扩 padding 像素）"""
    from config import FORMULA_CROP_PADDING
    pad = padding or FORMULA_CROP_PADDING

    x1, y1, x2, y2 = [int(v) for v in bbox]
    h, w = img_array.shape[:2]

    # 外扩并限制在图片边界内
    x1 = max(0, x1 - pad)
    y1 = max(0, y1 - pad)
    x2 = min(w, x2 + pad)
    y2 = min(h, y2 + pad)

    crop = img_array[y1:y2, x1:x2]
    return crop, (x1, y1, x2, y2)


def _crop_to_bytes(img_array, bbox: list, padding: int = 15) -> Optional[bytes]:
    """裁剪公式区域并返回 PNG 字节"""
    from PIL import Image
    import io

    crop, _ = _crop_formula_region(img_array, bbox, padding)
    if crop.size == 0:
        return None

    pil_img = Image.fromarray(crop)
    buf = io.BytesIO()
    pil_img.save(buf, format='PNG')
    return buf.getvalue()


def _recognize_formula(
    img_array,
    bbox: list,
    source_path: str = "",
    page_num: int = 0,
    line_height: float = 14,
    ocr_text: str = "",
) -> str:
    """公式识别主入口：SimpleTex API + 三级降级

    Args:
        img_array: 完整页面图片 (numpy array)
        bbox: 公式区域坐标 [x1, y1, x2, y2]
        source_path: 源文件路径（用于缓存 Key）
        page_num: 页码
        line_height: 正文行高（用于判断行内/行间）
        ocr_text: 该区域的 OCR 文本（降级时使用）

    Returns:
        公式 Markdown 字符串
    """
    _load_formula_cache()

    # 缓存查找
    cache_key = _get_cache_key(source_path, page_num, tuple(bbox))
    if cache_key in _formula_cache:
        return _formula_cache[cache_key]

    # 裁剪图片
    img_bytes = _crop_to_bytes(img_array, bbox)
    if not img_bytes:
        return ocr_text or "> 📐 [公式]"

    # 行内/行间判断
    from config import FORMULA_INLINE_HEIGHT_RATIO
    h = bbox[3] - bbox[1]
    is_inline = h <= line_height * FORMULA_INLINE_HEIGHT_RATIO

    # 1. 多 API 容灾链（百度 → SimpleTex）
    latex = _call_formula_api(img_bytes)
    if latex and _validate_latex(latex):
        wrapper = "$" if is_inline else "$$"
        result = f"{wrapper} {latex} {wrapper}"
        _formula_cache[cache_key] = result
        _save_formula_cache()
        return result

    # 2. OCR 纯文本降级（不缓存——API Key 配置后下次应该重试）
    if ocr_text and ocr_text.strip():
        clean = _clean_text(ocr_text)
        if clean:
            return clean

    # 3. 占位符
    return "> 📐 [公式]"


def _estimate_line_height(result: list) -> float:
    """从 PP-Structure 结果中估算正文行高"""
    heights = []
    for item in result:
        if item.get('type') == 'text':
            bbox = item.get('bbox')
            if bbox and len(bbox) == 4:
                h = bbox[3] - bbox[1]
                if 10 < h < 50:  # 合理行高范围
                    heights.append(h)
    return sum(heights) / len(heights) if heights else 14.0


def _structure_result_to_markdown(
    result: list,
    img_array=None,
    source_path: str = "",
    page_num: int = 0,
) -> str:
    """将 PP-Structure 版面分析结果转为结构化 Markdown

    元素类型：
      - text:  普通文本段落
      - title: 标题
      - table: HTML 表格 → 转 Markdown
      - table_caption: 表格标题
      - formula: LaTeX 公式
      - figure: 图片区域
    """
    if not result:
        return ""

    # 按阅读顺序排序：先 y 坐标（上→下），同 y 再 x 坐标（左→右）
    result = sorted(result, key=_get_reading_order_key)

    # 估算正文行高（用于行内/行间公式判断）
    line_height = _estimate_line_height(result)

    lines = []
    for item in result:
        item_type = item.get('type', 'text')
        res = item.get('res', {})
        bbox = item.get('bbox', None)

        if item_type == 'title':
            text = _extract_text_from_res(res)
            if text:
                lines.append(f"## {text}")
                lines.append("")

        elif item_type == 'table':
            html = res.get('html', '') if isinstance(res, dict) else ''
            if html:
                md_table = _html_table_to_md(html)
                if md_table:
                    lines.append("")
                    lines.append(md_table)
                    lines.append("")

        elif item_type == 'table_caption':
            text = _extract_text_from_res(res)
            if text:
                lines.append(f"**{text}**")
                lines.append("")

        elif item_type == 'formula' and img_array is not None and bbox:
            # PP-Structure 检测到的公式区域 → SimpleTex API
            ocr_text = _extract_text_from_res(res)
            formula_md = _recognize_formula(
                img_array, bbox, source_path, page_num,
                line_height=line_height, ocr_text=ocr_text,
            )
            if formula_md.startswith("$$"):
                lines.append("")
                lines.append(formula_md)
                lines.append("")
            else:
                lines.append(formula_md)

        elif item_type == 'figure':
            alt_text = _extract_text_from_res(res)
            if alt_text:
                lines.append(f"> 📷 {alt_text}")
            else:
                lines.append(f"> 📷 [图片]")
            lines.append("")

        else:
            # text / 其他：普通段落
            text = _extract_text_from_res(res)
            if not text:
                continue

            # 规则补召：检测文本是否疑似公式
            if img_array is not None and bbox and _is_formula_text(text):
                formula_md = _recognize_formula(
                    img_array, bbox, source_path, page_num,
                    line_height=line_height, ocr_text=text,
                )
                if formula_md.startswith("$"):
                    # 被识别为公式，替换原文
                    lines.append("")
                    lines.append(formula_md)
                    lines.append("")
                    continue

            # 普通文本：直接输出
            # 检测是否需要边界保护（公式前后空行）
            lines.append(text)
            lines.append("")

    markdown = "\n".join(lines)
    import re
    markdown = re.sub(r'\n{3,}', '\n\n', markdown)
    return markdown


def _get_reading_order_key(item: dict) -> tuple:
    """提取元素坐标作为阅读排序键：(y_center, x_center)

    PP-Structure 各元素类型都有坐标信息，按 y 从上到下、x 从左到右排序。
    """
    bbox = item.get('bbox', None)
    if bbox and len(bbox) == 4:
        x1, y1, x2, y2 = bbox
        # 按 y 中心排序，同 y 按 x 排序，容差 20px
        return (round(y1 / 20) * 20, x1)

    # 回退：从 res 中提取坐标
    res = item.get('res', {})
    if isinstance(res, dict):
        region = res.get('text_region', None)
        if region and len(region) == 4:
            return (round(region[0][1] / 20) * 20, region[0][0])  # 第一个点的 y, x

    return (9999, 0)  # 无坐标的放最后


def _extract_text_from_res(res) -> str:
    """从 PP-Structure 的 res 字段提取文本"""
    if isinstance(res, dict):
        text = res.get('text', '')
        if isinstance(text, list):
            return "\n".join(t.get('text', '') if isinstance(t, dict) else str(t) for t in text)
        return str(text)
    elif isinstance(res, list):
        return "\n".join(
            t.get('text', '') if isinstance(t, dict) else str(t) for t in res
        )
    return str(res) if res else ""


def _html_table_to_md(html: str) -> str:
    """HTML table → Markdown

    处理 PP-Structure V2 的特殊格式：所有行合并为一个 <tr>，
    每个 <td> 内含空格分隔的多行数据（列优先排列）。需拆分并转置。
    """
    import re

    rows = re.findall(r'<tr>(.*?)</tr>', html, re.DOTALL)
    if not rows:
        return ""

    # 第一行：提取所有列
    all_cols = []
    for row_html in rows:
        cells = re.findall(r'<t[dh]>(.*?)</t[dh]>', row_html, re.DOTALL)
        cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        if cells:
            all_cols.append(cells)

    if not all_cols:
        return ""

    # 判断是否为 PP-Structure V2 格式：只有 1 个有数据的 <tr>，其余为空
    non_empty_rows = [cells for cells in all_cols if any(c.strip() for c in cells)]
    if len(all_cols) > 1 and len(non_empty_rows) == 1:
        # PP-Structure V2 压缩格式：每列的值用空格连在一起，需拆分转置
        flat_cells = non_empty_rows[0]
        col_values = [c.split() for c in flat_cells]
        max_rows = max(len(v) for v in col_values) if col_values else 1
        lines = []
        for r in range(max_rows):
            row = [cv[r] if r < len(cv) else "" for cv in col_values]
            lines.append("| " + " | ".join(row) + " |")
            if r == 0:
                lines.append("| " + " | ".join(["---"] * len(col_values)) + " |")
        return "\n".join(lines)

    # 标准格式：每个 <tr> 是一行
    lines = []
    for i, cells in enumerate(all_cols):
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("| " + " | ".join(["---"] * len(cells)) + " |")
    return "\n".join(lines)


def _clean_text(text: str) -> str:
    """清洗 OCR 文本：去多余空格、统一换行"""
    if not text:
        return ""
    # 合并连续空格
    import re
    text = re.sub(r' {2,}', ' ', text)
    # 统一换行
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 去掉首尾空白
    return text.strip()
