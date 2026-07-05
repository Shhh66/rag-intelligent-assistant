#!/usr/bin/env python3
"""ChromaDB 知识库日常管理工具 —— 命令行操作向量库的增删改查"""

import sys
import os
import argparse

# 确保脚本从 rag_assistant 目录运行也能正常导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_store import (
    add_document, remove_document, update_document,
    list_documents, is_duplicate, get_status,
    repair, migrate, rollback, list_snapshots,
)


def cmd_status():
    """查看知识库状态"""
    s = get_status()
    print("📊 知识库状态")
    print(f"   就绪:       {'✅ 是' if s['ready'] else '❌ 否'}")
    print(f"   文档数:     {s['document_count']}")
    print(f"   Chunk 总数: {s['total_chunks']}")
    print(f"   嵌入模型:   {s['embedding_model']} ({s['embedding_dim']}维)")
    print(f"   存储路径:   {s['db_path']}")
    print(f"   元数据文件: {s['meta_path']}")


def cmd_list():
    """列出所有文档"""
    docs = list_documents()
    if not docs:
        print("📭 知识库为空")
        return

    print(f"📋 文档清单（共 {len(docs)} 个）\n")
    print(f"{'文档':<50} {'Chunks':>6}  {'入库时间':<20}  {'哈希':>12}")
    print("-" * 100)
    for d in docs:
        hash_short = d['file_hash'][:12] if d['file_hash'] else "(无)"
        print(f"{d['file_path']:<50} {d['chunks']:>6}  {d['added_at']:<20}  {hash_short:>12}")


def cmd_add(args):
    """添加文档"""
    file_path = args.file
    print(f"📄 正在添加: {file_path}")
    result = add_document(file_path, skip_duplicate=not args.force)
    if result.get("skipped"):
        print(f"⏭ 跳过（已存在）: {result['file_path']}")
    elif result.get("error"):
        print(f"❌ 失败: {result['error']}")
    else:
        print(f"✅ 已添加: {result['file_path']} → {result['chunks_added']} chunks")


def cmd_remove(args):
    """删除文档"""
    file_path = args.file
    print(f"🗑 正在删除: {file_path}")
    result = remove_document(file_path)
    if result.get("error"):
        print(f"❌ 失败: {result['error']}")
    else:
        print(f"✅ 已删除: {result['file_path']} → {result['chunks_removed']} chunks")


def cmd_update(args):
    """更新文档"""
    file_path = args.file
    print(f"🔄 正在更新: {file_path}")
    result = update_document(file_path)
    if result.get("error"):
        print(f"❌ 失败: {result['error']}")
    else:
        print(f"✅ 已更新: {result['file_path']}")
        print(f"   删除旧 chunk: {result['chunks_removed']}")
        print(f"   添加新 chunk: {result['chunks_added']}")


def cmd_add_dir(args):
    """批量添加目录"""
    import glob

    dir_path = args.dir
    recursive = args.recursive
    stop_on_error = not args.continue_on_error
    extensions = {".pdf", ".docx", ".txt", ".md"}

    # 收集文件
    if recursive:
        pattern = os.path.join(dir_path, "**", "*")
        files = [f for f in glob.glob(pattern, recursive=True)
                 if os.path.isfile(f) and os.path.splitext(f)[1].lower() in extensions]
    else:
        files = [os.path.join(dir_path, f) for f in os.listdir(dir_path)
                 if os.path.isfile(os.path.join(dir_path, f))
                 and os.path.splitext(f)[1].lower() in extensions]

    if not files:
        print(f"📭 目录中无支持的文档: {dir_path}")
        return

    print(f"📁 批量添加 {len(files)} 个文件\n")

    success = 0
    skipped = 0
    failed = []
    added_chunks = 0

    for f in sorted(files):
        rel = os.path.relpath(f).replace("\\", "/")
        if is_duplicate(f) and not args.force:
            print(f"   ⏭ 跳过: {rel}")
            skipped += 1
            continue

        result = add_document(f, skip_duplicate=True)
        if result.get("skipped"):
            print(f"   ⏭ 跳过: {rel}")
            skipped += 1
        elif result.get("error"):
            print(f"   ❌ 失败: {rel} — {result['error']}")
            failed.append((rel, result["error"]))
            if stop_on_error:
                print(f"\n⚠️ 遇到错误（--stop-on-error），已停止。成功 {success}，失败 {len(failed)}")
                if success > 0:
                    print("   提示: 已成功添加的文件已写入向量库。")
                break
        else:
            print(f"   ✅ {rel} → {result['chunks_added']} chunks")
            success += 1
            added_chunks += result['chunks_added']

    print(f"\n📊 批量添加完成: {success} 成功, {skipped} 跳过, {len(failed)} 失败, 共 {added_chunks} chunks")
    if failed:
        print("失败详情:")
        for name, err in failed:
            print(f"   - {name}: {err}")


def cmd_clear(args):
    """清空知识库"""
    import chromadb
    from config import VECTOR_DB_PATH
    from vector_store import _save_meta, _get_meta_path

    if not args.yes:
        confirm = input("⚠️ 确定要清空整个知识库吗？此操作不可逆！(yes/no): ")
        if confirm.lower() != "yes":
            print("已取消")
            return

    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
    try:
        client.delete_collection("langchain")
        print("🗑 已清空向量库")
    except Exception:
        print("   (向量库不存在，跳过)")

    # 清空 meta
    _save_meta({
        "embedding_model": "",
        "embedding_dim": 0,
        "documents": {},
        "total_chunks": 0,
    })

    # 清空快照
    import shutil
    from config import KB_SNAPSHOT_DIR
    snapshot_dir = KB_SNAPSHOT_DIR
    if os.path.exists(snapshot_dir):
        shutil.rmtree(snapshot_dir)

    print("✅ 知识库已清空")


