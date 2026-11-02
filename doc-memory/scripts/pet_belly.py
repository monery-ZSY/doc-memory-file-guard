#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档拾忆 · 宠物肚子（安全删除暂存区）
把文件"喂给"界面宠物 → 移入 belly 暂存目录（期间可随时吐出恢复原位），
24 小时后由清扫逻辑转入系统回收站（仍可还原，不直接删除）。
所有操作与索引联动（吃掉即从索引移除，吐出即回索引）。
"""

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from indexer import DB_PATH, DATA_DIR, extract_content, trunc_limit, init_db  # noqa: E402

BELLY_DIR = DATA_DIR / "belly"
REGISTRY = BELLY_DIR / "registry.json"
MAX_AGE = 86400        # 24 小时后转入系统回收站（可还原）
BELLY_LIMIT = 99       # 肚子最多暂存 99 个文件

_lock = threading.Lock()


def _load():
    if not REGISTRY.exists():
        return []
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8")).get("items", [])
    except Exception:
        return []


def _save(items):
    BELLY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def _remove_from_index(path):
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("DELETE FROM files WHERE path=?", (path,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _restore_to_index(path):
    """吐出后把文件重新写回索引（含正文提取）。"""
    fp = Path(path)
    if not fp.exists():
        return
    try:
        st = fp.stat()
    except OSError:
        return
    ext = fp.suffix.lower()
    try:
        content, status = extract_content(str(fp), ext, 20)
    except Exception:
        content, status = None, "name-only"
    if content:
        content = content[:trunc_limit(ext)]
    else:
        status = "name-only"
    today = date.today().isoformat()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        init_db(conn)
        conn.execute("""
            INSERT INTO files (path,name,ext,dir,size,mtime,first_seen,last_seen,content,status)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
                name=excluded.name, ext=excluded.ext, dir=excluded.dir,
                size=excluded.size, mtime=excluded.mtime, last_seen=excluded.last_seen,
                content=excluded.content, status=excluded.status
        """, (str(fp), fp.name, ext, str(fp.parent), st.st_size, st.st_mtime,
              today, today, content, status))
        conn.commit()
        conn.close()
    except Exception:
        pass


def eat(path):
    """吞下一个文件：移入肚子暂存目录并从索引移除。"""
    with _lock:
        fp = Path(path)
        if not fp.exists():
            return {"error": "文件不存在或已被移动"}
        if fp.is_dir():
            return {"error": "暂时只吃得下文件，文件夹先绕过啦"}
        if BELLY_DIR == fp or BELLY_DIR in fp.parents:
            return {"error": "这个文件已经在肚子里啦"}
        items = _load()
        if any(it["orig"] == str(fp) for it in items):
            return {"error": "这个文件已经在肚子里啦"}
        if len(items) >= BELLY_LIMIT:
            return {"error": "肚子快撑爆啦！最多装 " + str(BELLY_LIMIT) +
                            " 个文件，双击肚子吐出一些再喂我吧～"}
        BELLY_DIR.mkdir(parents=True, exist_ok=True)
        dest = BELLY_DIR / (uuid.uuid4().hex + fp.suffix)
        try:
            shutil.move(str(fp), str(dest))
        except Exception as exc:
            return {"error": "文件被占用，吃不动（" + str(exc) + "）"}
        items.append({
            "id": uuid.uuid4().hex[:8],
            "orig": str(fp),
            "belly": str(dest),
            "name": fp.name,
            "eaten_at": time.time(),
        })
        _save(items)
        count = len(items)
    _remove_from_index(str(fp))
    return {"ok": True, "name": fp.name, "count": count}


def eat_match(name, size):
    """从系统拖来的文件（浏览器拿不到完整路径）按 文件名+大小 在索引中唯一定位后吞下。"""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        rows = conn.execute(
            "SELECT path FROM files WHERE name=? AND size=?", (name, int(size))).fetchall()
        conn.close()
    except Exception:
        return {"error": "索引不可用"}
    if not rows:
        return {"error": "索引里没有找到同名同大小的文件，请先在界面搜索到它再拖拽"}
    if len(rows) > 1:
        return {"error": "同名文件有 " + str(len(rows)) + " 个，请在界面搜索后拖拽具体那张卡片"}
    return eat(rows[0][0])


def list_items():
    with _lock:
        items = _load()
    now = time.time()
    out = []
    for it in items:
        left = int(max(0, MAX_AGE - (now - it["eaten_at"])))
        h, m, s = left // 3600, left % 3600 // 60, left % 60
        out.append({"name": it["name"], "orig": it["orig"],
                    "left": "{:02d}:{:02d}:{:02d}".format(h, m, s)})
    return {"items": out, "count": len(out)}


def spit_all():
    """把肚子里的文件全部吐回原位（原位被占则存为「 (恢复-xxxx)」副本）。"""
    with _lock:
        items = _load()
    restored, failed, remain = [], [], []
    for it in items:
        src = Path(it["belly"])
        orig = Path(it["orig"])
        if not src.exists():
            failed.append({"name": it["name"], "error": "暂存文件已不存在"})
            continue
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            target = orig
            if target.exists():
                target = orig.with_name(orig.stem + " (恢复-" + it["id"] + ")" + orig.suffix)
            shutil.move(str(src), str(target))
            restored.append({"name": it["name"], "path": str(target)})
        except Exception as exc:
            failed.append({"name": it["name"], "error": str(exc)})
            remain.append(it)
            continue
    with _lock:
        _save(remain)
    for r in restored:
        _restore_to_index(r["path"])
    return {"restored": restored, "failed": failed, "left": len(remain)}


def _to_recycle_bin(path):
    """把文件送入 Windows 系统回收站（ALLOWUNDO，可还原）。成功返回 True。"""
    import ctypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = 3                    # FO_DELETE
    op.pFrom = str(path) + "\x00"   # 双零结尾
    op.pTo = None
    op.fFlags = 0x40 | 0x10 | 0x4   # ALLOWUNDO | NOCONFIRMATION | SILENT
    try:
        return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0
    except OSError:
        return False


def purge_expired():
    """超过 24 小时的暂存文件转入系统回收站（可还原；被占用的留到下轮）。"""
    with _lock:
        items = _load()
    now = time.time()
    keep, recycled = [], 0
    for it in items:
        if now - it["eaten_at"] > MAX_AGE:
            if _to_recycle_bin(it["belly"]):
                recycled += 1
            else:
                keep.append(it)  # 占用中，下轮再处理
        else:
            keep.append(it)
    with _lock:
        _save(keep)
    return recycled


if __name__ == "__main__":
    import pprint
    pprint.pprint(list_items())