def cmd_migrate(args):
    """迁移老数据"""
    print("🔧 正在迁移老数据...")
    result = migrate()
    print(f"✅ 迁移完成: {result.get('migrated_chunks', 0)} chunks / {result.get('documents_found', 0)} 文档")


def cmd_repair(args):
    """双源一致性修复"""
    print("🔧 正在校验 Chroma ⇔ db_meta.json 一致性...")
    result = repair()
    print(f"✅ 修复完成:")
    print(f"   从 Chroma 补全 meta: {result.get('added_to_meta', 0)}")
    print(f"   从 meta 移除不存在:  {result.get('removed_from_meta', 0)}")
    print(f"   修正 chunk 数量:    {result.get('chunks_fixed', 0)}")


def cmd_update_permission(args):
    """更新文档权限"""
    from vector_store import update_doc_permission

    result = update_doc_permission(
        args.file,
        kb_group=args.kb_group,
        visibility=args.visibility,
    )
    if result.get("error"):
        print(f"❌ {result['error']}")
    else:
        print(f"✅ 权限已更新: {result['file_path']} → {result['updated_chunks']} chunks")
        if args.kb_group:
            print(f"   kb_group: {args.kb_group}")
        if args.visibility:
            print(f"   visibility: {args.visibility}")


def cmd_rollback(args):
    """快照回退"""
    if args.list:
        snapshots = list_snapshots()
        if not snapshots:
            print("📭 没有可用快照")
            return
        print("📸 可用快照:\n")
        print(f"{'时间戳':<18} {'操作':<30} {'Chunk数':>8}")
        print("-" * 60)
        for s in snapshots:
            print(f"{s['timestamp']:<18} {s['operation']:<30} {s['chunk_count']:>8}")
        return

    ts = args.timestamp
    print(f"⏪ 正在回退到快照: {ts or '最近一次'}...")
    result = rollback(ts)
    print(f"   {result.get('note', '')}")
    if result.get('restored_documents', 0) > 0:
        print(f"   已恢复 {result['restored_documents']} 个文档的元数据记录")
        print(f"   💡 建议运行 python kb_manager.py repair 校验向量库一致性")


def main():
    parser = argparse.ArgumentParser(
        description="ChromaDB 知识库管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python kb_manager.py status
  python kb_manager.py list
  python kb_manager.py add uploaded_docs/新文档.pdf
  python kb_manager.py remove "uploaded_docs/旧文档.pdf"
  python kb_manager.py update uploaded_docs/修改后文档.pdf
  python kb_manager.py add-dir uploaded_docs/ --recursive
  python kb_manager.py clear
  python kb_manager.py migrate
  python kb_manager.py repair
  python kb_manager.py update-permission "uploaded_docs/文档.pdf" --kb-group dept_rd --visibility internal
  python kb_manager.py rollback --list
  python kb_manager.py rollback 20260704_153000
        """,
    )
    sub = parser.add_subparsers(dest="command", help="操作命令")

    # status
    sub.add_parser("status", help="查看知识库状态")

    # list
    sub.add_parser("list", help="列出所有文档")

    # add
    p_add = sub.add_parser("add", help="增量添加文档")
    p_add.add_argument("file", help="文档路径")
    p_add.add_argument("--force", action="store_true", help="强制添加（跳过去重检测）")

    # remove
    p_remove = sub.add_parser("remove", help="删除文档")
    p_remove.add_argument("file", help="文档路径（相对或绝对路径）")

    # update
    p_update = sub.add_parser("update", help="更新文档（先写新后删旧）")
    p_update.add_argument("file", help="文档路径")

    # add-dir
    p_adddir = sub.add_parser("add-dir", help="批量添加目录")
    p_adddir.add_argument("dir", help="目录路径")
    p_adddir.add_argument("--recursive", action="store_true", help="递归遍历子目录")
    p_adddir.add_argument("--force", action="store_true", help="强制添加（跳过去重检测）")
    p_adddir.add_argument("--continue-on-error", action="store_true",
                          help="跳过失败文件继续处理（默认 --stop-on-error）")

    # clear
    p_clear = sub.add_parser("clear", help="清空知识库")
    p_clear.add_argument("--yes", action="store_true", help="跳过确认")

    # migrate
    sub.add_parser("migrate", help="迁移老数据补全元数据")

    # repair
    sub.add_parser("repair", help="双源一致性修复（Chroma ⇔ db_meta.json）")

    # update-permission
    p_perm = sub.add_parser("update-permission", help="更新文档权限（分组+可见性）")
    p_perm.add_argument("file", help="文档路径")
    p_perm.add_argument("--kb-group", help="知识库分组（如 dept_rd）")
    p_perm.add_argument("--visibility",
                        choices=["public", "internal", "confidential"],
                        help="可见性等级")

    # rollback
    p_rollback = sub.add_parser("rollback", help="快照回退")
    p_rollback.add_argument("timestamp", nargs="?", help="回退到指定快照（默认最近一次）")
    p_rollback.add_argument("--list", action="store_true", help="列出所有可用快照")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    # 执行命令
    commands = {
        "status": lambda: cmd_status(),
        "list": lambda: cmd_list(),
        "add": lambda: cmd_add(args),
        "remove": lambda: cmd_remove(args),
        "update": lambda: cmd_update(args),
        "add-dir": lambda: cmd_add_dir(args),
        "clear": lambda: cmd_clear(args),
        "migrate": lambda: cmd_migrate(args),
        "repair": lambda: cmd_repair(args),
        "update-permission": lambda: cmd_update_permission(args),
        "rollback": lambda: cmd_rollback(args),
    }
    commands[args.command]()


if __name__ == "__main__":
    main()
