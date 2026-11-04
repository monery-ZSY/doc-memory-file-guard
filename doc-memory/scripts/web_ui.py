#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档拾忆 · 独立检索界面（Web UI）
本地启动轻量 HTTP 服务（仅绑定 127.0.0.1），自动打开浏览器：
- 按内容关键词检索本机文件，默认按修改时间排序，可切换相关度
- 结果卡片可一键「打开文件」「打开所在文件夹」（资源管理器定位选中）
- 检索历史：每次搜索自动记录（去重、最近 50 条），「检索历史」标签页一键重搜，
  防止意外关闭后丢失搜索上下文
- 首次使用引导：检测到未建索引时弹出授权确认，经用户同意才开始全盘扫描识别
 - 宠物文件管理：拖拽结果卡片喂宠物 = 移入肚子暂存（上限 99 个，到期转入系统回收站可还原），
  双击宠物肚子把文件吐回原位（抛物线恢复动画）
- 界面宠物小助手：进页面打招呼、搜索时思考、找到开心、没找到安慰
- 每日快照查看、一键增量更新索引
纯 Python 标准库实现，双击 run_web_ui.bat 或 `python web_ui.py` 即用。

用法:
  python web_ui.py [--port 8765] [--no-browser]
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import searcher  # noqa: E402
import pet_belly  # noqa: E402
import pet_shop  # noqa: E402  签到积分/宠物商店/喂食/天气
from indexer import DB_PATH, DATA_DIR, SNAPSHOT_DIR, init_db  # noqa: E402

# ---------- 检索历史 ----------

HISTORY_PATH = DATA_DIR / "history.json"
HISTORY_MAX = 50
WEATHER_CITY_PATH = DATA_DIR / "weather_city.txt"  # 天气默认城市（本机记住，避免 IP 定位漂移）


def _load_weather_city():
    """读取本机记住的默认城市（没填过返回空串）。"""
    try:
        if WEATHER_CITY_PATH.exists():
            return WEATHER_CITY_PATH.read_text(encoding="utf-8").strip()[:30]
    except Exception:
        pass
    return ""


def _hist_key(rec):
    return json.dumps([rec.get("kw"), rec.get("mode"), rec.get("sort"),
                       rec.get("type"), rec.get("new"), rec.get("within"),
                       rec.get("dir"), rec.get("lang"), rec.get("year")],
                      ensure_ascii=False)


def _record_history(kws, qs, total_hits):
    """搜索成功后记录历史：相同查询（关键词+全部过滤条件）只刷新时间与命中数。"""
    try:
        items = []
        if HISTORY_PATH.exists():
            items = json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("items", [])
        rec = {
            "kw": list(kws),
            "mode": qs.get("mode", ["any"])[0],
            "sort": qs.get("sort", ["score"])[0],
            "type": qs.get("type", [None])[0],
            "new": qs.get("new", [None])[0],
            "within": qs.get("within", [None])[0],
            "dir": qs.get("dir", [None])[0],
            "lang": qs.get("lang", [None])[0],
            "year": qs.get("year", [None])[0],
            "limit": int(qs.get("limit", ["50"])[0] or 50),
            "total_hits": total_hits,
            "ts": time.time(),
        }
        items = [it for it in items if _hist_key(it) != _hist_key(rec)]
        items.insert(0, rec)
        items = items[:HISTORY_MAX]
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:
        pass


def api_history():
    if not HISTORY_PATH.exists():
        return {"items": []}
    try:
        return {"items": json.loads(
            HISTORY_PATH.read_text(encoding="utf-8")).get("items", [])}
    except Exception:
        return {"items": []}


def api_history_clear():
    try:
        if HISTORY_PATH.exists():
            HISTORY_PATH.unlink()
    except Exception:
        pass
    return {"ok": True}


# ---------- 扫描子进程管理 ----------

_scan_lock = threading.Lock()
_scan_state = {"running": False, "log_file": None, "proc": None, "started_at": None}


def start_scan():
    with _scan_lock:
        if _scan_state["running"]:
            return False
        log_file = DATA_DIR / "scan-web.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "indexer.py"), "scan", "--report", "--quiet"],
            stdout=open(log_file, "w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT)
        _scan_state.update(running=True, proc=proc, log_file=log_file,
                           started_at=time.time())

        def _watch():
            proc.wait()
            with _scan_lock:
                _scan_state["running"] = False

        threading.Thread(target=_watch, daemon=True).start()
        return True


def scan_status():
    with _scan_lock:
        st = dict(running=_scan_state["running"],
                  started_at=_scan_state["started_at"])
        log_file = _scan_state["log_file"]
    if log_file and Path(log_file).exists():
        try:
            st["log_tail"] = Path(log_file).read_text(encoding="utf-8", errors="replace")[-600:]
        except OSError:
            pass
    return st


def _sweeper():
    """每 10 分钟清扫一次宠物肚子中超过 24 小时的暂存文件。"""
    while True:
        try:
            pet_belly.purge_expired()
        except Exception:
            pass
        time.sleep(600)


# ---------- API ----------

def api_status():
    first_use = not DB_PATH.exists()
    if first_use:
        return {"indexed": False, "first_use": True}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        init_db(conn)
        total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        with_text = conn.execute("SELECT COUNT(*) FROM files WHERE status='text'").fetchone()[0]
        last_scan = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
        today_new = conn.execute("SELECT COUNT(*) FROM files WHERE first_seen=?",
                                 (date.today().isoformat(),)).fetchone()[0]
        conn.close()
        return {
            "indexed": total > 0, "first_use": False, "total": total,
            "with_text": with_text, "today_new": today_new,
            "last_scan": last_scan[0] if last_scan else None,
            "db_mb": round(DB_PATH.stat().st_size / 1048576, 1),
        }
    except sqlite3.Error:
        # 扫描写入高峰等锁超时：返回简化状态而不是 500，界面保持可用
        return {"indexed": True, "first_use": False, "total": "…",
                "busy": True}


def api_search(qs):
    # 兼容两种传参：多个 kw=参数（界面多词）与单 kw 带空格，全部收齐再拆词
    kws = []
    for w in (qs.get("kw") or [""]):
        kws.extend(w.split())
    kws = [k for k in kws if k]
    if not kws:
        return {"error": "请输入关键词"}
    try:
        year = qs.get("year", [None])[0]
        year = int(year) if year else None
    except ValueError:
        return {"error": "year 参数需为年份数字"}
    nl = (qs.get("lang", [""])[0] or "").strip()
    if nl not in ("", "zh", "en"):
        return {"error": "lang 参数仅支持 zh/en"}
    try:
        out = searcher.run_search(
            kws,
            require_all=(qs.get("mode", ["any"])[0] == "all"),
            type_=qs.get("type", [None])[0] or None,
            within=qs.get("within", [None])[0] or None,
            new=qs.get("new", [None])[0] or None,
            dir_sub=qs.get("dir", [None])[0] or None,
            limit=min(int(qs.get("limit", ["50"])[0] or 50), 200),
            sort=qs.get("sort", ["score"])[0],
            year=year,
            name_lang=nl or None,
        )
    except (ValueError, FileNotFoundError) as exc:
        return {"error": str(exc)}
    _attach_duplicates(out)
    _record_history(kws, qs, out.get("total_hits", 0))
    # 无结果：多半是刚保存的文件还没进索引（每天凌晨才增量），自动补一次增量扫描
    if not out.get("total_hits"):
        try:
            if not scan_status().get("running"):
                start_scan()
            out["scan_pending"] = True
        except Exception:
            pass
    return out


def _attach_duplicates(out):
    """为搜索结果附带重复副本信息：同名且同大小的文件视为同一文件的重复保存。"""
    results = out.get("results") or []
    if not results or not DB_PATH.exists():
        return
    names = list({r["name"] for r in results})
    if not names:
        return
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        rows = conn.execute(
            f"SELECT name, size, path, mtime FROM files WHERE name IN ({','.join('?' * len(names))})",
            names).fetchall()
        conn.close()
    except sqlite3.Error:
        return
    by = {}
    for name, size, path, mtime in rows:
        by.setdefault((name, size), []).append({"path": path, "mtime": mtime})
    for r in results:
        grp = by.get((r["name"], r["size"]), [])
        dups = [d for d in grp if d["path"] != r["path"]]
        if dups:
            r["dup_count"] = len(dups)
            r["dups"] = dups


# ---------- 相似文件推荐（文献检索「相似文献」的本地版） ----------

def _name_bigrams(stem):
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", stem.lower())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _bigram_sim(a, b):
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def api_similar(body):
    """找相似文件：同目录邻居 + 全库文件名相似（字符 bigram Dice 系数）。"""
    p = (body.get("path") or "").strip()
    if not p:
        return {"error": "缺少路径"}
    if not DB_PATH.exists():
        return {"error": "索引尚未建立"}
    try:
        row = sqlite3.connect(str(DB_PATH), timeout=3).execute(
            "SELECT name, ext, dir FROM files WHERE path=?", (p,)).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return {"error": "文件不在索引中（可能已被移动或删除）"}
    name, ext, dirpath = row
    stem = Path(name).stem
    base_bi = _name_bigrams(stem)
    conn = sqlite3.connect(str(DB_PATH), timeout=3)
    items = []
    # 1) 同目录邻居（当年一起保存的文件最相关）
    try:
        rows = conn.execute(
            "SELECT path, name, dir, size, mtime FROM files WHERE dir=? AND path<>? "
            "ORDER BY mtime DESC LIMIT 40", (dirpath, p)).fetchall()
    except sqlite3.Error:
        rows = []
    same_dir = {}
    for rp, rn, rd, rsz, rmt in rows:
        same_dir[rp] = True
        items.append({"path": rp, "name": rn, "dir": rd, "size": rsz, "mtime": rmt,
                      "why": "同目录", "sim": round(_bigram_sim(base_bi, _name_bigrams(Path(rn).stem)), 2)})
    # 2) 全库名称相似：取文件名中较长的 token 做 LIKE 预筛，再精确算相似度
    toks = [t for t in re.split(r"[^0-9a-zA-Z\u4e00-\u9fff]+", stem)
            if len(t) >= 2]
    toks.sort(key=len, reverse=True)
    cands = []
    for t in toks[:3]:
        try:
            cands += conn.execute(
                "SELECT path, name, dir, size, mtime FROM files WHERE name LIKE ? LIMIT 300",
                (f"%{t[:8]}%",)).fetchall()
        except sqlite3.Error:
            pass
    conn.close()
    seen = {p}
    scored = []
    for rp, rn, rd, rsz, rmt in cands:
        if rp in seen or rp in same_dir:
            continue
        seen.add(rp)
        sim = _bigram_sim(base_bi, _name_bigrams(Path(rn).stem))
        if sim >= 0.3:
            scored.append({"path": rp, "name": rn, "dir": rd, "size": rsz, "mtime": rmt,
                           "why": "名称相似", "sim": round(sim, 2)})
    scored.sort(key=lambda x: -x["sim"])
    items += scored[:10]
    items.sort(key=lambda x: (0 if x["why"] == "同目录" else 1, -x["sim"], -x["mtime"]))
    return {"ok": True, "name": name, "items": items[:15]}


# ---------- 意见反馈（存本地，凌晨定时任务由智能体读取识别并优化） ----------

FEEDBACK_PATH = DATA_DIR / "feedback.json"
FEEDBACK_TYPES = {"idea": "功能建议", "ui": "界面问题", "ux": "体验不好", "bug": "疑似Bug", "other": "其他"}


def api_feedback(body):
    """提交反馈：写入本地 feedback.json，凌晨自检任务时由智能体读取识别并优化。"""
    typ = (body.get("type") or "other").strip()
    if typ not in FEEDBACK_TYPES:
        typ = "other"
    text = (body.get("text") or "").strip()
    if not text:
        return {"error": "反馈内容不能为空"}
    if len(text) > 1000:
        text = text[:1000]
    contact = (body.get("contact") or "").strip()[:60]
    try:
        items = []
        if FEEDBACK_PATH.exists():
            items = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8")).get("items", [])
        items.append({
            "id": time.strftime("%Y%m%d-%H%M%S"),
            "type": typ, "type_cn": FEEDBACK_TYPES[typ],
            "text": text, "contact": contact,
            "ts": time.time(), "status": "pending",
        })
        FEEDBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
        FEEDBACK_PATH.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        return {"error": "保存失败：" + str(exc)}
    pending = sum(1 for it in items if it.get("status") == "pending")
    return {"ok": True, "total": len(items), "pending": pending}


def api_feedback_list():
    if not FEEDBACK_PATH.exists():
        return {"items": []}
    try:
        return {"items": json.loads(
            FEEDBACK_PATH.read_text(encoding="utf-8")).get("items", [])}
    except Exception:
        return {"items": []}


# ---------- 数据库面板（索引可视化统计） ----------

def api_stats():
    """数据库面板：类型分布/正文率/磁盘来源/录入趋势/重复文件TOP榜。"""
    if not DB_PATH.exists():
        return {"error": "索引尚未建立"}
    conn = sqlite3.connect(str(DB_PATH), timeout=3)
    init_db(conn)

    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    with_text = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='text'").fetchone()[0]
    total_size = conn.execute(
        "SELECT SUM(size) FROM files").fetchone()[0] or 0
    content_chars = conn.execute(
        "SELECT SUM(length(content)) FROM files").fetchone()[0] or 0

    # 类型分布 TOP10（按数量）
    by_type = conn.execute(
        "SELECT ext, COUNT(*) AS n FROM files GROUP BY ext "
        "ORDER BY n DESC LIMIT 10").fetchall()

    # 磁盘/来源分布（按路径首段聚合）
    by_drive = []
    for d, n in conn.execute(
            "SELECT substr(path,1,instr(path||'\\','\\')-1) AS drive, COUNT(*) AS n "
            "FROM files GROUP BY drive ORDER BY n DESC LIMIT 8").fetchall():
        by_drive.append({"drive": d, "count": n})

    # 录入趋势（按 first_seen 天聚合，最近 14 天）
    trend = []
    for d, n in conn.execute(
            "SELECT first_seen, COUNT(*) FROM files WHERE first_seen IS NOT NULL "
            "GROUP BY first_seen ORDER BY first_seen DESC LIMIT 14").fetchall():
        trend.append({"date": d, "count": n})
    trend.reverse()

    # 重复文件 TOP 榜（同名同大小 ≥2 的组，按组数与浪费空间排序）
    dup_groups = conn.execute(
        "SELECT name, size, COUNT(*) AS n FROM files "
        "GROUP BY name, size HAVING n >= 2 ORDER BY n * size DESC LIMIT 10").fetchall()
    dups = [{"name": n_, "size": s, "copies": k - 1,
             "waste": s * (k - 1)} for n_, s, k in dup_groups]

    # 今日/昨日新增
    today = conn.execute(
        "SELECT COUNT(*) FROM files WHERE first_seen=?",
        (date.today().isoformat(),)).fetchone()[0]
    conn.close()

    return {
        "total": total, "with_text": with_text,
        "text_rate": round(with_text * 100.0 / total, 1) if total else 0,
        "total_size": total_size,
        "content_chars": content_chars,
        "db_mb": round(DB_PATH.stat().st_size / 1048576, 1),
        "by_type": [{"ext": e or "(无扩展名)", "count": n} for e, n in by_type],
        "by_drive": by_drive,
        "trend": trend,
        "dups": dups,
        "today_new": today,
    }


# ---------- 回收站（连接系统回收站，可从桌面还原） ----------


def api_recycle(body):
    """把文件移入 Windows 回收站（非直接删除，可从桌面回收站还原）并同步移出索引。"""
    import ctypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    p = (body.get("path") or "").strip()
    if not p:
        return {"error": "缺少路径"}
    fp = Path(p)
    if not fp.exists():
        return {"error": "文件不存在或已被移动"}
    if fp.is_dir():
        return {"error": "回收站暂只接收文件，不支持文件夹"}
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = 3                    # FO_DELETE
    op.pFrom = str(fp) + "\x00"     # 双零结尾
    op.pTo = None
    op.fFlags = 0x40 | 0x10 | 0x4   # ALLOWUNDO | NOCONFIRMATION | SILENT
    try:
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except OSError as exc:
        return {"error": "移入回收站失败：" + str(exc)}
    if res != 0:
        return {"error": "移入回收站失败（代码 {}）".format(res)}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=3)
        conn.execute("DELETE FROM files WHERE path=?", (str(fp),))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
    return {"ok": True, "name": fp.name}


def api_open_recyclebin():
    """打开系统回收站（与桌面回收站同一位置）。"""
    subprocess.Popen(["explorer", "shell:RecycleBinFolder"])
    return {"ok": True}


def api_ext_fileguard_launch():
    """拓展模块 · 文件卫士：确保 8766 服务在运行（在跑直接返回，没跑则后台拉起）。
    界面内嵌 iframe 使用，不依赖用户手动双击桌面图标。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 8766), timeout=1):
            return {"ok": True, "already": True}
    except OSError:
        pass
    # 定位文件卫士入口：C 盘技能目录优先，D 盘项目副本兜底
    cands = [
        SCRIPT_DIR.parent.parent / "file-guard" / "scripts" / "web_ui.py",
        Path(r"D:\文件卫士\scripts\web_ui.py"),
    ]
    wui = next((p for p in cands if p.exists()), None)
    if wui is None:
        return {"error": "未找到文件卫士程序（file-guard 技能未安装）"}
    exe = Path(sys.executable)
    pythonw = exe.parent / "pythonw.exe"
    py = str(pythonw) if pythonw.exists() else str(exe)
    try:
        subprocess.Popen([py, str(wui), "--no-browser"], cwd=str(wui.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return {"error": "启动失败：" + str(exc)}
    return {"ok": True, "already": False}


def _list_desktop_shortcuts(desktop):
    """枚举桌面全部 .lnk 的 名称|目标|参数（一次性 PowerShell，供去重与失效判断）。"""
    script = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        "$ws = New-Object -ComObject WScript.Shell; "
        f"Get-ChildItem -LiteralPath '{desktop}' -Filter *.lnk -ErrorAction SilentlyContinue | "
        "ForEach-Object { $s = $ws.CreateShortcut($_.FullName); "
        "Write-Output ($_.BaseName + '|' + $s.TargetPath + '|' + $s.Arguments) }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=20)
        out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
        # PowerShell 控制台输出可能是 GBK
        if not out.strip() and proc.returncode == 0:
            out = ""
        entries = []
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                entries.append({"name": parts[0].strip(),
                                "target": parts[1].strip(),
                                "args": parts[2].strip()})
        return entries
    except Exception:
        return []


def api_create_shortcut(body):
    """在桌面创建「文档拾忆」快捷方式（pythonw 零窗口直启 + 流星透明图标）。

    v1.2.1 三重防重复/防失效：
    1. 目标级去重：桌面任意 .lnk（无论叫什么名字）只要已指向本 web_ui.py，
       视为已存在，不再创建——用户手动建过「文档拾忆 DocMemory」等别名也不会重复。
    2. 失效修复：同名「文档拾忆.lnk」若指向已不存在的旧路径（用户换目录解压），
       自动重建指向当前位置，避免白色空图标。
    3. 同名且指向另一份仍存在的副本：不劫持，返回 existed。
    """
    import ctypes

    # 取桌面目录（CSIDL_DESKTOP=0）
    try:
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetSpecialFolderPathW(None, buf, 0, False)
        desktop = Path(buf.value)
    except Exception:
        desktop = Path.home() / "Desktop"
    lnk_path = desktop / "文档拾忆.lnk"
    cur_entry = str(SCRIPT_DIR / "web_ui.py").lower()

    entries = _list_desktop_shortcuts(desktop)

    # 1) 目标级去重：任何名字的快捷方式，只要指向本 web_ui.py 就算已存在
    for e in entries:
        combined = (e["target"] + " " + e["args"]).lower()
        if cur_entry in combined:
            return {"ok": True, "existed": True, "path": str(lnk_path),
                    "name": e["name"] or "文档拾忆"}

    # 2) 同名快捷方式存在 → 判断其指向是否失效
    if lnk_path.exists():
        mine = next((e for e in entries if e["name"] == "文档拾忆"), None)
        if mine:
            old_ref = (mine["args"].strip('"') or mine["target"]).strip()
            # 指向另一份仍存在的副本（活路径）→ 不劫持
            if Path(old_ref).exists() and cur_entry not in (mine["target"] + " " + mine["args"]).lower():
                return {"ok": True, "existed": True, "path": str(lnk_path)}
        # 指向已失效（旧目录被删/移动）→ 删除旧快捷方式后重建指向当前位置
        try:
            lnk_path.unlink()
        except OSError:
            pass

    # pythonw 路径：与当前解释器同目录
    exe_dir = Path(sys.executable).parent
    pythonw = exe_dir / "pythonw.exe"
    if not pythonw.exists():
        return {"error": "找不到 pythonw.exe，无法创建零窗口快捷方式"}
    # 图标：优先新「文档+∞」图标，旧环境缺失时回退流星图标
    icon = next((SCRIPT_DIR / n for n in ("doclogo.ico", "meteor2.ico") if (SCRIPT_DIR / n).exists()), None)
    icon_arg = str(icon) if icon else ""
    # 用 PowerShell 创建 .lnk（无需额外依赖）
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$lnk = $ws.CreateShortcut('{lnk_path}'); "
        f"$lnk.TargetPath = '{pythonw}'; "
        f"$lnk.Arguments = '\"{SCRIPT_DIR / 'web_ui.py'}\"'; "
        f"$lnk.WorkingDirectory = '{SCRIPT_DIR}'; "
        f"$lnk.WindowStyle = 7; "
        + (f"$lnk.IconLocation = '{icon_arg},0'; " if icon_arg else "")
        + "$lnk.Save()"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=20)
    except Exception as exc:
        return {"error": "创建失败：" + str(exc)}
    if proc.returncode != 0 or not lnk_path.exists():
        return {"error": "创建失败：" + proc.stderr.decode("utf-8", "replace")[:200]}
    return {"ok": True, "existed": False, "path": str(lnk_path)}


def _has_association(file_path):
    """FindExecutableW 查询关联程序，避免无关联类型弹系统对话框阻塞请求。"""
    import ctypes
    try:
        buf = ctypes.create_unicode_buffer(300)
        ret = ctypes.windll.shell32.FindExecutableW(
            str(file_path), str(Path(file_path).parent), buf)
        return ret > 32
    except Exception:
        return False


def _explorer_select(file_path):
    """ShellExecuteW 打开文件夹并选中文件（比命令行拼引号可靠）。"""
    import ctypes
    try:
        params = '/select,"' + str(file_path) + '"'
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "open", "explorer.exe", params, None, 1)
        return ret > 32
    except Exception:
        return False


def api_open(qs):
    p = (qs.get("path", [""])[0] or "").strip()
    mode = qs.get("mode", ["folder"])[0]
    if not p:
        return {"error": "缺少路径"}
    fp = Path(p)
    if not fp.exists():
        return {"error": "路径不存在或文件已被移动/删除"}
    try:
        if mode == "file":
            if not _has_association(fp):
                _explorer_select(fp)
                return {"ok": True, "action": "fallback_folder",
                        "message": "该文件类型没有关联的打开程序，已为你定位到所在文件夹"}
            try:
                os.startfile(str(fp))
                return {"ok": True, "action": "opened"}
            except OSError:
                _explorer_select(fp)
                return {"ok": True, "action": "fallback_folder",
                        "message": "该文件暂时无法用默认程序打开，已为你定位到所在文件夹"}
        if fp.is_dir():
            os.startfile(str(fp))
            return {"ok": True, "action": "opened"}
        if _explorer_select(fp):
            return {"ok": True, "action": "located"}
        os.startfile(str(fp.parent))
        return {"ok": True, "action": "fallback_folder",
                "message": "已打开文件所在文件夹（未选中文件）"}
    except OSError as exc:
        return {"error": "无法打开: " + str(exc)}


def api_snapshots():
    if not SNAPSHOT_DIR.exists():
        return {"dates": []}
    dates = sorted((p.stem for p in SNAPSHOT_DIR.glob("????-??-??.md")), reverse=True)
    return {"dates": dates}


def api_snapshot(qs):
    d = (qs.get("date", [""])[0] or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return {"error": "非法日期"}
    f = SNAPSHOT_DIR / (d + ".md")
    if not f.exists():
        return {"error": "无 " + d + " 快照"}
    return {"date": d, "content": f.read_text(encoding="utf-8", errors="replace")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif u.path == "/api/status":
            self._send(200, json.dumps(api_status(), ensure_ascii=False))
        elif u.path == "/api/search":
            self._send(200, json.dumps(api_search(parse_qs(u.query)), ensure_ascii=False))
        elif u.path == "/api/open":
            self._send(200, json.dumps(api_open(parse_qs(u.query)), ensure_ascii=False))
        elif u.path == "/api/snapshots":
            self._send(200, json.dumps(api_snapshots(), ensure_ascii=False))
        elif u.path == "/api/snapshot":
            self._send(200, json.dumps(api_snapshot(parse_qs(u.query)), ensure_ascii=False))
        elif u.path == "/api/pet/belly":
            self._send(200, json.dumps(pet_belly.list_items(), ensure_ascii=False))
        elif u.path == "/api/history":
            self._send(200, json.dumps(api_history(), ensure_ascii=False))
        elif u.path == "/api/feedback/list":
            self._send(200, json.dumps(api_feedback_list(), ensure_ascii=False))
        elif u.path == "/api/stats":
            self._send(200, json.dumps(api_stats(), ensure_ascii=False))
        elif u.path == "/api/pet/shop":
            self._send(200, json.dumps(pet_shop.status(), ensure_ascii=False))
        elif u.path == "/api/pet/weather":
            city = (parse_qs(u.query).get("city", [""])[0] or "").strip()
            if not city:  # 未填城市时优先用本机记住的默认城市，避免 IP 定位漂移
                city = _load_weather_city()
            self._send(200, json.dumps(pet_shop.get_weather(city), ensure_ascii=False))
        elif u.path == "/api/pet/weather/city":
            self._send(200, json.dumps({"city": _load_weather_city()},
                                       ensure_ascii=False))
        elif u.path == "/api/scan/status":
            self._send(200, json.dumps(scan_status(), ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/scan":
            ok = start_scan()
            self._send(200, json.dumps({"started": ok}, ensure_ascii=False))
        elif u.path == "/api/pet/weather/city":
            body = self._body_json()
            city = (body.get("city", "") or "").strip()[:30]
            try:
                if city:
                    WEATHER_CITY_PATH.write_text(city, encoding="utf-8")
                elif WEATHER_CITY_PATH.exists():
                    WEATHER_CITY_PATH.unlink()
                self._send(200, '{"ok":true}')
            except Exception as exc:
                self._send(200, json.dumps({"ok": False, "error": str(exc)},
                                           ensure_ascii=False))
        elif u.path == "/api/pet/eat":
            body = self._body_json()
            self._send(200, json.dumps(pet_belly.eat(body.get("path", "")),
                                       ensure_ascii=False))
        elif u.path == "/api/pet/eatmatch":
            qs = parse_qs(u.query)
            self._send(200, json.dumps(
                pet_belly.eat_match(qs.get("name", [""])[0], qs.get("size", [0])[0]),
                ensure_ascii=False))
        elif u.path == "/api/pet/spit":
            self._send(200, json.dumps(pet_belly.spit_all(), ensure_ascii=False))
        elif u.path == "/api/pet/signin":
            self._send(200, json.dumps(pet_shop.do_signin(), ensure_ascii=False))
        elif u.path == "/api/pet/buy":
            self._send(200, json.dumps(pet_shop.buy(self._body_json().get("food", "")), ensure_ascii=False))
        elif u.path == "/api/pet/feed":
            self._send(200, json.dumps(pet_shop.feed(self._body_json().get("food", "")), ensure_ascii=False))
        elif u.path == "/api/recycle":
            self._send(200, json.dumps(api_recycle(self._body_json()), ensure_ascii=False))
        elif u.path == "/api/similar":
            self._send(200, json.dumps(api_similar(self._body_json()), ensure_ascii=False))
        elif u.path == "/api/recycle/open":
            self._send(200, json.dumps(api_open_recyclebin(), ensure_ascii=False))
        elif u.path == "/api/ext/fileguard/launch":
            self._send(200, json.dumps(api_ext_fileguard_launch(), ensure_ascii=False))
        elif u.path == "/api/shortcut":
            self._send(200, json.dumps(api_create_shortcut(self._body_json()), ensure_ascii=False))
        elif u.path == "/api/feedback":
            self._send(200, json.dumps(api_feedback(self._body_json()), ensure_ascii=False))
        elif u.path == "/api/history/clear":
            self._send(200, json.dumps(api_history_clear(), ensure_ascii=False))
        else:
            self._send(404, '{"error":"not found"}')


# ---------- 内嵌页面（TeleAgent 风格 · 白底淡蓝 · 扁平图标 · 毛玻璃 · 宠物） ----------

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>文档拾忆 · 按内容找文件</title>
<style>
:root{
  --pri:#2f6da8; --pri-deep:#1d4e80; --pri-l:#eaf3fb; --pri-b:#c9e0f2;
  --txt:#22384f; --sub:#7f93a8; --bg:#f5f9fd; --card:#ffffff; --line:#e2ebf3;
  --r:14px; --shadow:0 4px 22px rgba(23,58,94,.07);
  --doc:#4a90d9; --pdf:#e05d5d; --ppt:#f0a04b; --xls:#5cb87a; --md:#45b8b0; --code:#f5c04b; --etc:#9db4c8;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:var(--bg);color:var(--txt);min-height:100vh}
svg.ic{width:15px;height:15px;stroke:currentColor;stroke-width:2;fill:none;
  stroke-linecap:round;stroke-linejoin:round;vertical-align:-2px}

/* ===== 顶部导航（流星主题渐变栏） ===== */
.appbar{background:linear-gradient(118deg,#173a5e 0%,#24568c 48%,#3b82c4 100%);
  color:#fff;padding:0 28px;height:64px;display:flex;align-items:center;gap:14px;
  box-shadow:0 2px 16px rgba(23,58,94,.25);position:relative;z-index:5;overflow:hidden}
/* 顶栏流星划过动画：与桌面图标呼应 */
.appbar .meteor-track{position:absolute;inset:0;pointer-events:none;overflow:hidden}
.appbar .meteor{position:absolute;width:74px;height:2.5px;border-radius:2px;
  background:linear-gradient(90deg,rgba(255,246,214,0),rgba(255,246,214,.95));
  filter:drop-shadow(0 0 5px rgba(255,230,150,.85));
  transform:rotate(-24deg);opacity:0;
  animation:meteorFly 9s ease-in infinite}
.appbar .meteor.m2{width:46px;animation-duration:13s;animation-delay:4.5s}
.appbar .meteor.m3{width:30px;animation-duration:16s;animation-delay:8s}
@keyframes meteorFly{
  0%{left:-90px;top:8px;opacity:0}
  3%{opacity:1}
  30%{left:110%;top:52px;opacity:0}
  100%{left:110%;top:52px;opacity:0}}
.appbar .mark{width:36px;height:36px;border-radius:10px;position:relative;
  background:linear-gradient(135deg,#24568c,#173a5e);
  border:1px solid rgba(255,255,255,.3);display:flex;align-items:center;justify-content:center;
  backdrop-filter:blur(6px);box-shadow:0 2px 10px rgba(10,25,45,.4)}
.appbar .mark svg{width:22px;height:22px;overflow:visible}
.appbar .mark .tr{animation:markTr 2.8s ease-in-out infinite;transform-origin:11px 13px}
@keyframes markTr{0%,100%{opacity:.55;transform:scaleX(1)}50%{opacity:1;transform:scaleX(1.12)}}
.appbar .title{font-size:17px;font-weight:700;letter-spacing:.5px}
.appbar .divider{width:1px;height:22px;background:rgba(255,255,255,.25)}
.appbar .subtitle{font-size:12px;color:rgba(255,255,255,.82)}
.appbar .right{margin-left:auto;display:flex;gap:10px;align-items:center}
.appbar .chip{font-size:11px;padding:4px 12px;border-radius:20px;
  background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);color:rgba(255,255,255,.9)}

.wrap{max-width:1000px;margin:0 auto;padding:18px 24px 120px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;
  box-shadow:var(--shadow);padding:18px 20px;margin-top:16px}

/* ===== 状态条 ===== */
.statusbar{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.stat{display:flex;align-items:center;gap:8px;background:var(--pri-l);
  border:1px solid var(--pri-b);border-radius:10px;padding:8px 14px;font-size:12px;color:var(--sub)}
.stat b{color:var(--pri);font-size:15px;font-weight:700}
.stat .ic{color:var(--pri)}
.scanbtn{margin-left:auto;display:inline-flex;align-items:center;gap:6px;border:1px solid var(--pri-b);
  background:#fff;color:var(--pri);padding:8px 16px;border-radius:10px;font-size:13px;cursor:pointer;transition:.15s}
.scanbtn:hover{background:var(--pri-l)}

/* ===== 搜索区 ===== */
.searchbox{display:flex;gap:10px;margin-top:16px}
.searchbox .kwwrap{flex:1;position:relative}
.searchbox .kwwrap .ic{position:absolute;left:15px;top:50%;transform:translateY(-50%);color:var(--sub)}
.searchbox input[type=text]{width:100%;border:1.5px solid var(--pri-b);border-radius:24px;
  padding:12px 16px 12px 42px;font-size:15px;outline:none;transition:.18s;background:#fff}
.searchbox input[type=text]:focus{border-color:var(--pri);box-shadow:0 0 0 4px rgba(47,109,168,.13)}
.searchbox button{display:inline-flex;align-items:center;gap:7px;border:none;
  background:linear-gradient(135deg,var(--pri-deep),var(--pri));color:#fff;padding:0 30px;
  border-radius:24px;font-size:14px;font-weight:600;cursor:pointer;transition:.15s;
  box-shadow:0 4px 14px rgba(36,86,140,.3)}
.searchbox button:hover{filter:brightness(1.08)}

.filters{display:flex;gap:9px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--sub);margin-top:13px}
.filters select,.filters input[type=text]{border:1px solid var(--line);border-radius:8px;
  padding:5px 9px;font-size:12px;color:var(--txt);background:#fff;outline:none}
.seg{display:flex;border:1px solid var(--pri-b);border-radius:9px;overflow:hidden}
.seg span{padding:5px 13px;cursor:pointer;background:#fff;transition:.15s;color:var(--sub);font-size:12px}
.seg span.on{background:var(--pri);color:#fff}
.seg-label{color:var(--sub);font-size:12px}
.draghint{width:100%;margin-top:12px;font-size:11.5px;color:var(--sub);background:var(--pri-l);
  border:1px dashed var(--pri-b);border-radius:9px;padding:7px 12px;display:flex;align-items:center;gap:7px}
.draghint .ic{color:var(--pri);flex-shrink:0}

/* 检索中：搜索按钮流星动画 */
.searchbox button.searching{position:relative;overflow:hidden;pointer-events:none}
.searchbox button.searching::after{content:"";position:absolute;top:8px;left:-40px;width:36px;height:2px;
  border-radius:2px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.95));
  transform:rotate(-18deg);animation:btnMeteor 1.1s linear infinite}
@keyframes btnMeteor{to{left:110%}}

/* 空态搜索图标：流星小动画 */
.empty .meteoric{width:64px;height:64px;margin:0 auto 10px;opacity:.9}
.empty .meteoric .tr{animation:markTr 2.8s ease-in-out infinite}

/* 滚动条精修 */
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#c7d8e8;border-radius:6px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:#a8c2dc}
::-webkit-scrollbar-track{background:transparent}

/* ===== 数据库面板 ===== */
.db-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:11px;margin-bottom:16px}
.db-card{background:var(--pri-l);border:1px solid var(--pri-b);border-radius:13px;padding:13px 16px}
.db-card .v{font-size:21px;font-weight:700;color:var(--pri);letter-spacing:.3px}
.db-card .v small{font-size:11px;font-weight:400;color:var(--sub);margin-left:3px}
.db-card .k{font-size:11px;color:var(--sub);margin-top:4px}
.db-sec{margin-bottom:16px}
.db-sec h4{font-size:13px;font-weight:700;color:var(--txt);margin-bottom:10px;display:flex;align-items:center;gap:7px}
.db-sec h4 .ic{color:var(--pri)}
.db-sec h4 .sub{font-size:10.5px;color:var(--sub);font-weight:400;margin-left:2px}
/* 类型分布横条 */
.tp-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.tp-row .nm{width:86px;font-size:11.5px;color:var(--txt);text-align:right;flex-shrink:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.tp-row .bar-wrap{flex:1;height:16px;background:var(--pri-l);border-radius:8px;overflow:hidden}
.tp-row .bar{height:100%;border-radius:8px;min-width:3px;
  background:linear-gradient(90deg,var(--pri-deep),var(--pri));transition:width .5s ease}
.tp-row .val{width:64px;font-size:11px;color:var(--sub);flex-shrink:0;font-family:Consolas,monospace}
/* 趋势柱状图 */
.tr-chart{display:flex;align-items:flex-end;gap:5px;height:92px;padding:6px 4px 0;
  background:var(--pri-l);border-radius:12px;border:1px solid var(--line)}
.tr-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0}
.tr-bar{width:70%;background:linear-gradient(180deg,#5c9fd6,var(--pri-deep));border-radius:4px 4px 0 0;
  min-height:2px;transition:height .5s ease}
.tr-lab{font-size:9px;color:var(--sub);white-space:nowrap;transform:scale(.86);font-family:Consolas,monospace}
.tr-val{font-size:9px;color:var(--pri);font-weight:700;font-family:Consolas,monospace}
/* 重复榜 */
.dup-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:10px;
  background:#fff;border:1px solid var(--line);margin-bottom:7px;transition:.15s}
.dup-row:hover{border-color:#f2dcae;box-shadow:0 2px 10px rgba(240,160,75,.1)}
.dup-row .rk{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,#f0a04b,#f5c04b);
  color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.dup-row .dn{flex:1;min-width:0;font-size:12px;font-weight:600;color:var(--txt);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dup-row .dc{font-size:11px;color:#c96a1a;background:#fdf0d5;border:1px solid #eddaa5;
  border-radius:6px;padding:1px 8px;white-space:nowrap;flex-shrink:0}
.dup-row .dw{font-size:11px;color:var(--sub);white-space:nowrap;flex-shrink:0;font-family:Consolas,monospace}
.db-empty{text-align:center;color:var(--sub);font-size:12px;padding:26px 0}

/* ===== 标签页 ===== */
.tabs{display:flex;gap:8px;margin:18px 0 12px;flex-wrap:wrap}
.tabs span{padding:7px 20px;font-size:13px;cursor:pointer;color:var(--sub);border-radius:20px;
  border:1px solid var(--line);background:#fff;transition:.15s}
.tabs span.on{color:#fff;background:linear-gradient(135deg,var(--pri-deep),var(--pri));
  border-color:transparent;font-weight:600;box-shadow:0 3px 10px rgba(36,86,140,.25)}
.tabs .badge-mini{display:inline-block;min-width:17px;height:17px;line-height:17px;border-radius:9px;
  background:#f0a04b;color:#fff;font-size:10px;font-weight:700;margin-left:5px;padding:0 5px}

/* ===== 结果 ===== */
.result-head{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin:4px 0 12px;font-size:13px;color:var(--sub)}
.result-head b{color:var(--pri)}

/* ===== 时间分布轴（文献检索时间轴的本地版） ===== */
.yearbar{display:none;align-items:flex-end;gap:5px;margin:0 0 14px;padding:12px 14px 10px;
  background:#fff;border:1px solid var(--line);border-radius:12px;overflow-x:auto}
.yearbar.show{display:flex}
.yearbar .yb-t{font-size:10.5px;color:var(--sub);white-space:nowrap;margin-right:8px;
  padding-bottom:14px;flex-shrink:0}
.yb{flex:1;min-width:34px;display:flex;flex-direction:column;align-items:center;gap:3px;
  cursor:pointer;flex-shrink:0;transition:.15s;padding-bottom:2px;border-radius:7px}
.yb:hover{background:var(--pri-l)}
.yb .yb-n{font-size:10px;color:var(--pri-deep);font-weight:700;font-family:Consolas,monospace}
.yb .yb-c{width:100%;max-width:38px;border-radius:4px 4px 2px 2px;min-height:4px;
  background:linear-gradient(180deg,#7fb3e0,var(--pri));transition:.15s}
.yb .yb-y{font-size:10px;color:var(--sub);font-family:Consolas,monospace;
  padding:1px 4px;border-radius:5px;white-space:nowrap}
.yb.on{background:var(--pri-l);outline:1.5px solid var(--pri);outline-offset:-1.5px}
.yb.on .yb-y{background:var(--pri);color:#fff;font-weight:700}
.yb.dim .yb-c{background:#c9d6e4;opacity:.55}
.yb.dim .yb-n{color:#9fb0c2}

/* ===== 相似文件弹窗 ===== */
.sim-item{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:10px;
  border:1px solid var(--line);background:#fff;margin-bottom:7px;cursor:pointer;transition:.15s}
.sim-item:hover{border-color:var(--pri-b);background:var(--pri-l)}
.sim-item .si-n{font-size:12.5px;font-weight:600;color:var(--txt);word-break:break-all}
.sim-item .si-m{font-size:10.5px;color:var(--sub);margin-top:3px;font-family:Consolas,monospace;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.sim-item .si-w{flex-shrink:0;font-size:10px;padding:2px 8px;border-radius:8px;font-weight:600}
.si-w.d1{background:var(--pri-l);color:var(--pri)}
.si-w.d2{background:#fdf3e2;color:#b26a1b}
.sim-item .si-x{margin-left:auto;flex-shrink:0;font-size:10.5px;color:var(--sub);
  font-family:Consolas,monospace;white-space:nowrap}
.sim-empty{font-size:12px;color:var(--sub);text-align:center;padding:14px 0}

/* ===== 结果卡片新按钮 ===== */
.cbtn.ai{border-color:#b8c9ea;color:#4a6fa5}
.cbtn.ai:hover{background:#eef3fb;color:#2f5da0}
.cbtn.sim{border-color:#c3d9c9;color:#3f8a63}
.cbtn.sim:hover{background:#eef7f1;color:#2e7d4f}
/* ===== 反馈模块（v1.2.0：角落入口已并入「设置」分区，样式清理） ===== */

.fb-mask{display:none;position:fixed;inset:0;background:rgba(23,58,94,.4);backdrop-filter:blur(4px);
  z-index:210;align-items:center;justify-content:center}
.fb-mask.show{display:flex}
.fb-modal{background:rgba(255,255,255,.96);backdrop-filter:blur(12px);border:1px solid #fff;
  border-radius:16px;padding:22px 26px;max-width:460px;width:90%;box-shadow:0 12px 48px rgba(23,58,94,.28)}
.fb-modal h3{font-size:15.5px;margin-bottom:4px;display:flex;align-items:center;gap:8px}
.fb-modal .fsub{font-size:11.5px;color:var(--sub);margin-bottom:13px;line-height:1.6}
.fb-types{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:11px}
.fb-types span{padding:5px 13px;border-radius:16px;border:1.5px solid var(--line);font-size:12px;
  color:var(--sub);cursor:pointer;transition:.15s;background:#fff}
.fb-types span.on{border-color:var(--pri);color:var(--pri);background:var(--pri-l);font-weight:600}
.fb-modal textarea{width:100%;height:110px;border:1.5px solid var(--pri-b);border-radius:11px;
  padding:11px 13px;font-size:13px;font-family:inherit;outline:none;resize:vertical;transition:.15s}
.fb-modal textarea:focus{border-color:var(--pri);box-shadow:0 0 0 3px rgba(47,109,168,.12)}
.fb-modal .fcontact{margin-top:9px;width:100%;border:1px solid var(--line);border-radius:9px;
  padding:8px 12px;font-size:12px;outline:none;font-family:inherit}
.fb-modal .fcontact:focus{border-color:var(--pri-b)}
.fb-btns{display:flex;gap:10px;margin-top:14px}
.fb-btns button{flex:1;border:none;border-radius:11px;padding:10px 0;font-size:13px;font-weight:600;cursor:pointer}
.fb-send{background:linear-gradient(135deg,var(--pri-deep),var(--pri));color:#fff;
  box-shadow:0 4px 14px rgba(36,86,140,.3)}
.fb-send:hover{filter:brightness(1.08)}
.fb-cancel{background:#fff;border:1.5px solid var(--line);color:var(--sub)}
.fb-cancel:hover{border-color:var(--pri-b);color:var(--pri)}
.fb-ok{text-align:center;padding:16px 0 6px}
.fb-ok .okic{width:52px;height:52px;margin:0 auto 10px;border-radius:50%;
  background:#e8f7ee;display:flex;align-items:center;justify-content:center}
.fb-ok .okic svg{width:26px;height:26px;stroke:#3a9a5f;stroke-width:2.4;fill:none;stroke-linecap:round;stroke-linejoin:round}
.fb-ok h4{font-size:15px;margin-bottom:8px}
.fb-ok p{font-size:11.5px;color:var(--sub);line-height:1.8;padding:0 10px}

/* ===== 左下角回收站（连接系统回收站） ===== */
.recycle-zone{position:fixed;left:22px;bottom:18px;z-index:60;width:92px;height:96px;
  display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
  user-select:none;cursor:pointer}
.recycle-zone .rc-ic{width:58px;height:58px;border-radius:16px;background:rgba(255,255,255,.9);
  backdrop-filter:blur(8px);border:1.5px solid var(--pri-b);display:flex;align-items:center;
  justify-content:center;box-shadow:0 4px 18px rgba(23,58,94,.12);transition:.18s;position:relative}
.recycle-zone svg.rc{width:30px;height:30px;stroke:#5a7fa3;stroke-width:1.8;fill:none;
  stroke-linecap:round;stroke-linejoin:round;transition:.18s}
.recycle-zone:hover .rc-ic{transform:translateY(-2px);box-shadow:0 6px 22px rgba(23,58,94,.18)}
.recycle-zone:hover svg.rc{stroke:var(--pri)}
.recycle-zone.dragover .rc-ic{border-color:#f0a04b;box-shadow:0 0 0 6px rgba(240,160,75,.18),0 6px 22px rgba(240,160,75,.25);
  transform:scale(1.08)}
.recycle-zone.dragover svg.rc{stroke:#e0884b}
.recycle-zone .rc-cap{font-size:10.5px;color:var(--sub);margin-top:6px;text-align:center;
  line-height:1.4;background:rgba(255,255,255,.85);padding:2px 8px;border-radius:8px}
.recycle-zone .rc-badge{position:absolute;right:-6px;top:-6px;min-width:20px;height:20px;border-radius:10px;
  background:linear-gradient(135deg,#f0a04b,#f5c04b);color:#fff;font-size:11px;font-weight:700;
  display:none;align-items:center;justify-content:center;padding:0 6px;border:2px solid #fff;
  box-shadow:0 3px 10px rgba(240,160,75,.5)}
.recycle-zone .rc-badge.show{display:inline-flex}
.recycle-zone.rc-done .rc-ic{animation:rcBounce .55s ease}
@keyframes rcBounce{0%,100%{transform:translateY(0)}35%{transform:translateY(-9px) rotate(-5deg)}70%{transform:translateY(0) rotate(3deg)}}
.recycle-zone.rc-done svg.rc{animation:rcFlash .55s ease}
@keyframes rcFlash{0%,100%{stroke:#5a7fa3}50%{stroke:#3a9a5f}}

/* ===== 结果卡片：入场动画 ===== */
.result{background:#fff;border:1px solid var(--line);border-radius:var(--r);
  padding:13px 16px 13px 19px;margin-bottom:11px;transition:.18s;position:relative;overflow:hidden;
  cursor:grab;animation:cardIn .32s ease both}
@keyframes cardIn{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:translateY(0)}}
.result:nth-child(1){animation-delay:.02s}.result:nth-child(2){animation-delay:.06s}
.result:nth-child(3){animation-delay:.1s}.result:nth-child(4){animation-delay:.14s}
.result:nth-child(5){animation-delay:.18s}.result:nth-child(6){animation-delay:.22s}
.result:active{cursor:grabbing}
.result::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--etc)}
.result[data-t=".docx"]::before{background:var(--doc)} .result[data-t=".pdf"]::before{background:var(--pdf)}
.result[data-t=".pptx"]::before{background:var(--ppt)} .result[data-t=".xlsx"]::before{background:var(--xls)}
.result[data-t=".md"]::before,.result[data-t=".markdown"]::before{background:var(--md)}
.result[data-t=".py"]::before,.result[data-t=".js"]::before,.result[data-t=".java"]::before{background:var(--code)}
.result:hover{border-color:var(--pri-b);box-shadow:0 4px 18px rgba(36,86,140,.12);transform:translateY(-1px)}
.rtop{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.rank{font-size:11px;color:#fff;background:linear-gradient(135deg,var(--pri-deep),var(--pri));
  border-radius:50%;min-width:22px;height:22px;display:inline-flex;align-items:center;
  justify-content:center;font-weight:700;flex-shrink:0}
.fname{font-size:14px;font-weight:600;word-break:break-all}
.tag{font-size:10px;border-radius:5px;padding:1px 8px;border:1px solid var(--line);color:var(--sub);flex-shrink:0}
.score{margin-left:auto;font-size:11px;color:var(--pri);background:var(--pri-l);
  border:1px solid var(--pri-b);border-radius:6px;padding:1px 9px;font-weight:600;flex-shrink:0}
.meta{font-size:11px;color:var(--sub);margin-top:8px;display:flex;gap:14px;flex-wrap:wrap}
.meta .ic{width:13px;height:13px}
.oprow{display:flex;gap:8px;margin-top:9px;flex-wrap:wrap;align-items:center}
.path{flex:1;min-width:200px;font-size:11px;color:var(--pri-deep);background:var(--pri-l);
  border-radius:7px;padding:6px 11px;font-family:Consolas,monospace;word-break:break-all}
.act{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--pri-b);background:#fff;
  color:var(--pri);border-radius:8px;padding:5px 11px;font-size:11px;cursor:pointer;transition:.15s;white-space:nowrap}
.act:hover{background:var(--pri-l)}
.act.done{background:#e8f7ee;border-color:#b5e0c4;color:#3a9a5f}
.snip{font-size:12px;line-height:1.75;color:#55606d;margin-top:9px;background:var(--pri-l);
  border-left:3px solid var(--pri-b);padding:7px 12px;border-radius:0 8px 8px 0;word-break:break-all}
mark{background:#fff3cd;color:#b8860b;padding:0 3px;border-radius:3px;font-weight:600}
.empty{text-align:center;color:var(--sub);padding:44px 0;font-size:13px;line-height:2.2}

/* 重复副本区 */
.dupbox{margin-top:9px;background:#fffbe9;border:1px solid #f2e2b0;border-radius:9px;overflow:hidden}
.dup-head{font-size:11px;color:#a07728;padding:6px 11px;display:flex;align-items:center;gap:6px;
  background:#fdf6e0;border-bottom:1px solid #f2e2b0}
.dup-head .ic{width:13px;height:13px;color:#c99a30}
.dup-head .tip{margin-left:auto;color:#b8952f;font-size:10px}
.dup-item{display:flex;align-items:center;gap:8px;padding:6px 11px;font-size:11px;color:#7a6a3a;
  border-bottom:1px dashed #efe2b5;cursor:grab;transition:.15s}
.dup-item:last-child{border-bottom:none}
.dup-item:hover{background:#fdf3d5}
.dup-item:active{cursor:grabbing}
.dup-item .dpath{flex:1;font-family:Consolas,monospace;word-break:break-all;color:#8a7a4a}
.dup-item .dtime{color:#b0a070;white-space:nowrap}
.dup-item .act{padding:3px 9px;font-size:10px}
.dup-item.recycled{animation:dupGone .5s ease forwards}
@keyframes dupGone{to{opacity:0;transform:translateX(14px) scale(.9)}}
.dup-badge{font-size:10px;color:#a06a1a;background:#fdf0d5;border:1px solid #eddaa5;
  border-radius:5px;padding:0 7px;font-weight:600;flex-shrink:0}

/* 吞噬动画：空间扭曲变小 */
.result.eaten{animation:warpAway .62s cubic-bezier(.5,0,.9,.4) forwards;pointer-events:none}
@keyframes warpAway{
  0%{transform:perspective(700px) rotateY(0) rotate(0) scale(1) skewX(0);filter:blur(0);opacity:1;
     box-shadow:0 0 0 0 rgba(245,192,75,0)}
  55%{transform:perspective(700px) rotateY(38deg) rotate(9deg) scale(.42) skewX(6deg);
     filter:blur(3px) saturate(1.6);opacity:.75;box-shadow:0 0 34px 8px rgba(245,192,75,.55)}
  100%{transform:perspective(700px) rotateY(78deg) rotate(26deg) scale(.04) skewX(14deg);
     filter:blur(8px) saturate(2);opacity:0;box-shadow:0 0 60px 20px rgba(245,192,75,.35)}}

/* ===== 检索历史 ===== */
.hist-bar{display:flex;align-items:center;gap:10px;margin-bottom:12px;font-size:12px;color:var(--sub)}
.hist-bar .cnt{color:var(--pri);font-weight:700}
.hist-clear{margin-left:auto;border:1px solid #e8c9c9;background:#fdf3f3;color:#c96a5a;
  border-radius:8px;padding:5px 12px;font-size:11px;cursor:pointer}
.hist-clear:hover{background:#f9e3e3}
.hist-item{display:flex;align-items:center;gap:12px;background:#fff;border:1px solid var(--line);
  border-radius:12px;padding:11px 15px;margin-bottom:9px;transition:.15s;flex-wrap:wrap}
.hist-item:hover{border-color:var(--pri-b);box-shadow:0 3px 14px rgba(36,86,140,.08)}
.h-time{font-size:11px;color:var(--sub);font-family:Consolas,monospace;white-space:nowrap;flex-shrink:0}
.h-kw{display:flex;gap:5px;flex-wrap:wrap;flex-shrink:1;min-width:120px}
.h-kw span{background:var(--pri-l);border:1px solid var(--pri-b);color:var(--pri);
  border-radius:14px;padding:1px 10px;font-size:12px;font-weight:600}
.h-cond{font-size:11px;color:var(--sub);flex-shrink:1}
.h-hits{font-size:11px;color:var(--sub);white-space:nowrap}
.h-hits b{color:var(--pri)}
.h-go{margin-left:auto;border:none;border-radius:8px;padding:5px 14px;font-size:11px;white-space:nowrap;
  background:linear-gradient(135deg,var(--pri-deep),var(--pri));color:#fff;cursor:pointer;flex-shrink:0}
.h-go:hover{filter:brightness(1.08)}
.hist-empty{text-align:center;color:var(--sub);padding:40px 0;font-size:13px}

/* ===== 快照 ===== */
.snapbar{display:flex;gap:10px;align-items:center;margin:6px 0 12px}
.snapbar select{border:1px solid var(--pri-b);border-radius:9px;padding:6px 10px;font-size:12px}
.snap-content{background:var(--pri-l);border:1px solid var(--line);border-radius:var(--r);
  padding:16px 20px;font-size:12px;line-height:1.9;white-space:pre-wrap;max-height:62vh;overflow:auto}

/* ===== 宠物区 ===== */
.pet-zone{position:fixed;right:22px;bottom:18px;z-index:60;width:230px;height:245px;
  display:flex;align-items:flex-end;justify-content:center;user-select:none}
.pet-zone.dragover{background:radial-gradient(circle at 50% 70%,rgba(59,130,196,.14),transparent 65%)}
.bubble{position:absolute;right:80px;bottom:150px;max-width:240px;background:rgba(255,255,255,.94);
  backdrop-filter:blur(8px);border:1px solid var(--pri-b);border-radius:14px 14px 4px 14px;
  padding:9px 14px;font-size:12.5px;line-height:1.65;color:var(--txt);
  box-shadow:0 4px 18px rgba(36,86,140,.16);opacity:0;transform:translateY(6px);transition:.25s;pointer-events:none}
.bubble.show{opacity:1;transform:translateY(0)}

/* 肚子面板 */
.belly-panel{position:absolute;right:56px;bottom:168px;width:300px;background:rgba(255,255,255,.95);
  backdrop-filter:blur(10px);border:1px solid var(--pri-b);border-radius:14px;padding:12px 14px;
  box-shadow:0 8px 30px rgba(36,86,140,.2);display:none}
.belly-panel.show{display:block}
.bp-title{font-size:12px;font-weight:700;color:var(--txt);display:flex;align-items:center;gap:6px}
.bp-title .cnt{color:var(--pri)}
.bp-sub{font-size:10.5px;color:var(--sub);margin-top:3px;line-height:1.6}
.bp-list{margin-top:8px;max-height:150px;overflow:auto}
.bp-item{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;
  background:var(--pri-l);margin-bottom:6px;font-size:11px}
.bp-item .nm{flex:1;word-break:break-all;font-weight:600;color:var(--txt)}
.bp-item .cd{color:#d9822b;font-family:Consolas,monospace;white-space:nowrap}
.bp-empty{font-size:11px;color:var(--sub);text-align:center;padding:10px 0}
.bp-btn{width:100%;margin-top:4px;border:none;border-radius:9px;padding:7px 0;font-size:12px;
  background:linear-gradient(135deg,var(--pri-deep),var(--pri));color:#fff;cursor:pointer}
.bp-btn:hover{filter:brightness(1.08)}

/* 肚子徽标 */
.belly-badge{position:absolute;right:36px;bottom:62px;min-width:22px;height:22px;border-radius:11px;
  background:linear-gradient(135deg,#f0a04b,#f5c04b);color:#fff;font-size:11px;font-weight:700;
  display:none;align-items:center;justify-content:center;padding:0 6px;
  box-shadow:0 3px 10px rgba(240,160,75,.5);cursor:pointer;border:2px solid #fff}
.belly-badge.show{display:inline-flex}
.belly-badge.full99{background:linear-gradient(135deg,#e05d5d,#f0a04b)}
.belly-badge.pulse{animation:badgePulse .6s ease}
@keyframes badgePulse{0%{transform:scale(1)}40%{transform:scale(1.35)}100%{transform:scale(1)}}

/* 宠物本体（细节版：头-脖子-身体分区） */
.pet{cursor:pointer;filter:drop-shadow(0 8px 18px rgba(36,86,140,.22));position:relative;z-index:2}
.pet svg{width:136px;height:140px;display:block;overflow:visible}
.pet .body-g{animation:breathe 2.8s ease-in-out infinite;transform-origin:75px 115px}
@keyframes breathe{0%,100%{transform:scaleY(1)}50%{transform:scaleY(1.035)}}
.pet .tail-g{animation:wag 1.5s ease-in-out infinite;transform-origin:104px 115px}
@keyframes wag{0%,100%{transform:rotate(0)}50%{transform:rotate(-14deg)}}
.pet .eye-n{animation:blink 4.4s infinite;transform-origin:center}
@keyframes blink{0%,94%,100%{transform:scaleY(1)}97%{transform:scaleY(.06)}}
.pet .arm-r{transform-origin:108px 98px}
.pet.welcome .arm-r{animation:wave .5s ease-in-out 5}
@keyframes wave{0%,100%{transform:rotate(0)}50%{transform:rotate(-40deg)}}
/* 表情开关（默认全关，按状态类点亮）。
   规则：张圆形大嘴仅限吃文件(eating)/吐文件(spit)；其余一律闭合弯月笑 */
.eye-h,.mouth-o,.mouth-s,.mouth-big,.brow-s,.mouth-n{display:none}
.pet.idle .mouth-n,.pet.welcome .mouth-n,.pet.happy .mouth-n{display:block}
.pet.happy .eye-h,.pet.eating .eye-h,.pet.hungry .eye-h,.pet.feeding .eye-h,
.pet.full .eye-h,.pet.welcome .eye-h{display:block}
.pet.happy .eye-n,.pet.eating .eye-n,.pet.hungry .eye-n,.pet.feeding .eye-n,
.pet.full .eye-n,.pet.welcome .eye-n{display:none}
.pet.sad .mouth-s{display:block}
.pet.sad .eye-n{display:block}
.pet.sad .brow-s{display:block}
.pet.sad .ear-l,.pet.sad .ear-r{transform:rotate(22deg)}
.pet.sad .ear-l{transform-origin:52px 45px}
.pet.sad .ear-r{transform-origin:98px 45px}
/* 张圆形大嘴：只给吃文件/吐文件瞬间 */
.pet.eating .mouth-big,.pet.spit .mouth-big{display:block}
.pet.eating .head-g{animation:leanIn .3s ease}
/* hungry（拖文件悬停）：保持弯月笑+前倾期待，不再张大嘴 */
.pet.hungry .head-g{animation:leanIn .3s ease}
.pet.hungry .mouth-n{display:block}
@keyframes leanIn{from{transform:translateY(0)}to{transform:translateY(3px)}}
.pet.happy{animation:jump .55s ease 2}
@keyframes jump{0%,100%{transform:translateY(0)}45%{transform:translateY(-15px)}}
.pet.gulp .head-g{animation:gulp .55s ease}
@keyframes gulp{0%,100%{transform:translateY(0)}40%{transform:translateY(7px)}70%{transform:translateY(-2px)}}
.pet .belly-e{transition:transform .45s cubic-bezier(.3,1.6,.5,1);transform-origin:75px 119px}
.pet.full .belly-e{transform:scale(1.22)}
.pet.full .body-g{animation:bellyBounce .6s ease}
@keyframes bellyBounce{0%,100%{transform:scaleY(1)}45%{transform:scaleY(1.05) scaleX(1.04)}}
.pet.thinking .think-g{display:block}
.think-g{display:none}
.think-g circle{animation:dots 1.2s infinite}
.think-g circle:nth-child(2){animation-delay:.2s}
.think-g circle:nth-child(3){animation-delay:.4s}
@keyframes dots{0%,60%,100%{opacity:.25}30%{opacity:1}}
.pet.thinking .head-g{animation:tilt 2.4s ease-in-out infinite;transform-origin:75px 63px}
@keyframes tilt{0%,100%{transform:rotate(0)}50%{transform:rotate(-7deg)}}

/* 吃掉时的星光闪烁 */
.spark{position:absolute;left:50%;top:36%;width:9px;height:9px;border-radius:2px;
  background:radial-gradient(#fff6d8,#f5c04b);box-shadow:0 0 10px #f5c04b,0 0 18px #ffd97a;
  transform:rotate(45deg);animation:sparkFly .75s ease-out forwards;pointer-events:none;z-index:3}
@keyframes sparkFly{to{transform:translate(var(--sx),var(--sy)) scale(.15) rotate(230deg);opacity:0}}
.burp-fx{position:absolute;left:64%;top:18%;font-size:15px;font-weight:700;color:#c96a2d;
  text-shadow:0 0 8px rgba(245,192,75,.7);animation:burpUp 1.1s ease-out forwards;pointer-events:none;z-index:3}
@keyframes burpUp{0%{opacity:0;transform:translateY(0) scale(.6)}25%{opacity:1}
  100%{opacity:0;transform:translateY(-34px) scale(1.25)}}

/* 吐出：抛物线飞行 */
.fly-outer{position:fixed;z-index:120;pointer-events:none;animation:flyX .95s linear forwards}
@keyframes flyX{to{transform:translateX(var(--dx))}}
.fly-inner{animation:flyY .95s cubic-bezier(.3,.15,.7,.95) forwards}
@keyframes flyY{0%{transform:translateY(0)}45%{transform:translateY(-120px)}100%{transform:translateY(var(--dy))}}
.fly-chip{background:#fff;border:1.5px solid var(--pri-b);border-radius:9px;padding:5px 12px;
  font-size:11.5px;font-weight:600;color:var(--txt);white-space:nowrap;max-width:220px;overflow:hidden;
  text-overflow:ellipsis;box-shadow:0 8px 24px rgba(36,86,140,.3);
  animation:chipGrow .95s ease-out forwards}
@keyframes chipGrow{0%{transform:scale(.12) rotate(-18deg);filter:blur(5px);opacity:.15}
  30%{opacity:1}100%{transform:scale(1) rotate(0);filter:blur(0);opacity:1}}

/* ===== 睡觉 ===== */
.pet .eye-c,.pet .zzz{display:none}
.pet.sleep .eye-n,.pet.sleep .eye-h{display:none}
.pet.sleep .eye-c{display:block}
.pet.sleep .zzz{display:block}
.pet.sleep .body-g{animation:breathe 4.6s ease-in-out infinite}
.pet.sleepy .head-g{animation:tiltSlow 2.8s ease-in-out infinite;transform-origin:75px 63px}
.pet.sleepy .mouth-n{display:block}
@keyframes tiltSlow{0%,100%{transform:rotate(0)}50%{transform:rotate(-10deg)}}
.pet .zzz text{font:700 13px sans-serif;fill:#6b9bd2}
.pet .zzz text:nth-child(1){animation:zzUp 2.2s ease-out infinite}
.pet .zzz text:nth-child(2){animation:zzUp 2.2s ease-out infinite .7s}
.pet .zzz text:nth-child(3){animation:zzUp 2.2s ease-out infinite 1.4s}
@keyframes zzUp{0%{opacity:0;transform:translateY(4px) scale(.7)}30%{opacity:1}
  100%{opacity:0;transform:translateY(-14px) scale(1.15)}}

/* ===== 天气装扮 ===== */
.wear{display:none}
.pet-zone.w-rain .wear-umbrella,.pet-zone.w-rain .wear-boots{display:block}
.pet-zone.w-wind .wear-scarf{display:block}
.pet-zone.w-snow .wear-coat,.pet-zone.w-snow .wear-earmuffs{display:block}
/* 晴：左上角小太阳 */
.sun-ic{display:none;position:absolute;left:4px;top:-56px;width:44px;height:44px;z-index:1}
.pet-zone.w-sun .sun-ic{display:block}
.sun-ic .core{position:absolute;left:12px;top:12px;width:20px;height:20px;border-radius:50%;
  background:radial-gradient(circle at 35% 35%,#ffe9a8,#f5c04b);box-shadow:0 0 14px rgba(245,192,75,.8)}
.sun-ic .ray{position:absolute;left:20.5px;top:2px;width:3px;height:8px;border-radius:2px;
  background:#f5c04b;transform-origin:1.5px 20px}
.sun-ic .rays{position:absolute;inset:0;animation:sunSpin 14s linear infinite}
@keyframes sunSpin{to{transform:rotate(360deg)}}
/* 雨：云 + 实时雨滴 */
.cloudy{display:none;position:absolute;left:50%;top:-72px;transform:translateX(-50%);z-index:1}
.pet-zone.w-rain .cloudy{display:block}
.cloudy .cl{position:absolute;background:linear-gradient(#c8d6e5,#9fb3c8);border-radius:50%}
.cloudy .cl:nth-child(1){left:0;top:12px;width:34px;height:34px}
.cloudy .cl:nth-child(2){left:22px;top:0;width:44px;height:44px}
.cloudy .cl:nth-child(3){left:52px;top:10px;width:36px;height:36px}
.cloudy .clbase{position:absolute;left:2px;top:34px;width:82px;height:16px;border-radius:10px;
  background:#9fb3c8}
.rain-box{display:none;position:absolute;left:24px;right:24px;top:-36px;height:110px;
  overflow:hidden;pointer-events:none;z-index:0}
.pet-zone.w-rain .rain-box{display:block}
.rain-box .rd{position:absolute;top:-14px;width:1.6px;height:11px;border-radius:1px;
  background:linear-gradient(rgba(122,179,224,0),#7ab3e0);animation:rdFall .75s linear infinite}
@keyframes rdFall{to{transform:translateY(150px)}}
/* 雪：雪花飘落 */
.snow-box{display:none;position:absolute;left:14px;right:14px;top:-56px;height:130px;
  overflow:hidden;pointer-events:none;z-index:0}
.pet-zone.w-snow .snow-box{display:block}
.snow-box .sd{position:absolute;top:-8px;width:5px;height:5px;border-radius:50%;
  background:#fff;box-shadow:0 0 5px #dfeaf5;animation:sdFall 4.2s linear infinite}
@keyframes sdFall{to{transform:translateY(160px) translateX(10px)}}
/* 风：枫叶围绕 */
.leaf-box{display:none;position:absolute;left:-6px;right:-6px;top:-40px;height:190px;
  overflow:visible;pointer-events:none;z-index:1}
.pet-zone.w-wind .leaf-box{display:block}
.leaf-box .lf{position:absolute;animation:lfFly 4.4s linear infinite}
@keyframes lfFly{0%{transform:translate(-16px,0) rotate(0);opacity:0}
  12%{opacity:1}88%{opacity:1}
  100%{transform:translate(190px,-26px) rotate(520deg);opacity:0}}
.leaf-box .lf svg{display:block}
/* 天气小标签 */
.wchip{display:none;position:absolute;left:50%;top:-92px;transform:translateX(-50%);
  font-size:10.5px;color:#fff;background:rgba(47,109,168,.78);padding:3px 10px;border-radius:10px;
  white-space:nowrap;backdrop-filter:blur(4px);z-index:2}
.pet-zone.w-sun .wchip,.pet-zone.w-rain .wchip,.pet-zone.w-snow .wchip,.pet-zone.w-wind .wchip{display:block}

/* 吐文件：张圆形大嘴 + 仰头吐出节奏 */
.pet.spit .mouth-big{display:block}
.pet.spit .head-g{animation:gulp .5s ease infinite}

/* ===== 喂食特效粒子 ===== */
.fxP{position:absolute;left:50%;top:30%;font-size:15px;pointer-events:none;z-index:3;
  animation:fxFly 1.5s ease-out forwards}
@keyframes fxFly{0%{opacity:0;transform:translate(0,0) scale(.5)}
  20%{opacity:1}100%{opacity:0;transform:translate(var(--fx),-70px) scale(1.3) rotate(var(--fr))}}
/* 喂粮食：抿嘴弯月笑 + 点头咀嚼，不再张大嘴 */
.pet.feeding .mouth-n{display:block}
.pet.feeding .head-g{animation:gulp .5s ease infinite}
.pet.anim-jerky .ear-l,.pet.anim-jerky .ear-r{animation:earUp .4s ease infinite}
@keyframes earUp{0%,100%{transform:rotate(0)}50%{transform:rotate(-9deg)}}
.pet.anim-can{animation:jump .55s ease 3}
.pet.anim-feast{animation:jump .5s ease 4}
.pet.anim-feast .heart-fx{display:block}
.pet .heart-fx{display:none}

/* ===== 天气授权弹窗 ===== */
.wauth-info{background:var(--pri-l);border-radius:11px;padding:11px 14px;
  font-size:11.5px;color:var(--sub);line-height:1.9}
.wauth-info b{color:var(--pri)}
.wcity-input{margin-top:12px;width:100%;border:1.5px solid var(--pri-b);border-radius:10px;
  padding:9px 12px;font-size:13px;outline:none;font-family:inherit;transition:.15s}
.wcity-input:focus{border-color:var(--pri);box-shadow:0 0 0 3px rgba(47,109,168,.12)}

/* ===== 签到 / 商店 / 拓展面板 ===== */
.pm-mask{display:none;position:fixed;inset:0;background:rgba(23,58,94,.4);backdrop-filter:blur(4px);
  z-index:220;align-items:center;justify-content:center}
.pm-mask.show{display:flex}
.pm{background:rgba(255,255,255,.97);backdrop-filter:blur(12px);border:1px solid #fff;
  border-radius:16px;padding:20px 24px;max-width:470px;width:90%;
  box-shadow:0 12px 48px rgba(23,58,94,.28);max-height:86vh;overflow:auto}
.pm h3{font-size:15.5px;display:flex;align-items:center;gap:8px;margin-bottom:2px}
.pm .psub{font-size:11.5px;color:var(--sub);line-height:1.6;margin-bottom:12px}
.pts-row{display:flex;gap:10px;margin-bottom:14px}
.pts-card{flex:1;background:var(--pri-l);border:1px solid var(--pri-b);border-radius:12px;
  padding:10px 6px;text-align:center}
.pts-card b{display:block;font-size:20px;color:var(--pri-deep);font-family:Consolas,monospace}
.pts-card span{font-size:10.5px;color:var(--sub)}
.week-row{display:flex;gap:6px;margin-bottom:14px}
.wk{flex:1;text-align:center;border-radius:10px;padding:7px 0;border:1px solid var(--line);background:#fff}
.wk .d{font-size:10px;color:var(--sub)}
.wk .dot{width:9px;height:9px;border-radius:50%;background:#e3eaf2;margin:5px auto 0}
.wk.on{background:var(--ok-bg);border-color:#b5e0c4}
.wk.on .dot{background:#3a9a5f}
.wk.today{border-color:var(--pri);border-width:1.5px}
.sign-btn{width:100%;border:none;border-radius:11px;padding:12px 0;font-size:14px;font-weight:700;
  cursor:pointer;color:#fff;background:linear-gradient(135deg,#c9852e,#f0a04b);
  box-shadow:0 4px 14px rgba(240,160,75,.35);transition:.15s}
.sign-btn:hover{filter:brightness(1.07)}
.sign-btn:disabled{opacity:.6;cursor:not-allowed}
.pm-sub-btn{width:100%;margin-top:9px;border:none;border-radius:10px;padding:9px 0;font-size:12.5px;
  cursor:pointer;background:#fff;border:1.5px solid var(--pri-b);color:var(--pri)}
.pm-sub-btn:hover{background:var(--pri-l)}
/* 商店卡片 */
.shop-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.food-card{border:1px solid var(--line);border-radius:13px;padding:12px 10px;background:#fff;
  display:flex;flex-direction:column;transition:.15s}
.food-card:hover{border-color:var(--pri-b);box-shadow:0 4px 16px rgba(36,86,140,.1)}
.food-card .fe{font-size:26px;line-height:1;margin-bottom:6px}
.food-card .fn{font-size:13px;font-weight:700;color:var(--txt)}
.food-card .fd{font-size:10.5px;color:var(--sub);line-height:1.5;margin:4px 0 8px;flex:1}
.food-card .fp{font-size:12px;color:#c9852e;font-weight:700;margin-bottom:8px}
.food-card .fp b{font-size:15px}
.food-card .fb-row{display:flex;gap:6px}
.food-card .fb-row button{flex:1;border:none;border-radius:8px;padding:7px 0;font-size:11.5px;
  font-weight:600;cursor:pointer;transition:.15s}
.fb-buy{background:linear-gradient(135deg,var(--pri-deep),var(--pri));color:#fff}
.fb-buy:hover{filter:brightness(1.08)}
.fb-buy:disabled{opacity:.5;cursor:not-allowed}
.fb-feed{background:var(--ok-bg);border:1px solid #b5e0c4;color:#3a9a5f}
.fb-feed:hover{filter:brightness(1.05)}
.fb-feed:disabled{opacity:.45;cursor:not-allowed}
.own-tag{font-size:10.5px;color:var(--pri);background:var(--pri-l);border-radius:7px;
  padding:1px 7px;margin-left:5px;display:inline-block}

/* ===== 弹窗 ===== */
.mask{display:none;position:fixed;inset:0;background:rgba(23,58,94,.4);backdrop-filter:blur(4px);
  z-index:200;align-items:center;justify-content:center}
.mask.show{display:flex}
.modal{background:rgba(255,255,255,.96);backdrop-filter:blur(12px);border:1px solid #fff;
  border-radius:16px;padding:24px 28px;max-width:480px;width:88%;box-shadow:0 12px 48px rgba(23,58,94,.28)}
.modal h3{font-size:15px;margin-bottom:10px}
.modal pre{font-size:11.5px;color:var(--sub);line-height:1.7;max-height:200px;overflow:auto;
  background:var(--pri-l);border-radius:9px;padding:11px;white-space:pre-wrap}
.modal .close{text-align:right;margin-top:14px}

/* 首次使用欢迎（授权全盘扫描） */
.welcome .wpet{width:88px;height:90px;margin:0 auto 6px}
.welcome h3{font-size:17px;text-align:center;margin-bottom:12px}
.welcome .winfo{font-size:12.5px;color:var(--sub);line-height:1.9;
  background:var(--pri-l);border-radius:11px;padding:13px 16px}
.welcome .winfo b{color:var(--txt)}
.welcome .wask{font-size:14px;font-weight:700;color:var(--txt);text-align:center;margin:15px 0 4px}
.welcome .wbtns{display:flex;gap:12px;margin-top:14px}
.welcome .wbtns button{flex:1;border:none;border-radius:11px;padding:11px 0;font-size:13.5px;
  font-weight:600;cursor:pointer;transition:.15s}
.welcome .wyes{background:linear-gradient(135deg,var(--pri-deep),var(--pri));color:#fff;
  box-shadow:0 4px 14px rgba(36,86,140,.3)}
.welcome .wyes:hover{filter:brightness(1.08)}
.welcome .wno{background:#fff;border:1.5px solid var(--line);color:var(--sub)}
.welcome .wno:hover{border-color:var(--pri-b);color:var(--pri)}
/* 快捷方式勾选项 */
.wshort{margin-top:13px;display:flex;align-items:center;gap:9px;cursor:pointer;
  border:1.5px dashed var(--pri-b);border-radius:10px;padding:9px 13px;transition:.15s;
  background:#fff;user-select:none}
.wshort:hover{background:var(--pri-l)}
.wshort input{width:16px;height:16px;accent-color:var(--pri);cursor:pointer;flex-shrink:0}
.wshort .wt{font-size:12px;color:var(--txt);line-height:1.5}
.wshort .wt b{color:var(--pri)}
.wshort .wt .desc{display:block;font-size:10.5px;color:var(--sub);font-weight:400}

/* 顶部待授权横幅（选「暂不」后显示） */
.auth-banner{display:none;align-items:center;gap:12px;margin-top:14px;
  background:linear-gradient(118deg,#fff7e8,#fdeed2);border:1.5px solid #f2dcae;
  border-radius:13px;padding:11px 16px;box-shadow:0 3px 14px rgba(240,160,75,.15);flex-wrap:wrap}
.auth-banner.show{display:flex;animation:cardIn .35s ease}
.auth-banner .ab-ic{width:36px;height:36px;border-radius:10px;flex-shrink:0;
  background:linear-gradient(135deg,#f0a04b,#f5c04b);display:flex;align-items:center;justify-content:center;
  box-shadow:0 3px 10px rgba(240,160,75,.4)}
.auth-banner .ab-ic svg{width:19px;height:19px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.auth-banner .ab-tx{flex:1;min-width:220px}
.auth-banner .ab-tx .t1{font-size:13px;font-weight:700;color:#8a6216}
.auth-banner .ab-tx .t2{font-size:11px;color:#a9862f;line-height:1.6;margin-top:2px}
.auth-banner .ab-go{border:none;border-radius:9px;padding:9px 20px;font-size:12.5px;font-weight:600;
  background:linear-gradient(135deg,#e0884b,#f0a04b);color:#fff;cursor:pointer;white-space:nowrap;
  box-shadow:0 3px 10px rgba(224,136,75,.35);transition:.15s;flex-shrink:0}
.auth-banner .ab-go:hover{filter:brightness(1.08)}
.auth-banner .ab-x{border:none;background:none;color:#c9a75a;font-size:16px;cursor:pointer;
  padding:4px 6px;line-height:1;flex-shrink:0;border-radius:6px}
.auth-banner .ab-x:hover{background:rgba(240,160,75,.15)}

footer{max-width:1000px;margin:0 auto;padding:16px 24px 30px;color:var(--sub);font-size:11px;
line-height:1.9;border-top:1px solid var(--line)}
footer code{background:#fff;border:1px solid var(--line);border-radius:4px;padding:1px 6px}

/* ===== v1.2.0 · 检索第一优先级：最近搜索胶囊 / 批量回收条 / 设置页 / 空态诊断 ===== */
.recentbar{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin:12px 0 0}
.rb-t{font-size:12px;color:var(--sub)}
.rb-chip{font-size:12px;color:#2f6da8;background:#eef6fe;border:1px solid #d3e5f5;border-radius:20px;
padding:3px 11px;cursor:pointer;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rb-chip:hover{background:#e0effc;border-color:#b9d9f2}
.rb-all{margin-left:auto;font-size:12px;color:var(--pri);cursor:pointer;font-weight:600}
.rb-all:hover{text-decoration:underline}

.selbar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:10px 0;padding:8px 13px;
background:#eef6fe;border:1px solid #cfe3f5;border-radius:11px;font-size:12.5px}
.selbar input[type=checkbox]{accent-color:var(--pri);width:14px;height:14px;cursor:pointer}
.sb-all{display:inline-flex;align-items:center;gap:5px;cursor:pointer;color:#2f5d8a}
.sb-info{color:#2f5d8a}
.sb-go{margin-left:auto;background:linear-gradient(135deg,#e08a4b,#d0713a);color:#fff;border:none;border-radius:8px;
padding:5px 14px;font-size:12.5px;cursor:pointer;font-weight:600}
.sb-go:hover{filter:brightness(1.06)}
.sb-tip{color:var(--sub);font-size:11.5px}

.result .ck{display:inline-flex;align-items:center;margin-right:2px;vertical-align:-2px}
.result .ck input{accent-color:var(--pri);width:15px;height:15px;cursor:pointer}
.result.sel-on{outline:2px solid #a9cdf0;outline-offset:-2px;border-radius:12px}

.empty .e-title{font-size:15px;font-weight:700;color:#2f5d8a;margin:6px 0 12px}
.empty .e-tips{display:flex;flex-direction:column;gap:8px;text-align:left;max-width:640px;margin:0 auto}
.empty .e-tip{font-size:12.5px;color:#4a6b8f;background:#f4f9fe;border:1px solid #ddebf7;border-radius:9px;
padding:8px 12px;line-height:1.7}
.empty .e-tip a{color:var(--pri);font-weight:700;text-decoration:none;border-bottom:1px dashed var(--pri)}
.empty .e-tip a:hover{border-bottom-style:solid}
.empty .e-more{margin-top:12px;font-size:12px}
.empty .e-more a{color:var(--pri);text-decoration:none}
.empty .e-more a:hover{text-decoration:underline}

.hist-scroll{max-height:52vh;overflow-y:auto;border:1px solid var(--line);border-radius:10px;
background:#fff;padding:6px 10px;margin-top:8px}
.rc-list{max-height:190px;overflow-y:auto;border:1px solid var(--line);border-radius:10px;background:#fff;
padding:8px 12px;margin-top:10px;font-size:12.5px;color:#4a6b8f;line-height:1.9;text-align:left}
.rc-note{margin-top:10px;font-size:12px;color:#2f7a4f;background:#f0faf4;border:1px solid #cfe9d8;
border-radius:8px;padding:7px 11px;text-align:left}

.bd-list{display:flex;flex-direction:column;gap:7px;margin-top:10px;text-align:left}
.bd-row{display:flex;gap:10px;font-size:12.5px;line-height:1.7;align-items:baseline}
.bd-row b{flex:0 0 92px;color:#2f5d8a;font-size:12px}
.bd-row span{color:#4a6b8f}
.set-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
.set-btn{font-size:13px;background:#fff;border:1px solid var(--pri);color:var(--pri);border-radius:9px;
padding:8px 16px;cursor:pointer;font-weight:600}
.set-btn:hover{background:#eef6fe}
.set-btn.fg-btn{border-color:#2f7a4f;color:#2f7a4f}
.set-btn.fg-btn:hover{background:#f0faf4}
.snapbar{display:flex;align-items:center;gap:10px;margin-top:8px}
.snapbar select{border:1px solid var(--line);border-radius:7px;padding:4px 8px;font-size:12.5px}
</style>
</head>
<body>

<div class="appbar">
  <div class="meteor-track">
    <span class="meteor m1"></span><span class="meteor m2"></span><span class="meteor m3"></span>
  </div>
  <span class="mark">
    <svg viewBox="0 0 24 24">
      <defs><linearGradient id="gmk" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#fff6d8"/><stop offset="100%" stop-color="#ffd97a"/></linearGradient></defs>
      <g class="tr"><path d="M3 15 L15 3" stroke="url(#gmk)" stroke-width="2.6" stroke-linecap="round" fill="none"/></g>
      <circle cx="17" cy="5" r="2.6" fill="url(#gmk)"/>
      <circle cx="6.5" cy="18.5" r="1" fill="#fff" opacity=".7"/>
      <circle cx="20" cy="14" r=".8" fill="#fff" opacity=".5"/>
    </svg>
  </span>
  <span class="title">文档拾忆</span>
  <span class="divider"></span>
  <span class="subtitle">星辰超级智能体技能 · 按内容找回本机文件</span>
  <div class="right">
    <span class="chip">仅本机 127.0.0.1</span>
    <span class="chip">数据不出电脑</span>
  </div>
</div>

<div class="wrap">
  <!-- 待授权横幅：选「暂不」后显示，可随时再次授权 -->
  <div class="auth-banner" id="authBanner">
    <span class="ab-ic"><svg viewBox="0 0 24 24"><path d="M12 2l2.6 6.2 6.7.5-5.1 4.4 1.6 6.6L12 16.2 6.2 19.7l1.6-6.6-5.1-4.4 6.7-.5z"/></svg></span>
    <span class="ab-tx">
      <div class="t1">还未授权全盘扫描，暂时搜不到文件</div>
      <div class="t2">授权后即可按内容搜索本机文件，扫描约 10~30 分钟，数据全程留在本机</div>
    </span>
    <button class="ab-go" onclick="authNow()">现在授权扫描</button>
    <button class="ab-x" onclick="hideAuthBanner(event)" title="暂时隐藏">×</button>
  </div>
  <div class="panel">
    <div class="statusbar">
      <div class="stat"><svg class="ic" viewBox="0 0 24 24"><ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>已收录 <b id="stTotal">…</b></div>
      <div class="stat"><svg class="ic" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="15" y2="17"/></svg>提取正文 <b id="stText">…</b></div>
      <div class="stat"><svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>今天新增 <b id="stToday">…</b></div>
      <div class="stat">上次扫描 <b id="stScan" style="font-size:11px">…</b></div>
      <button class="scanbtn" id="scanBtn" onclick="startScan()"><svg class="ic" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.3"/><polyline points="21 3 21 9 15 9"/></svg>增量更新索引</button>
    </div>

    <div class="searchbox">
      <div class="kwwrap">
        <svg class="ic" viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><line x1="15.5" y1="15.5" x2="21" y2="21"/></svg>
        <input type="text" id="kw" placeholder="输入内容关键词，空格分隔多个词（如：液冷板 相变材料）" autofocus>
      </div>
      <button onclick="doSearch()"><svg class="ic" viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6.5"/><line x1="15.5" y1="15.5" x2="21" y2="21"/></svg>搜 索</button>
    </div>

    <div class="filters">
      <span class="seg-label">命中</span>
      <div class="seg"><span id="mAny" class="on" onclick="setMode('any')">任一</span><span id="mAll" onclick="setMode('all')">全部</span></div>
      <span class="seg-label">排序</span>
      <div class="seg"><span id="sScore" class="on" onclick="setSort('score')">按匹配度</span><span id="sTime" onclick="setSort('time')">按时间</span></div>
      <select id="fType" onchange="filterChanged()"><option value="">全部类型</option><option>docx</option><option>pdf</option>
        <option>pptx</option><option>xlsx</option><option>md</option><option>txt</option><option>py</option>
        <option value="jpg,jpeg,png,gif,webp,bmp,tiff,ico">图片</option></select>
      <select id="fLang" onchange="filterChanged()" title="按文件名是否含中文筛选"><option value="">中英文文件名</option>
        <option value="zh">中文名</option><option value="en">英文名</option></select>
      <select id="fNew" onchange="filterChanged()"><option value="">不限收录时间</option><option value="today">今天收录</option>
        <option value="yesterday">昨天收录</option><option value="7d">近7天收录</option></select>
      <select id="fWithin" onchange="filterChanged()"><option value="">不限修改时间</option><option value="1d">24小时内修改</option>
        <option value="7d">7天内修改</option><option value="30d">30天内修改</option></select>
      <input type="text" id="fDir" placeholder="路径包含…" style="width:105px" onkeydown="if(event.key==='Enter')filterChanged()" onfocus="this.dataset.prev=this.value" onblur="if(this.dataset.prev!==undefined&&this.dataset.prev!==this.value)filterChanged()">
      <select id="fLimit" onchange="filterChanged()"><option>20</option><option selected>50</option><option>100</option></select>
      <div class="draghint"><svg class="ic" viewBox="0 0 24 24"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8z"/></svg>
        删除文件：<b>勾选结果卡片 → 移入回收站</b>（送系统回收站，随时可还原）；也可把卡片拖给宠物暂存 24 小时，到期自动转入回收站</div>
    </div>
  </div>

  <div class="tabs">
    <span id="tabSearch" class="on" onclick="setTab('search')">检索</span>
    <span id="tabLib" onclick="setTab('lib')">资料库</span>
    <span id="tabDiscover" onclick="setTab('discover')">发现</span>
    <span id="tabSettings" onclick="setTab('settings')">设置</span>
  </div>

  <div id="paneSearch">
    <div class="recentbar" id="recentbar" style="display:none"></div>
    <div id="resultHead" class="result-head" style="display:none"></div>
    <div class="selbar" id="selbar" style="display:none"></div>
    <div class="yearbar" id="yearbar"></div>
    <div id="list"></div>
    <div id="empty" class="empty" style="display:none"></div>
  </div>

  <!-- 检索历史已并入「检索」页的最近搜索胶囊；完整历史在此弹窗查看 -->
  <div id="paneDiscover" style="display:none">
    <div class="panel" style="margin-top:0">
      <div class="db-sec">
        <h4><svg class="ic" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="17" rx="2.5"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="8" y1="2.5" x2="8" y2="5.5"/><line x1="16" y1="2.5" x2="16" y2="5.5"/><path d="M8.5 14.5 l2.5 2.5 4.5-4.5"/></svg>每日快照 <span class="sub">每天 00:00 定时扫描自动生成，记录当天新增与修改</span></h4>
        <div class="snapbar">
          <select id="snapDate" onchange="loadSnap()"></select>
          <span style="font-size:12px;color:var(--sub)">选择日期查看当天新增与修改的文件</span>
        </div>
        <div class="snap-content" id="snapContent">选择日期查看当天新增与修改的文件…</div>
      </div>
      <div class="db-sec">
        <h4><svg class="ic" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>疑似重复 TOP10 <span class="sub">同名同大小判定 · 未经哈希验证 · 建议打开确认内容后再清理</span></h4>
        <div id="dbDups"></div>
      </div>
      <div class="db-sec" style="margin-bottom:0">
        <h4><svg class="ic" viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8.5 7.5 L16 10.5 M9 16 L15.5 13.5"/></svg>找相似 <span class="sub">在检索结果卡片上点「找相似」：同目录邻居 + 全库文件名相似</span></h4>
        <div class="db-empty" style="display:flex;align-items:center;gap:8px;justify-content:flex-start">
          <span>去</span><span onclick="document.getElementById('kw').focus()" style="color:var(--pri);cursor:pointer;font-weight:600">搜索框</span>
          <span>搜个关键词，然后在结果卡片上点「找相似」，会列出当年一起保存的邻居文件～</span>
        </div>
      </div>
    </div>
  </div>

  <div id="paneLib" style="display:none">
    <div class="panel" style="margin-top:0">
      <div class="db-grid" id="dbCards"></div>
      <div class="db-sec">
        <h4><svg class="ic" viewBox="0 0 24 24"><ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5v13c0 1.7 3.6 3 8 3s8-1.3 8-3v-13"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>类型分布 <span class="sub">按收录数量 TOP10</span></h4>
        <div id="dbTypes"></div>
      </div>
      <div class="db-sec">
        <h4><svg class="ic" viewBox="0 0 24 24"><path d="M3 3v18h18"/><rect x="7" y="10" width="3" height="7"/><rect x="12" y="6" width="3" height="11"/><rect x="17" y="13" width="3" height="4"/></svg>录入趋势 <span class="sub">每日新增收录（最近14天）</span></h4>
        <div class="tr-chart" id="dbTrend"></div>
      </div>
      <div class="db-sec" style="margin-bottom:0">
        <h4><svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><circle cx="12" cy="16" r=".5"/></svg>索引与检索边界 <span class="sub">为什么有的东西搜不到</span></h4>
        <div class="bd-list">
          <div class="bd-row"><b>PDF</b><span>仅提取前 40 页正文（保证索引速度，绝大多数文档核心内容在前 40 页）</span></div>
          <div class="bd-row"><b>正文截断</b><span>文档类约前 1.2 万字符，代码/数据类约前 4 千字符（控制索引体积）</span></div>
          <div class="bd-row"><b>大文件</b><span>超过 20MB 只记文件名不提正文</span></div>
          <div class="bd-row"><b>图片 OCR</b><span>新扫描的图片（jpg/png 等，8MB 以内）会用 Windows 自带引擎本地识别图中文字，可直接按内容搜；首次全量扫描不做 OCR（存量图太多），视频/压缩包仍只能按文件名搜</span></div>
          <div class="bd-row"><b>纯二进制与依赖</b><span>dll/exe/enc/字体等 52 类扩展名与 Anaconda/node_modules 等 38 类依赖目录默认不收录（可在 config.json 调整）</span></div>
          <div class="bd-row"><b>刚保存的文件</b><span>搜索没结果时会自动做一次增量扫描（后台），完成后会提醒你再搜一次；也可手动点「增量更新索引」</span></div>
        </div>
      </div>
    </div>
  </div>

  <div id="paneSettings" style="display:none">
    <div class="panel" style="margin-top:0">
      <div class="db-sec">
        <h4><svg class="ic" viewBox="0 0 24 24"><path d="M12 2 20 5.5V11c0 5-3.4 8.8-8 11-4.6-2.2-8-6-8-11V5.5Z"/><path d="M8.5 11.5 l2.5 2.5 4.5-4.5"/></svg>隐私与数据边界</h4>
        <div class="bd-list">
          <div class="bd-row"><b>本机运行</b><span>界面只监听 127.0.0.1，本机之外谁也访问不到；不上传任何文件与数据</span></div>
          <div class="bd-row"><b>数据位置</b><span>索引、快照、配置全部保存在技能目录 scripts/data/ 下，删除该目录即彻底清除</span></div>
          <div class="bd-row"><b>唯一联网项</b><span>天气装扮查询，需要你手动授权且可拒绝；拒绝后完全不发任何网络请求</span></div>
          <div class="bd-row"><b>删除可还原</b><span>「移入回收站」送系统回收站随时可还原；宠物暂存到期也是转回收站，不直接删除</span></div>
        </div>
      </div>
      <div class="db-sec">
        <h4><svg class="ic" viewBox="0 0 24 24"><circle cx="9" cy="20" r="1.6"/><circle cx="18" cy="20" r="1.6"/><path d="M2 3h3l2.6 12.4a2 2 0 0 0 2 1.6h8.9a2 2 0 0 0 2-1.6L22 7H6"/></svg>个性化 <span class="sub">陪伴小宠物 · 当前积分 <b id="setPoints" style="color:var(--pri)">0</b></span></h4>
        <div class="set-actions">
          <button class="set-btn" onclick="openSignin()">📅 每日签到领积分</button>
          <button class="set-btn" onclick="openShop()">🐾 宠物商店买粮</button>
          <button class="set-btn" onclick="weatherAgain()">🌤 重新授权天气装扮</button>
          <button class="set-btn" onclick="createShortcut(false)">🖥 创建桌面快捷方式</button>
        </div>
        <div class="bd-list" style="margin-top:10px">
          <div class="bd-row"><b>宠物玩法</b><span>右下角小家伙可以点它互动、喂食表演；搜索结果卡片可以拖给它暂存（24 小时内双击肚子吐回原位，到期自动转入回收站）</span></div>
        </div>
      </div>
      <div class="db-sec">
        <h4><svg class="ic" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><path d="M17.5 14.5v6M14.5 17.5h6"/></svg>外部工具与反馈</h4>
        <div class="set-actions">
          <button class="set-btn fg-btn" onclick="openFileGuard()">🛡 打开文件卫士（新标签页）</button>
          <button class="set-btn" onclick="openFeedback()">💬 意见反馈</button>
        </div>
        <div class="bd-list" style="margin-top:10px">
          <div class="bd-row"><b>文件卫士</b><span>独立技能：病毒伪装 / 广告捆绑 / 破损文件检测与修复，隔离送回收站可还原；以独立窗口打开，不与检索混用</span></div>
          <div class="bd-row"><b>意见反馈</b><span>保存在本机 feedback.json，凌晨智能体自检时读取并针对性优化界面</span></div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ===== 检索历史弹窗（并入检索页胶囊 · 全部历史在此查看） ===== -->
<div class="pm-mask" id="histMask">
  <div class="pm" style="max-width:680px">
    <h3><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#2f6da8" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
      检索历史</h3>
    <div class="psub">最近 <b id="histCount">0</b> 条 · 意外关闭页面也不怕，一键重搜（最多保留 50 条）</div>
    <div class="hist-scroll" id="histList"></div>
    <div class="fb-btns" style="margin-top:14px">
      <button class="fb-cancel" onclick="clearHistory()">清空历史</button>
      <button class="fb-send" onclick="closeHist()">关闭</button>
    </div>
  </div>
</div>

<!-- ===== 移入回收站确认弹窗（影响摘要 · 可还原） ===== -->
<div class="pm-mask" id="recycleMask">
  <div class="pm" style="max-width:520px">
    <h3><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#c96a2e" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
      移入回收站确认</h3>
    <div class="psub" id="recycleSummary"></div>
    <div class="rc-list" id="recycleList"></div>
    <div class="rc-note">移入的是 <b>Windows 系统回收站</b>，随时可还原，不必担心误删。</div>
    <div class="fb-btns" style="margin-top:16px">
      <button class="fb-cancel" onclick="closeRecycleConfirm()">取消</button>
      <button class="fb-send" onclick="doRecycleConfirm()">确认移入</button>
    </div>
  </div>
</div>

<!-- ===== 反馈弹窗 ===== -->
<div class="fb-mask" id="fbMask">
  <div class="fb-modal" id="fbForm">
    <h3><svg class="ic" viewBox="0 0 24 24" style="width:17px;height:17px;color:var(--pri)"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8z"/></svg>意见反馈</h3>
    <div class="fsub">你的反馈会保存在本机，凌晨智能体自检时会读取识别并针对性优化界面。</div>
    <div class="fb-types" id="fbTypes">
      <span data-t="idea" onclick="fbSetType(this)">功能建议</span>
      <span data-t="ui" onclick="fbSetType(this)">界面问题</span>
      <span class="on" data-t="ux" onclick="fbSetType(this)">体验不好</span>
      <span data-t="bug" onclick="fbSetType(this)">疑似Bug</span>
      <span data-t="other" onclick="fbSetType(this)">其他</span>
    </div>
    <textarea id="fbText" placeholder="说说你的想法：哪里不顺手、想要什么功能、界面哪里需要改…（必填，最多 1000 字）"></textarea>
    <input class="fcontact" id="fbContact" placeholder="联系方式（选填，方便需要时进一步沟通）">
    <div class="fb-btns">
      <button class="fb-cancel" onclick="closeFeedback()">取消</button>
      <button class="fb-send" onclick="sendFeedback()">提交反馈</button>
    </div>
  </div>
  <div class="fb-modal" id="fbDone" style="display:none">
    <div class="fb-ok">
      <div class="okic"><svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg></div>
      <h4>反馈已记录！</h4>
      <p>谢谢你的意见～反馈保存在本机 feedback.json<br>凌晨智能体自检时会读取并针对性优化界面<br>也可以随时对 TeleAgent 说「看看界面反馈并优化」</p>
      <div class="fb-btns" style="margin-top:16px"><button class="fb-send" onclick="closeFeedback()">好的</button></div>
    </div>
  </div>
</div>

<!-- ===== 天气联网授权（每次进入界面询问一次） ===== -->
<div class="pm-mask" id="wAuthMask">
  <div class="pm" style="max-width:420px">
    <h3><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#2f6da8" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="5"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
      天气装扮 · 联网授权</h3>
    <div class="psub">宠物想知道你那边的天气，给你配上应景装扮（晴天小太阳 / 雨天撑伞 / 风天围脖 / 雪天耳包）。同意后<b>联网查询当地天气和温度</b>。</div>
    <div class="wauth-info">
      <div>· <b>建议填写你所在的城市</b>（如：郑州），按城市精确查询；
        留空则按 IP 自动定位——<b>开 VPN/代理时 IP 会漂移到境外，城市温度就不准了</b></div>
      <div>· 城市只保存在<b>本机</b>，不上传；填过之后每次打开都会自动带上，不再受 IP 漂移影响</div>
      <div>· 仅查询天气一项，不上传本机任何文件与数据；拒绝也不影响其他功能</div>
    </div>
    <input class="wcity-input" id="wCity" placeholder="你的城市（如：郑州）留空则自动定位" maxlength="30">
    <div class="fb-btns" style="margin-top:16px">
      <button class="fb-cancel" onclick="weatherDeny()">仅本机使用</button>
      <button class="fb-send" onclick="weatherAgree()">同意，查天气</button>
    </div>
  </div>
</div>

<!-- ===== 每日签到面板 ===== -->
<div class="pm-mask" id="signinMask">
  <div class="pm">
    <h3><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#2f6da8" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="17" rx="2.5"/><line x1="3" y1="9" x2="21" y2="9"/><path d="M8.5 14.5 l2.5 2.5 4.5-4.5"/></svg>
      每日签到</h3>
    <div class="psub">每天来签到领积分，连续签到加成更多；积分可以在宠物商店买粮食喂我～</div>
    <div class="pts-row">
      <div class="pts-card"><b id="sgPoints">0</b><span>我的积分</span></div>
      <div class="pts-card"><b id="sgStreak">0</b><span>连续签到（天）</span></div>
      <div class="pts-card"><b id="sgNext">+10</b><span>明日可得</span></div>
    </div>
    <div class="week-row" id="sgWeek"></div>
    <button class="sign-btn" id="sgBtn" onclick="doSignin()">签到领积分</button>
    <button class="pm-sub-btn" onclick="openShopFromSignin()">🐾 去宠物商店逛逛</button>
    <button class="pm-sub-btn" onclick="closeSignin()">关闭</button>
  </div>
</div>

<!-- ===== 宠物商店面板 ===== -->
<div class="pm-mask" id="shopMask">
  <div class="pm" style="max-width:560px">
    <h3><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#c9852e" stroke-width="2" stroke-linecap="round"><circle cx="9" cy="20" r="1.6"/><circle cx="18" cy="20" r="1.6"/><path d="M2 3h3l2.6 12.4a2 2 0 0 0 2 1.6h8.9a2 2 0 0 0 2-1.6L22 7H6"/></svg>
      宠物商店 · 粮食铺</h3>
    <div class="psub">用签到积分给我买粮食吧！不同粮食我会有不同的表情和表演哦～ <b id="shPoints" style="color:var(--pri)">0</b> 积分</div>
    <div class="shop-grid" id="shGrid"></div>
    <button class="pm-sub-btn" style="margin-top:12px" onclick="openSigninFromShop()">📅 签到领积分</button>
    <button class="pm-sub-btn" onclick="closeShop()">关闭</button>
  </div>
</div>

<!-- ===== 文件卫士已改为「设置 → 外部工具」独立窗口打开（评审建议：不内嵌 iframe） ===== -->

<!-- ===== 左下角回收站（连接系统回收站） ===== -->
<div class="recycle-zone" id="recycleZone" title="拖入重复文件送入系统回收站 · 双击打开回收站">
  <div class="rc-ic">
    <svg class="rc" viewBox="0 0 24 24">
      <polyline points="3 6 5 6 21 6"/>
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>
      <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
      <line x1="10" y1="11" x2="10" y2="17"/>
      <line x1="14" y1="11" x2="14" y2="17"/>
      <path d="M2.5 6.2 L4.8 9.5 M21.5 6.2 L19.2 9.5" stroke-width="1.2" opacity=".6"/>
    </svg>
    <span class="rc-badge" id="rcBadge">0</span>
  </div>
  <span class="rc-cap">回收站<br>拖入重复文件 · 双击打开</span>
</div>

<footer>
  引擎：TeleAgent 技能「文档拾忆」（本地 SQLite 索引 · 纯标准库 · 内置 pypdf） ·
  本界面仅监听 127.0.0.1，数据不出本机 · 命令行可用 <code>python searcher.py "关键词"</code> ·
  TeleAgent 对话直接说"帮我找讲 xx 的文件" · 勾选结果可移入回收站（可还原）
</footer>

<!-- ===== 宠物区 ===== -->
<div class="pet-zone" id="petZone">
  <div class="belly-panel" id="bellyPanel">
    <div class="bp-title"><svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><circle cx="12" cy="16" r=".5"/></svg>
      宠物的肚子 <span class="cnt" id="bpCount">0</span>/99</div>
    <div class="bp-sub">吃进去的文件暂存中；<b>双击宠物肚子</b>可全部吐出恢复原位。到期自动转入系统回收站（仍可还原）</div>
    <div class="bp-list" id="bpList"></div>
    <button class="bp-btn" onclick="spitAll()">全部吐出</button>
  </div>

  <div class="bubble" id="bubble"></div>
  <span class="belly-badge" id="bellyBadge" onclick="toggleBellyPanel(event)" title="点开肚子清单，双击宠物肚子可吐出文件">0</span>

  <!-- 天气层（装扮） -->
  <div class="sun-ic" id="sunIc"><div class="rays">
    <div class="ray"></div><div class="ray" style="transform:rotate(45deg)"></div>
    <div class="ray" style="transform:rotate(90deg)"></div><div class="ray" style="transform:rotate(135deg)"></div>
    <div class="ray" style="transform:rotate(180deg)"></div><div class="ray" style="transform:rotate(225deg)"></div>
    <div class="ray" style="transform:rotate(270deg)"></div><div class="ray" style="transform:rotate(315deg)"></div>
  </div><div class="core"></div></div>
  <div class="cloudy"><div class="cl"></div><div class="cl"></div><div class="cl"></div><div class="clbase"></div></div>
  <div class="rain-box" id="rainBox"></div>
  <div class="snow-box" id="snowBox"></div>
  <div class="leaf-box" id="leafBox"></div>
  <span class="wchip" id="wChip"></span>

  <div class="pet welcome" id="pet" onclick="petTap()" title="点我互动 · 拖文件给我吃">
    <svg viewBox="0 0 150 152">
      <defs>
        <radialGradient id="gBody" cx="42%" cy="30%" r="80%">
          <stop offset="0%" stop-color="#ffd9a0"/><stop offset="55%" stop-color="#f4bd75"/><stop offset="100%" stop-color="#e59f4b"/>
        </radialGradient>
        <radialGradient id="gBelly" cx="45%" cy="32%" r="85%">
          <stop offset="0%" stop-color="#fffaf0"/><stop offset="100%" stop-color="#f3e3c0"/>
        </radialGradient>
      </defs>

      <!-- 尾巴 -->
      <g class="tail-g">
        <path d="M104 115 Q136 107 128 79" stroke="#e8a44c" stroke-width="11" fill="none" stroke-linecap="round"/>
        <path d="M128 79 Q126 72 130 70" stroke="#f6d9a6" stroke-width="8" fill="none" stroke-linecap="round"/>
      </g>

      <!-- 身体（头身直接相连，紧凑造型） -->
      <g class="body-g">
        <ellipse cx="75" cy="115" rx="34" ry="23" fill="url(#gBody)"/>
        <ellipse class="belly-e" cx="75" cy="119" rx="22" ry="14" fill="url(#gBelly)"/>
        <!-- 脚 -->
        <ellipse cx="57" cy="137" rx="10" ry="5.5" fill="#f2c17e"/>
        <ellipse cx="93" cy="137" rx="10" ry="5.5" fill="#f2c17e"/>
        <path d="M54 137 l1.5 -3 M58 137 l0 -3.5 M62 137 l-1.5 -3" stroke="#d99a45" stroke-width="1.4" stroke-linecap="round" fill="none"/>
        <path d="M90 137 l1.5 -3 M94 137 l0 -3.5 M98 137 l-1.5 -3" stroke="#d99a45" stroke-width="1.4" stroke-linecap="round" fill="none"/>
        <!-- 左手 -->
        <ellipse cx="44" cy="104" rx="8" ry="13" fill="url(#gBody)" transform="rotate(20 44 104)"/>
        <!-- 右手（打招呼/挥手） -->
        <ellipse class="arm-r" cx="108" cy="98" rx="7.5" ry="12" fill="url(#gBody)"/>
        <!-- 穿戴：雨靴（雨天） -->
        <g class="wear wear-boots">
          <path d="M47 128 q-3 10 2 12 h16 q4-1 2-12 z" fill="#3f6fa8"/>
          <path d="M83 128 q-3 10 2 12 h16 q4-1 2-12 z" fill="#3f6fa8"/>
          <ellipse cx="57" cy="140" rx="12" ry="4" fill="#31578a"/>
          <ellipse cx="93" cy="140" rx="12" ry="4" fill="#31578a"/>
        </g>
        <!-- 穿戴：围脖（刮风，贴近毛色的深橘） -->
        <g class="wear wear-scarf">
          <path d="M52 92 Q75 102 98 92 Q98 101 75 106 Q52 101 52 92 Z" fill="#e8935a" stroke="#d97f3e" stroke-width="1.5"/>
          <path d="M88 98 q7 8 5 15 q-6 2-9-2 q1-7-2-11 z" fill="#e8935a" stroke="#d97f3e" stroke-width="1.2"/>
        </g>
        <!-- 穿戴：羽绒服（下雪，白色厚边） -->
        <g class="wear wear-coat">
          <ellipse cx="75" cy="115" rx="38" ry="26" fill="none" stroke="#f4f8fc" stroke-width="9"/>
          <ellipse cx="75" cy="115" rx="38" ry="26" fill="none" stroke="#c9dcec" stroke-width="2"/>
          <circle cx="62" cy="104" r="2" fill="#c9dcec"/><circle cx="75" cy="101" r="2" fill="#c9dcec"/><circle cx="88" cy="104" r="2" fill="#c9dcec"/>
        </g>
      </g>

      <!-- 头（与身体直接相连，无缝衔接） -->
      <g class="head-g">
        <!-- 耳朵 -->
        <g class="ear-l"><path d="M52 45 L42 17 L70 32 Z" fill="#e8a44c"/><path d="M54 40 L48 24 L64 34 Z" fill="#f2c9c0"/></g>
        <g class="ear-r"><path d="M98 45 L108 17 L80 32 Z" fill="#e8a44c"/><path d="M96 40 L102 24 L86 34 Z" fill="#f2c9c0"/></g>
        <circle cx="75" cy="63" r="31" fill="url(#gBody)"/>
        <!-- 腮红 -->
        <ellipse cx="52" cy="76" rx="6.5" ry="4" fill="#f6a894" opacity=".8"/>
        <ellipse cx="98" cy="76" rx="6.5" ry="4" fill="#f6a894" opacity=".8"/>
        <!-- 胡须 -->
        <path d="M40 65 h-9 M40 71 l-9 3 M110 65 h9 M110 71 l9 3" stroke="#d99a45" stroke-width="1.4" stroke-linecap="round" fill="none"/>
        <!-- Zzz（睡着） -->
        <g class="zzz">
          <text x="108" y="20">Z</text><text x="118" y="12">Z</text><text x="126" y="5">Z</text>
        </g>
        <!-- 眼睛：普通 -->
        <g class="eye-n"><circle cx="62" cy="59" r="4.2" fill="#35322e"/><circle cx="88" cy="59" r="4.2" fill="#35322e"/>
          <circle cx="63.6" cy="57.4" r="1.5" fill="#fff"/><circle cx="89.6" cy="57.4" r="1.5" fill="#fff"/></g>
        <!-- 眼睛：开心弯月 -->
        <g class="eye-h"><path d="M55 60 Q62 52 69 60" stroke="#35322e" stroke-width="3.2" fill="none" stroke-linecap="round"/>
          <path d="M81 60 Q88 52 95 60" stroke="#35322e" stroke-width="3.2" fill="none" stroke-linecap="round"/></g>
        <!-- 眼睛：睡着闭眼 -->
        <g class="eye-c"><path d="M56 58 Q62 63 68 58" stroke="#35322e" stroke-width="3" fill="none" stroke-linecap="round"/>
          <path d="M82 58 Q88 63 94 58" stroke="#35322e" stroke-width="3" fill="none" stroke-linecap="round"/></g>
        <!-- 难过眉毛 -->
        <g class="brow-s"><path d="M55 50 l12 3" stroke="#35322e" stroke-width="2.4" stroke-linecap="round" fill="none"/>
          <path d="M95 50 l-12 3" stroke="#35322e" stroke-width="2.4" stroke-linecap="round" fill="none"/></g>
        <!-- 口鼻 -->
        <ellipse cx="75" cy="74" rx="16" ry="11" fill="url(#gBelly)"/>
        <ellipse cx="75" cy="69" rx="4.6" ry="3.3" fill="#6b4f2e"/>
        <!-- 嘴：常态微笑 / 难过 / 开心笑（弯月+小舌） / 温和大笑（吃） -->
        <path class="mouth-n" d="M67 76 Q75 82 83 76" stroke="#6b4f2e" stroke-width="2.5" fill="none" stroke-linecap="round"/>
        <path class="mouth-s" d="M67 80 Q75 74 83 80" stroke="#6b4f2e" stroke-width="2.5" fill="none" stroke-linecap="round"/>
        <g class="mouth-o">
          <path d="M65 74 Q75 83.5 85 74" stroke="#6b4f2e" stroke-width="2.8" fill="none" stroke-linecap="round"/>
          <path d="M71 78 Q75 81.5 79 78 Q75 79.5 71 78 Z" fill="#e8968a"/>
        </g>
        <!-- 张圆形大嘴：仅限吃文件 / 吐文件两个瞬间 -->
        <g class="mouth-big">
          <ellipse cx="75" cy="78" rx="13" ry="11" fill="#a3554a"/>
          <ellipse cx="75" cy="83.5" rx="8.5" ry="5" fill="#e8887a"/>
          <ellipse cx="75" cy="70.5" rx="7" ry="3" fill="#fff" opacity=".92"/>
        </g>
        <!-- 思考点 -->
        <g class="think-g" fill="#3b82c4">
          <circle cx="122" cy="27" r="3.6"/><circle cx="131" cy="19" r="2.8"/><circle cx="138" cy="13" r="2.1"/>
        </g>
        <!-- 穿戴：耳包（下雪，最后绘制确保盖住耳朵） -->
        <g class="wear wear-earmuffs">
          <path d="M50 34 Q75 16 100 34" stroke="#f4ede2" stroke-width="4" fill="none" stroke-linecap="round"/>
          <ellipse cx="50" cy="33" rx="10.5" ry="8" fill="#f7f2e9" stroke="#d9c9b4" stroke-width="1.5"/>
          <ellipse cx="100" cy="33" rx="10.5" ry="8" fill="#f7f2e9" stroke="#d9c9b4" stroke-width="1.5"/>
        </g>
      </g>

      <!-- 肚子热区（双击吐出） -->
      <circle id="bellyHot" cx="75" cy="119" r="27" fill="transparent" style="cursor:pointer" pointer-events="all"/>

      <!-- 穿戴：雨伞（雨天，举在右上方） -->
      <g class="wear wear-umbrella">
        <path d="M68 -12 Q99 -34 130 -12 Q128 -8 121 -9 Q118 -4 111 -6 Q107 -1 99 -3 Q95 2 88 -1 Q82 3 75 0 Q70 4 68 -12 Z" fill="#4a86c2" opacity=".95"/>
        <path d="M68 -12 Q99 -34 130 -12" stroke="#35659e" stroke-width="2.5" fill="none"/>
        <line x1="99" y1="-10" x2="99" y2="86" stroke="#8a5a3a" stroke-width="3.5" stroke-linecap="round"/>
        <path d="M99 86 q0 7 6 7 q5 0 5-5" stroke="#8a5a3a" stroke-width="3.5" fill="none" stroke-linecap="round"/>
      </g>

      <!-- 爱心（豪华大餐） -->
      <g class="heart-fx" fill="#ef7d9d">
        <path d="M30 30 c-2-6-10-6-10 0 c0 4 6 7 10 10 c4-3 10-6 10-10 c0-6-8-6-10 0z" transform="scale(.9)"/>
        <path d="M118 26 c-2-6-10-6-10 0 c0 4 6 7 10 10 c4-3 10-6 10-10 c0-6-8-6-10 0z" transform="scale(.75)"/>
      </g>
    </svg>
  </div>
</div>

<div class="mask" id="mask">
  <div class="modal">
    <h3 id="mTitle"></h3>
    <pre id="mBody"></pre>
    <div class="close"><button class="scanbtn" style="margin-left:0" onclick="hideModal()">知道了</button></div>
  </div>
</div>

<!-- 相似文件弹窗（找相似） -->
<div class="mask" id="simMask">
  <div class="modal" style="max-width:560px">
    <h3 id="simTitle">相似文件</h3>
    <pre id="simSub" style="max-height:70px"></pre>
    <div id="simList" style="max-height:52vh;overflow:auto"></div>
    <div class="close"><button class="scanbtn" style="margin-left:0" onclick="closeSim()">关闭</button></div>
  </div>
</div>

<!-- 首次使用：授权全盘扫描 -->
<div class="mask" id="welcomeMask">
  <div class="modal welcome">
    <div class="wpet">
      <svg viewBox="0 0 150 152">
        <g>
          <path d="M104 115 Q136 107 128 79" stroke="#e8a44c" stroke-width="11" fill="none" stroke-linecap="round"/>
          <ellipse cx="75" cy="115" rx="34" ry="23" fill="#f4bd75"/>
          <ellipse cx="75" cy="119" rx="22" ry="14" fill="#fdf3e0"/>
          <ellipse cx="57" cy="137" rx="10" ry="5.5" fill="#f2c17e"/>
          <ellipse cx="93" cy="137" rx="10" ry="5.5" fill="#f2c17e"/>
          <path d="M52 45 L42 17 L70 32 Z" fill="#e8a44c"/>
          <path d="M98 45 L108 17 L80 32 Z" fill="#e8a44c"/>
          <circle cx="75" cy="63" r="31" fill="#f4bd75"/>
          <ellipse cx="52" cy="76" rx="6.5" ry="4" fill="#f6a894" opacity=".8"/>
          <ellipse cx="98" cy="76" rx="6.5" ry="4" fill="#f6a894" opacity=".8"/>
          <path d="M55 60 Q62 52 69 60" stroke="#35322e" stroke-width="3.2" fill="none" stroke-linecap="round"/>
          <path d="M81 60 Q88 52 95 60" stroke="#35322e" stroke-width="3.2" fill="none" stroke-linecap="round"/>
          <ellipse cx="75" cy="74" rx="16" ry="11" fill="#fdf3e0"/>
          <ellipse cx="75" cy="69" rx="4.6" ry="3.3" fill="#6b4f2e"/>
          <path d="M67 76 Q75 82 83 76" stroke="#6b4f2e" stroke-width="2.5" fill="none" stroke-linecap="round"/>
        </g>
      </svg>
    </div>
    <h3>欢迎使用文档拾忆！</h3>
    <div class="winfo">
      首次使用需要对本机文件做一次<b>全盘识别</b>（建立内容索引）：<br>
      · 扫描范围：文档 / 桌面 / 下载 / 微信QQ文件夹 及非C盘本地磁盘<br>
      · 所需时间：视文件数量约 10~30 分钟，期间可最小化窗口做别的<br>
      · 数据安全：索引全程只保存在<b>本机</b>，不联网、不上传
    </div>
    <div class="wask">是否授权现在开始全盘扫描识别？</div>
    <label class="wshort" for="wShortcut">
      <input type="checkbox" id="wShortcut">
      <span class="wt"><b>在桌面创建「文档拾忆」快捷方式</b>（可选）
      <span class="desc">透明底图标·双击零窗口直达界面。默认不创建，不动你的桌面；已有同名或手动建的图标也不会重复</span></span>
    </label>
    <div class="wbtns">
      <button class="wyes" onclick="firstUseScan(true)">授权并开始扫描</button>
      <button class="wno" onclick="firstUseScan(false)">暂不，先逛逛界面</button>
    </div>
  </div>
</div>

<script>
var mode='any', sort='score', tab='search', yearSel=null;
var window__results=[];

/* ---------- 宠物 ---------- */
var petEl=document.getElementById('pet'), bubbleEl=document.getElementById('bubble');
var petZone=document.getElementById('petZone');
var bellyBadge=document.getElementById('bellyBadge'), bellyPanel=document.getElementById('bellyPanel');
var bubbleTimer=null, thinkingTimer=null;
var L={
  welcome:['你好呀！我是你的找文件小助手，找不到文件尽管问我！','欢迎回来～今天想找回什么文件？','嗨！只记得内容不记得文件名？交给我！'],
  thinking:['正在翻箱倒柜找找看…','嗯…让我想想放哪了…','别急别急，马上就好～'],
  thinkingDeep:['嗯…这个文件藏得有点深，让我再想想…','翻了好几个文件夹了，再等等…'],
  happy:['找到啦！一共 {n} 个命中，都在下面列表里！','搞定！共 {n} 个文件，点路径一键直达～','耶！全部帮你揪出来了，共 {n} 个！'],
  sad:['呜…没找到…换个短一点的关键词试试？','这个真没有…也许文件还没入索引？点右上角更新一下～'],
  hungry:['饿饿！把文件喂给我吧，我帮你看住它～','张嘴等投喂中…松手我就吃！','要吃这个吗？一天之内随时吐还给你！'],
  eatOk:['嗝～主人我吃饱了！','唔姆唔姆…嗝！这个文件我先存肚子里啦～','啊呜…嗝！24小时内想拿回去就拍拍我肚子～'],
  bellyNearFull:['呜…肚子快装不下了，还剩 {n} 个位置～','只剩 {n} 个位置啦，别再猛喂了嘛～'],
  bellyFull:'肚子撑爆啦！最多装 99 个，先双击我肚子吐出一些吧～',
  lastSearch:['上次你在找「{kw}」，去「检索历史」能一键重搜哦～','想继续上次的「{kw}」？检索历史里点一下就行！'],
  spitEmpty:'肚子里空空的，还没吃过东西呢～',
  spitDone:'都吐出来啦！文件已经回到原来的位置～',
  idle:['我一直在这帮你守着文件哦～','忘了文件名没关系，记得内容就行！','每天凌晨 00:00 我都会自动做一次快照～','吃掉的文件 24 小时内可以拍拍我肚子拿回，到期也只是进回收站','关掉页面也不怕，检索历史帮你记着呢～','有什么想找的，尽管搜！'],
  tap:['汪！我在呢～','有我在，文件跑不掉～','要不要摸摸头？','汪汪！快去搜点什么吧～']
};
function pick(a){return a[Math.floor(Math.random()*a.length)];}
function petSay(txt,ms){
  bubbleEl.textContent=txt; bubbleEl.classList.add('show');
  clearTimeout(bubbleTimer);
  bubbleTimer=setTimeout(function(){bubbleEl.classList.remove('show');},ms||4200);
}
function setPet(mood){
  petEl.className='pet '+mood;
  if(mood==='welcome')petSay(pick(L.welcome),4600);
  if(mood==='thinking')petSay(pick(L.thinking),8000);
  if(mood==='sad')petSay(pick(L.sad),5200);
}
function petBackToIdle(){
  if(petEl.className.indexOf('happy')>-1||petEl.className.indexOf('sad')>-1||
     petEl.className.indexOf('full')>-1||petEl.className.indexOf('welcome')>-1||
     petEl.className.indexOf('sleep')>-1){petEl.className='pet idle';}
}
function petTap(){
  if(sleepStage>0){return;}
  petSay(pick(L.tap));
  petEl.classList.add('happy');
  setTimeout(function(){petEl.classList.remove('happy');},1200);
}
setInterval(function(){
  if(sleepStage===0&&petEl.className==='pet idle'&&Math.random()<0.55)petSay(pick(L.idle));
},17000);

/* ---------- 工具 ---------- */
function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function hi(s){return esc(s).replace(/【(.+?)】/g,'<mark>$1</mark>');}
function fmtSize(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';return (n/1048576).toFixed(1)+' MB';}
function fmtTime(t){var d=new Date(t*1000),p=function(x){return String(x).padStart(2,'0');};
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function fmtShortTime(t){var d=new Date(t*1000),p=function(x){return String(x).padStart(2,'0');};
  return p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function showModal(t,b){document.getElementById('mTitle').textContent=t;
  document.getElementById('mBody').textContent=b;document.getElementById('mask').classList.add('show');}
function hideModal(){document.getElementById('mask').classList.remove('show');}
function markDone(btn,txt){btn.classList.add('done');var old=btn.innerHTML;btn.innerHTML=txt;
  setTimeout(function(){btn.classList.remove('done');btn.innerHTML=old;},1400);}

/* ---------- 状态 ---------- */
function loadStatus(){
  fetch('/api/status').then(function(r){return r.json()}).then(function(s){
    if(s.first_use){
      document.getElementById('stTotal').textContent='未建索引';
      document.getElementById('welcomeMask').classList.add('show');
      petSay('第一次见面！先授权我认识一下你电脑里的文件吧～',6000);
      return;
    }
    if(!s.indexed){
      document.getElementById('stTotal').textContent='索引为空';
      showModal('索引为空','点右上角「增量更新索引」重新建立索引（首次全盘扫描需要一段时间）。');
      return;
    }
    document.getElementById('authBanner').classList.remove('show');
    document.getElementById('stTotal').textContent=s.total;
    if(s.busy){  // 首次扫描写入高峰：数字暂不可读，下一轮轮询自会恢复
      document.getElementById('stScan').textContent='扫描进行中，索引数字稍后刷新';
      return;
    }
    document.getElementById('stText').textContent=s.with_text;
    document.getElementById('stToday').textContent=s.today_new;
    document.getElementById('stScan').textContent=(s.last_scan||'-')+' · '+s.db_mb+'MB';
  }).catch(function(){});
}
function firstUseScan(yes){
  document.getElementById('welcomeMask').classList.remove('show');
  if(yes){
    var cb=document.getElementById('wShortcut');
    if(cb&&cb.checked)createShortcut(true);
    petSay('收到授权！我开始翻遍全盘认识文件啦，你可以先去忙别的～',6000);
    startScan(true);
  }else{
    document.getElementById('authBanner').classList.add('show');
    petSay('好的～想搜索时点上方「现在授权扫描」就好，我随时待命！',6000);
  }
}
/* 顶部横幅：再次授权 */
function authNow(){
  document.getElementById('authBanner').classList.remove('show');
  petSay('收到授权！我开始翻遍全盘认识文件啦～',6000);
  startScan(true);
}
function hideAuthBanner(ev){
  ev.stopPropagation();
  document.getElementById('authBanner').classList.remove('show');
  petSay('好的～需要时再叫我，顶部提示会一直留到授权完成哦',5000);
}
/* 创建桌面快捷方式（勾选时调用，也可后续手动） */
function createShortcut(silent){
  fetch('/api/shortcut',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({})})
    .then(function(r){return r.json()}).then(function(s){
      if(!s.ok){if(!silent)showModal('创建失败',s.error||'未知错误');return;}
      if(silent){
        petSay(s.existed?'桌面已经有直达这里的快捷方式啦（'+(s.name||'文档拾忆')+'），不再重复创建～':'桌面快捷方式已创建！透明底图标双击直达～',5600);
      }else{
        showModal('桌面快捷方式',s.existed
          ?('桌面已有指向本程序的快捷方式（'+(s.name||'文档拾忆')+'），不再重复创建。\n\n也可在桌面右键它 → 重命名为你喜欢的名字。')
          :('已在桌面创建「文档拾忆」快捷方式：\n'+s.path+'\n\n透明底图标 · 双击零窗口直达界面。'));
      }
    }).catch(function(e){if(!silent)showModal('请求失败',String(e));});
}

/* ---------- 搜索 ---------- */
function setMode(m){mode=m;
  document.getElementById('mAny').className=(m==='any')?'on':'';
  document.getElementById('mAll').className=(m==='all')?'on':'';
  if(document.getElementById('kw').value.trim())doSearch();}
function setSort(s){sort=s;
  document.getElementById('sTime').className=(s==='time')?'on':'';
  document.getElementById('sScore').className=(s==='score')?'on':'';
  if(document.getElementById('kw').value.trim())doSearch();}
/* 过滤条件变更（类型/收录时间/修改时间/上限/路径）：立即自动重搜，无需再点搜索键 */
function filterChanged(){
  if(document.getElementById('kw').value.trim())doSearch();
  else if(document.getElementById('resultHead').style.display!=='none'){
    /* 还没输入关键词时提示一次 */
    petSay('先输入内容关键词，再选过滤条件会立即生效哦～',4200);
  }
}

function doSearch(){
  var kw=document.getElementById('kw').value.trim();
  if(!kw){petSay('输入内容关键词才能找哦～');return;}
  var sbtn=document.querySelector('.searchbox button');
  sbtn.classList.add('searching');
  setPet('thinking');
  clearTimeout(thinkingTimer);
  thinkingTimer=setTimeout(function(){
    if(petEl.className==='pet thinking')petSay(pick(L.thinkingDeep),9000);
  },2600);
  var q=new URLSearchParams();
  kw.split(/\s+/).forEach(function(k){q.append('kw',k);});
  q.set('mode',mode);q.set('sort',sort);
  if(yearSel)q.set('year',yearSel);
  var t=document.getElementById('fType').value;if(t)q.set('type',t);
  var n=document.getElementById('fNew').value;if(n)q.set('new',n);
  var w=document.getElementById('fWithin').value;if(w)q.set('within',w);
  var d=document.getElementById('fDir').value.trim();if(d)q.set('dir',d);
  var lg=document.getElementById('fLang').value;if(lg)q.set('lang',lg);
  if(yearSel)q.set('year',yearSel);
  q.set('limit',document.getElementById('fLimit').value);
  var rh=document.getElementById('resultHead');
  rh.style.display='';rh.innerHTML='检索中…';
  fetch('/api/search?'+q.toString()).then(function(r){return r.json()}).then(function(out){
    clearTimeout(thinkingTimer);
    document.querySelector('.searchbox button').classList.remove('searching');
    if(out.error){rh.innerHTML='';document.getElementById('list').innerHTML='';
      document.getElementById('empty').style.display='none';showModal('提示',out.error);
      setPet('sad');return;}
    rh.innerHTML='按「<b>'+esc(out.keywords.join(' '))+'</b>」（'+(out.mode==='all'?'全部命中':'任一命中')+
      ' · '+(out.sort==='time'?'按时间排序':'按匹配度排序')+
      (yearSel?(' · <b>'+yearSel+' 年</b>'):'')+'）找到 <b>'+out.total_hits+'</b> 个文件，显示前 '+out.returned+' 个：';
    renderResults(out.results, out.scan_pending);
    renderYearbar(out.by_year||[]);
    if(out.total_hits>0){setPet('happy');petSay(pick(L.happy).replace('{n}',out.total_hits),5000);}
    else{setPet('sad');
      if(out.scan_pending)petSay('没找到…可能是刚保存的文件还没进索引，我已经在后台增量扫描啦，完成后会提醒你，到时再搜一次就能找到！',8000);}
    setTimeout(petBackToIdle,5600);
    loadHistory(false);
    loadRecentKws();
  }).catch(function(e){clearTimeout(thinkingTimer);
    document.querySelector('.searchbox button').classList.remove('searching');
    showModal('请求失败',String(e));
    petEl.className='pet idle';});
}

function renderResults(rs, scanPending){
  window__results=rs;
  selSet={};updateSelbar();
  var list=document.getElementById('list'), empty=document.getElementById('empty');
  if(!rs.length){list.innerHTML='';empty.style.display='';
    empty.innerHTML='<div class="meteoric"><svg viewBox="0 0 64 64"><defs><linearGradient id="gem" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#e8f4fd"/><stop offset="100%" stop-color="#a8c9e6"/></linearGradient></defs><g class="tr"><path d="M10 40 L40 10" stroke="url(#gem)" stroke-width="4" stroke-linecap="round" fill="none"/></g><circle cx="44" cy="14" r="4.5" fill="url(#gem)"/><circle cx="24" cy="46" r="1.6" fill="#a8c9e6"/><circle cx="52" cy="34" r="1.2" fill="#a8c9e6"/><circle cx="14" cy="24" r="1" fill="#c6dcee"/></svg></div>'+
      '<div class="e-title">没有找到相关文件</div>'+
      '<div class="e-tips">'+
      (scanPending
        ?'<div class="e-tip"><b>可能刚保存的文件还没进索引</b>——已自动开始增量扫描（后台运行，完成后自动提示），<b>稍后再搜一次就能找到</b></div>'
        :'<div class="e-tip"><b>刚保存的文件还没进索引？</b> 点 <a href="javascript:void(0)" onclick="startScan()">立即增量扫描</a>（后台运行，完成后自动提示，再搜一次就能找到）</div>')+
      '<div class="e-tip"><b>找的是图片？</b> 新扫描的图片会自动识别图中的文字，可直接按内容搜；老图片与视频/压缩包只能按<b>文件名</b>搜索（类型筛选可选「图片」）</div>'+
      '<div class="e-tip"><b>找的是图片 / 视频 / 压缩包？</b> 这类文件没有可提取的正文，只能按<b>文件名</b>搜索——换成文件名里的词试试</div>'+
      '<div class="e-tip"><b>纯二进制与依赖不收录</b> dll / exe / 安装包 / 字体，以及 Anaconda、node_modules 等依赖目录默认不进索引</div>'+
      '<div class="e-tip"><b>关键词太具体？</b> 换更短或更宽泛的词、点「任一命中」、清空多余的筛选条件（类型/时间/路径）</div>'+
      '</div>'+
      '<div class="e-more"><a href="javascript:void(0)" onclick="setTab(\'settings\')">查看检索能力边界说明 →</a></div>';
    return;}
  empty.style.display='none';
  list.innerHTML=rs.map(function(r,i){
    var sn=(r.snippets||[]).map(function(s){return '<div class="snip">'+hi(s)+'</div>';}).join('');
    var dup='';
    if(r.dup_count&&r.dups){
      var items=r.dups.map(function(d){
        return '<div class="dup-item" draggable="true" ondragstart="dupDragStart(event,this)" data-p="'+encodeURIComponent(d.path)+'">'+
          '<span class="dpath">'+esc(d.path)+'</span>'+
          '<span class="dtime">'+fmtTime(d.mtime)+'</span>'+
          '<button class="act" onclick="openP(this.dataset.p,\'folder\',this)" data-p="'+encodeURIComponent(d.path)+'"><svg class="ic" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>定位</button>'+
          '<button class="act" onclick="recycleOne(this)" data-p="'+encodeURIComponent(d.path)+'" data-n="'+esc(r.name)+'"><svg class="ic" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>回收</button></div>';
      }).join('');
      dup='<div class="dupbox"><div class="dup-head"><svg class="ic" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>'+
        '发现 <b>'+r.dup_count+'</b> 个重复副本（同名同大小 · 未经哈希验证）<span class="tip">建议先打开确认内容再回收</span></div>'+items+'</div>';
    }
    return '<div class="result" id="rc-'+i+'" data-t="'+esc(r.ext||'')+'" draggable="true" ondragstart="dragStart(event,'+i+')">'+
    '<div class="rtop"><label class="ck"><input type="checkbox" id="ck-'+i+'" onclick="toggleSel('+i+',event)" title="勾选后可批量移入回收站"></label>'+
    '<span class="rank">'+(i+1)+'</span>'+
    '<span class="fname">'+esc(r.name)+'</span>'+
    (r.dup_count?'<span class="dup-badge">重复 ×'+r.dup_count+'</span>':'')+
    '<span class="tag">'+esc((r.ext||'').replace('.','').toUpperCase())+'</span>'+
    '<span class="score">匹配度 '+r.pct+'%</span></div>'+
    '<div class="meta"><span><svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg> '+fmtTime(r.mtime)+'</span>'+
    '<span><svg class="ic" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/></svg> '+fmtSize(r.size)+'</span>'+
    '<span>首次记录 '+(r.first_seen||'-')+'</span><span>'+(r.status==='text'?'已提取正文':'仅文件名')+'</span></div>'+
    '<div class="oprow"><div class="path">'+esc(r.path)+'</div>'+
    '<button class="act" onclick="openP(\''+encodeURIComponent(r.path)+'\',\'folder\',this)"><svg class="ic" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>所在文件夹</button>'+
    '<button class="act" onclick="openP(\''+encodeURIComponent(r.path)+'\',\'file\',this)"><svg class="ic" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>打开文件</button>'+
    '<button class="act sim" onclick="showSimilar(\''+encodeURIComponent(r.path)+'\',\''+esc(r.name).replace(/'/g,'')+'\')"><svg class="ic" viewBox="0 0 24 24"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8.5 7.5 L16 10.5 M9 16 L15.5 13.5"/></svg>找相似</button>'+
    '<button class="act ai" onclick="aiSummarize(this,\''+encodeURIComponent(r.path)+'\')"><svg class="ic" viewBox="0 0 24 24"><path d="M12 3l1.9 5.6L19.5 10l-5.6 1.9L12 17.5l-1.9-5.6L4.5 10l5.6-1.4z"/><path d="M19 15l.9 2.6L22.5 18l-2.6.9L19 21.5l-.9-2.6L15.5 18l2.6-.9z"/></svg>AI 总结</button>'+
    '<button class="act" onclick="copyP(\''+encodeURIComponent(r.path)+'\',this)"><svg class="ic" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>复制路径</button></div>'+sn+dup+'</div>';
  }).join('');
}

/* ---------- 勾选删除（评审主流程）：勾选 → 影响摘要 → 确认 → 移入回收站（可还原） ---------- */
var selSet={};
function toggleSel(i,ev){
  ev.stopPropagation();
  var r=window__results[i];if(!r)return;
  if(selSet[r.path])delete selSet[r.path];else selSet[r.path]={name:r.name,size:r.size};
  var card=document.getElementById('rc-'+i);
  if(card)card.classList.toggle('sel-on',!!selSet[r.path]);
  updateSelbar();
}
function selAll(){
  var rs=window__results||[];
  var keys=Object.keys(selSet);
  if(keys.length>=rs.length){selSet={};}
  else{rs.forEach(function(r){selSet[r.path]={name:r.name,size:r.size};});}
  rs.forEach(function(r,i){
    var c=document.getElementById('ck-'+i);if(c)c.checked=!!selSet[r.path];
    var card=document.getElementById('rc-'+i);if(card)card.classList.toggle('sel-on',!!selSet[r.path]);
  });
  updateSelbar();
}
function updateSelbar(){
  var bar=document.getElementById('selbar');
  var keys=Object.keys(selSet);
  var rs=window__results||[];
  if(!keys.length||!rs.length){bar.style.display='none';return;}
  var total=0;keys.forEach(function(k){total+=(selSet[k].size||0);});
  bar.style.display='flex';
  bar.innerHTML='<label class="sb-all"><input type="checkbox" '+(keys.length>=rs.length?'checked':'')+' onchange="selAll()"> 全选</label>'+
    '<span class="sb-info">已选 <b>'+keys.length+'</b> / '+rs.length+' 个 · 共约 '+fmtSize(total)+'</span>'+
    '<span class="sb-tip">送系统回收站，随时可还原</span>'+
    '<button class="sb-go" onclick="confirmRecycle()">移入回收站</button>';
}
var recyclePending=[];
function confirmRecycle(){
  var keys=Object.keys(selSet);
  if(!keys.length)return;
  var total=0;keys.forEach(function(k){total+=(selSet[k].size||0);});
  document.getElementById('recycleSummary').innerHTML='将把选中的 <b>'+keys.length+'</b> 个文件移入系统回收站（约 '+fmtSize(total)+'）：';
  var names=keys.map(function(k){return '· '+esc(selSet[k].name);});
  document.getElementById('recycleList').innerHTML=names.slice(0,8).join('<br>')+
    (names.length>8?('<br>· …等共 '+names.length+' 个'):'');
  recyclePending=keys.slice();
  document.getElementById('recycleMask').classList.add('show');
}
function closeRecycleConfirm(){document.getElementById('recycleMask').classList.remove('show');}
document.getElementById('recycleMask').addEventListener('click',function(ev){
  if(ev.target===this)closeRecycleConfirm();});
function doRecycleConfirm(){
  var keys=recyclePending.slice();recyclePending=[];
  document.getElementById('recycleMask').classList.remove('show');
  if(!keys.length)return;
  var done=0,ok=0,failed=[];
  (function next(){
    if(done>=keys.length){
      selSet={};updateSelbar();
      petSay(failed.length?('移入完成：'+ok+' 个成功，'+failed.length+' 个失败（可能被占用或已移动）～')
        :('已把 '+ok+' 个文件移入回收站，随时可还原～'),5600);
      doSearchQuiet();loadStatus();
      return;}
    var p=keys[done++];
    fetch('/api/recycle',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:p})})
      .then(function(r){return r.json()}).then(function(s){
        if(s.ok)ok++;else failed.push(selSet[p]?selSet[p].name:p);
        next();})
      .catch(function(){failed.push(p);next();});
  })();
}

/* ---------- 时间分布轴：点击年份过滤，再点取消 ---------- */
function renderYearbar(by){
  var yb=document.getElementById('yearbar');
  if(!by||by.length<2){yb.className='yearbar';yb.innerHTML='';return;}
  var mx=Math.max.apply(null,by.map(function(b){return b.n;}));
  yb.innerHTML='<span class="yb-t">时间分布（'+by.length+' 年 · 点年份筛选）</span>'+
    by.map(function(b){
      var h=Math.max(5,Math.round(b.n*44/mx));
      var on=(yearSel===b.year)?' on':'';
      var dim=(yearSel&&yearSel!==b.year)?' dim':'';
      return '<div class="yb'+on+dim+'" onclick="toggleYear('+b.year+')" title="'+b.year+' 年 '+b.n+' 个文件">'+
        '<span class="yb-n">'+b.n+'</span>'+
        '<span class="yb-c" style="height:'+h+'px"></span>'+
        '<span class="yb-y">'+b.year+'</span></div>';
    }).join('');
  yb.className='yearbar show';
}
function toggleYear(y){
  if(yearSel===y){yearSel=null;petSay('已取消年份筛选，看全部时间～',3600);}
  else{yearSel=y;petSay('只看 '+y+' 年的文件啦，再点一次取消～',4000);}
  doSearch();
}

/* ---------- 相似文件（文献「相似文献」的本地版） ---------- */
function showSimilar(p,nm){
  var m=document.getElementById('simMask');
  document.getElementById('simTitle').textContent='与「'+(nm||'该文件')+'」相似';
  document.getElementById('simSub').textContent='同目录（当年一起保存的）+ 全库文件名相似的文件，点击条目直接定位：';
  document.getElementById('simList').innerHTML='<div class="sim-empty">正在翻索引找相似文件…</div>';
  m.classList.add('show');
  fetch('/api/similar',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:decodeURIComponent(p)})})
    .then(function(r){return r.json()}).then(function(s){
      if(s.error){document.getElementById('simList').innerHTML=
        '<div class="sim-empty">'+esc(s.error)+'</div>';return;}
      var it=s.items||[];
      if(!it.length){document.getElementById('simList').innerHTML=
        '<div class="sim-empty">没找到相似文件（可能它比较独一无二～）</div>';return;}
      document.getElementById('simList').innerHTML=it.map(function(x,i){
        return '<div class="sim-item" onclick="openP(\''+encodeURIComponent(x.path)+'\',\'folder\',null)" title="'+esc(x.path)+'">'+
          '<span class="si-w '+(x.why==='同目录'?'d1':'d2')+'">'+x.why+'</span>'+
          '<div style="flex:1;min-width:0"><div class="si-n">'+esc(x.name)+'</div>'+
          '<div class="si-m">'+esc(x.dir)+'</div></div>'+
          '<span class="si-x">'+fmtShortTime(x.mtime)+(x.sim?'<br>相似 '+(x.sim*100).toFixed(0)+'%':'')+'</span></div>';
      }).join('');
      petSay('帮你找到 '+it.length+' 个相似文件，点条目就能定位～',4200);
    }).catch(function(e){document.getElementById('simList').innerHTML=
      '<div class="sim-empty">请求失败：'+esc(String(e))+'</div>';});
}
function closeSim(){document.getElementById('simMask').classList.remove('show');}
document.getElementById('simMask').addEventListener('click',function(ev){
  if(ev.target===this)closeSim();});

/* ---------- AI 总结（桥接 TeleAgent 对话：复制路径 + 指引） ---------- */
function aiSummarize(btn,p){
  var path=decodeURIComponent(p);
  function done(){
    markDone(btn,'✓已复制');
    petSay('路径已复制！到 TeleAgent 对话里粘贴，说「总结 '+path.split(/[\\/]/).pop()+'」就能帮我读啦～',6500);
  }
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(path).then(done,function(){fallbackCopy(path)&&done();});
  }else{fallbackCopy(path)&&done();}
}
function fallbackCopy(txt){
  try{
    var ta=document.createElement('textarea');ta.value=txt;
    ta.style.position='fixed';ta.style.opacity='0';document.body.appendChild(ta);
    ta.select();var ok=document.execCommand('copy');ta.remove();return ok;
  }catch(e){return false;}
}

/* ---------- 检索历史（检索页胶囊 + 弹窗完整列表） ---------- */
function loadHistory(autoplay){
  fetch('/api/history').then(function(r){return r.json()}).then(function(s){
    var items=s.items||[];
    document.getElementById('histCount').textContent=items.length;
    var el=document.getElementById('histList');
    if(!items.length){
      el.innerHTML='<div class="hist-empty">还没有检索记录<br>搜索一次就会出现在这里（最多保留最近 50 条）</div>';
      return;}
    el.innerHTML=items.map(function(it,i){
      var cond=[];
      cond.push(it.mode==='all'?'全部命中':'任一命中');
      cond.push(it.sort==='time'?'按时间':'按匹配度');
      if(it.type)cond.push('类型 '+esc(it.type));
      if(it.new)cond.push('收录 '+esc(it.new));
      if(it.within)cond.push('修改 '+esc(it.within));
      if(it.dir)cond.push('路径含 '+esc(it.dir));
      return '<div class="hist-item">'+
        '<span class="h-time">'+fmtShortTime(it.ts)+'</span>'+
        '<span class="h-kw">'+it.kw.map(function(k){return '<span>'+esc(k)+'</span>';}).join('')+'</span>'+
        '<span class="h-cond">'+cond.join(' · ')+'</span>'+
        '<span class="h-hits">命中 <b>'+it.total_hits+'</b> 个</span>'+
        '<button class="h-go" onclick="restoreSearch('+i+')"><svg class="ic" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-2.6-6.3"/><polyline points="21 3 21 9 15 9"/></svg>重新搜索</button></div>';
    }).join('');
    window__history=items;
    if(autoplay&&items.length&&petEl.className.indexOf('welcome')>-1){
      setTimeout(function(){
        petSay(pick(L.lastSearch).replace('{kw}',items[0].kw.join(' ')),6000);
      },5200);
    }
  });
}
var window__history=[];
function openHist(){loadHistory(false);document.getElementById('histMask').classList.add('show');}
function closeHist(){document.getElementById('histMask').classList.remove('show');}
document.getElementById('histMask').addEventListener('click',function(ev){
  if(ev.target===this)closeHist();});
/* 检索页顶部「最近搜索」胶囊：一键重搜 */
var window__recent=[];
function loadRecentKws(){
  fetch('/api/history').then(function(r){return r.json()}).then(function(s){
    var items=(s.items||[]).slice(0,6);
    var bar=document.getElementById('recentbar');
    if(!items.length){bar.style.display='none';return;}
    window__recent=items;
    bar.style.display='flex';
    bar.innerHTML='<span class="rb-t">最近搜索</span>'+
      items.map(function(it,i){
        return '<span class="rb-chip" onclick="recentGo('+i+')" title="'+esc(it.kw.join(' '))+'">'+esc(it.kw.join(' '))+'</span>';
      }).join('')+
      '<span class="rb-all" onclick="openHist()">全部历史 →</span>';
  });
}
function recentGo(i){
  var it=window__recent[i];if(!it)return;
  document.getElementById('kw').value=it.kw.join(' ');
  doSearch();
}
function restoreSearch(i){
  var it=window__history[i];if(!it)return;
  document.getElementById('kw').value=it.kw.join(' ');
  setModeRaw(it.mode||'any');setSortRaw(it.sort||'score');
  document.getElementById('fType').value=it.type||'';
  document.getElementById('fNew').value=it.new||'';
  document.getElementById('fWithin').value=it.within||'';
  document.getElementById('fDir').value=it.dir||'';
  document.getElementById('fLang').value=(it.lang==='zh'||it.lang==='en')?it.lang:'';
  yearSel=(it.year&&!isNaN(parseInt(it.year)))?parseInt(it.year):null;
  if(it.limit)document.getElementById('fLimit').value=String(it.limit);
  setTab('search');
  petSay('重搜「'+it.kw.join(' ')+'」，安排！',3600);
  doSearch();
}
function setModeRaw(m){mode=m;
  document.getElementById('mAny').className=(m==='any')?'on':'';
  document.getElementById('mAll').className=(m==='all')?'on':'';}
function setSortRaw(s){sort=s;
  document.getElementById('sTime').className=(s==='time')?'on':'';
  document.getElementById('sScore').className=(s==='score')?'on':'';}
function clearHistory(){
  if(!confirm('确定清空全部检索历史吗？'))return;
  fetch('/api/history/clear',{method:'POST'}).then(function(r){return r.json()})
    .then(function(){loadHistory(false);petSay('历史清空啦，从今天重新开始记～');});
}

/* ---------- 意见反馈 ---------- */
var fbType='ux';
function openFeedback(){
  document.getElementById('fbForm').style.display='';
  document.getElementById('fbDone').style.display='none';
  document.getElementById('fbMask').classList.add('show');
  setTimeout(function(){document.getElementById('fbText').focus();},150);
}
function closeFeedback(){document.getElementById('fbMask').classList.remove('show');}
function fbSetType(el){
  fbType=el.dataset.t;
  var sp=document.getElementById('fbTypes').querySelectorAll('span');
  for(var i=0;i<sp.length;i++)sp[i].classList.remove('on');
  el.classList.add('on');
}
document.getElementById('fbMask').addEventListener('click',function(ev){
  if(ev.target===this)closeFeedback();});
function sendFeedback(){
  var text=document.getElementById('fbText').value.trim();
  if(!text){petSay('反馈内容还没写哦～');document.getElementById('fbText').focus();return;}
  fetch('/api/feedback',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({type:fbType,text:text,
      contact:document.getElementById('fbContact').value.trim()})})
    .then(function(r){return r.json()}).then(function(s){
      if(!s.ok){showModal('提交失败',s.error||'未知错误');return;}
      document.getElementById('fbForm').style.display='none';
      document.getElementById('fbDone').style.display='';
      document.getElementById('fbText').value='';
      document.getElementById('fbContact').value='';
      updateFbBadge();
      petSay('收到你的反馈啦，我会记下来慢慢改进的～',5200);
    }).catch(function(e){showModal('请求失败',String(e));});
}
    function updateFbBadge(){
      fetch('/api/feedback/list').then(function(r){return r.json()}).then(function(s){
        var pending=(s.items||[]).filter(function(it){return it.status==='pending';}).length;
        /* v1.2.0：反馈入口已并入设置页，无角标徽章可更新 */
      });
    }

/* ---------- 每日签到 / 宠物商店 ---------- */
function openSignin(){loadSignin();document.getElementById('signinMask').classList.add('show');}
function closeSignin(){document.getElementById('signinMask').classList.remove('show');}
function openShop(){loadShop();document.getElementById('shopMask').classList.add('show');}
function closeShop(){document.getElementById('shopMask').classList.remove('show');}
function openShopFromSignin(){closeSignin();openShop();}
function openSigninFromShop(){closeShop();openSignin();}
/* 设置页：重新授权天气装扮 */
function weatherAgain(){
  fillWeatherCity();
  document.getElementById('wAuthMask').classList.add('show');
}
/* 文件卫士：独立技能，新标签页打开（评审建议：不内嵌 iframe，两个产品风险模型与任务节奏不同） */
function openFileGuard(){
  fetch('http://127.0.0.1:8766/',{mode:'no-cors'}).then(function(){
    window.open('http://127.0.0.1:8766/','_blank');
    petSay('文件卫士已在新标签页打开，扫一扫更安心～',4200);
  }).catch(function(){
    petSay('文件卫士未启动，正在后台拉起…',4200);
    fetch('/api/ext/fileguard/launch',{method:'POST'})
      .then(function(r){return r.json()})
      .then(function(s){
        if(!s.ok){
          showModal('文件卫士','启动失败：'+(s.error||'未知错误')+'\n\n也可以手动双击桌面「文件卫士」图标使用。');return;}
        setTimeout(function(){window.open('http://127.0.0.1:8766/','_blank');},s.already?200:3500);
      }).catch(function(){showModal('文件卫士','无法连接文件卫士服务，请手动双击桌面「文件卫士」图标使用。');});
  });
}
['signinMask','shopMask'].forEach(function(id){
  document.getElementById(id).addEventListener('click',function(ev){
    if(ev.target===this)this.classList.remove('show');});
});

function _fmtToday(){var d=new Date(),p=function(x){return String(x).padStart(2,'0');};
  return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());}

function loadSignin(){
  fetch('/api/pet/shop').then(function(r){return r.json()}).then(function(s){
    document.getElementById('sgPoints').textContent=s.points;
    document.getElementById('sgStreak').textContent=s.streak;
    document.getElementById('sgNext').textContent='+'+s.next_gain;
    var btn=document.getElementById('sgBtn');
    if(s.today_signed){btn.disabled=true;btn.textContent='今日已签到 ✓ 明天再来';}
    else{btn.disabled=false;btn.textContent='签到领 +'+s.next_gain+' 积分';}
    document.getElementById('sgWeek').innerHTML=(s.week||[]).map(function(w){
      var today=w.date===_fmtToday();
      return '<div class="wk'+(w.signed?' on':'')+(today?' today':'')+'" title="'+w.date+'">'+
        '<div class="d">'+w.day+'</div><div class="dot"></div></div>';
    }).join('');
    /* v1.2.0：签到角落入口已并入设置页，积分显示在设置页个性化卡片 */
    var sp=document.getElementById('setPoints');
    if(sp){sp.textContent=s.points;}
  });
}
function doSignin(){
  fetch('/api/pet/signin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({})}).then(function(r){return r.json()}).then(function(o){
    if(o.error){petSay(o.error,4200);loadSignin();return;}
    var btn=document.getElementById('sgBtn');btn.disabled=true;btn.textContent='今日已签到 ✓';
    petEl.className='pet happy';
    petSay('签到成功！+'+o.gained+' 积分到手，连续 '+o.streak+' 天啦～',5200);
    setTimeout(petBackToIdle,5600);
    loadSignin();
  }).catch(function(e){showModal('请求失败',String(e));});
}

function loadShop(){
  fetch('/api/pet/shop').then(function(r){return r.json()}).then(function(s){
    document.getElementById('shPoints').textContent=s.points;
    var inv={};(s.inventory||[]).forEach(function(i){inv[i.id]=i.count;});
    document.getElementById('shGrid').innerHTML=(s.foods||[]).map(function(f){
      var n=inv[f.id]||0;
      return '<div class="food-card"><div class="fe">'+f.emoji+'</div>'+
        '<div class="fn">'+f.name+(n?'<span class="own-tag">库存 ×'+n+'</span>':'')+'</div>'+
        '<div class="fd">'+f.desc+'</div>'+
        '<div class="fp"><b>'+f.price+'</b> 积分</div>'+
        '<div class="fb-row">'+
        '<button class="fb-buy"'+(s.points<f.price?' disabled':'')+' onclick="buyFood(\''+f.id+'\',this)">购买</button>'+
        '<button class="fb-feed"'+(n?'':' disabled')+' onclick="feedFood(\''+f.id+'\')">喂食</button>'+
        '</div></div>';
    }).join('');
        var b=document.getElementById('shPoints');
        var sp=document.getElementById('setPoints');
        if(sp)sp.textContent=s.points;
      });
    }
    function buyFood(id,btn){
  btn.disabled=true;btn.textContent='…';
  fetch('/api/pet/buy',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({food:id})}).then(function(r){return r.json()}).then(function(o){
    if(o.error){petSay(o.error,4200);loadShop();return;}
    petSay('买到了「'+o.name+'」（库存 ×'+o.count+'），想喂就喂～',4600);
    loadShop();
  }).catch(function(e){showModal('请求失败',String(e));});
}
function feedFood(id){
  fetch('/api/pet/feed',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({food:id})}).then(function(r){return r.json()}).then(function(o){
    if(o.error){petSay(o.error,4200);return;}
    playFeed(o.anim,o.fx);
    petSay(o.say,5600);
    setTimeout(loadShop,2800);
  }).catch(function(e){showModal('请求失败',String(e));});
}
function playFeed(anim,fx){
  wakeUp(false);
  petEl.className='pet feeding anim-'+anim;
  spawnFx(fx);
  setTimeout(function(){petEl.className='pet full';},2700);
  setTimeout(function(){petEl.className='pet idle';},4400);
}
function spawnFx(fx){
  var syms={spark:'✨',note:'🎵',star:'⭐',heart:'💗'};
  var s=syms[fx]||'✨';
  for(var i=0;i<6;i++){
    (function(i){
      var p=document.createElement('div');p.className='fxP';p.textContent=s;
      p.style.setProperty('--fx',(Math.random()*100-50)+'px');
      p.style.setProperty('--fr',(Math.random()*360)+'deg');
      p.style.top=(18+Math.random()*34)+'%';
      p.style.animationDelay=(i*0.15)+'s';
      petZone.appendChild(p);
      setTimeout(function(){p.remove();},2400);
    })(i);
  }
}

/* ---------- 睡觉机制（长时间不动犯困睡着，点击唤醒） ---------- */
var lastActive=Date.now(),sleepStage=0;
function resetActivity(){lastActive=Date.now();}
['mousemove','keydown','wheel','touchstart','mousedown'].forEach(function(ev){
  document.addEventListener(ev,resetActivity,{passive:true});
});
document.addEventListener('click',function(){
  if(sleepStage>0)wakeUp(true);
  resetActivity();
});
function _anyModalOpen(){
  return !!(document.querySelector('.pm-mask.show')||document.querySelector('.mask.show'));
}
setInterval(function(){
  var cls=petEl.className;
  if(_anyModalOpen()||cls.indexOf('thinking')>-1||cls.indexOf('welcome')>-1||
     cls.indexOf('eating')>-1||cls.indexOf('feeding')>-1||cls.indexOf('dragging')>-1){
    resetActivity();return;
  }
  var idle=Date.now()-lastActive;
  if(idle>110000&&cls.indexOf('sleep')<0){
    petEl.className='pet sleep';sleepStage=2;
    bubbleEl.classList.remove('show');
  }else if(idle>75000&&sleepStage<1&&(cls==='pet idle'||cls==='pet')){
    petEl.className='pet sleepy';sleepStage=1;
    petSay('唔…有点犯困了…眼皮开始打架…',4200);
  }
},5000);
function wakeUp(loud){
  if(sleepStage===0)return;
  sleepStage=0;resetActivity();
  petEl.className='pet idle';
  petEl.classList.add('happy');
  petSay(loud?'呼啊…！被你点醒啦！精神满满继续工作～':'嗯…小睡一下，元气恢复！',4800);
  setTimeout(function(){petEl.classList.remove('happy');},1400);
}

/* ---------- 天气装扮 ---------- */
var weatherLoaded=false;
function buildRain(){
  var b=document.getElementById('rainBox');b.innerHTML='';
  for(var i=0;i<14;i++){
    var d=document.createElement('div');d.className='rd';
    d.style.left=(Math.random()*100)+'%';
    d.style.animationDelay=(Math.random()*0.9)+'s';
    d.style.animationDuration=(0.55+Math.random()*0.5)+'s';
    b.appendChild(d);
  }
}
function buildSnow(){
  var b=document.getElementById('snowBox');b.innerHTML='';
  for(var i=0;i<10;i++){
    var d=document.createElement('div');d.className='sd';
    d.style.left=(Math.random()*100)+'%';
    d.style.animationDelay=(Math.random()*4)+'s';
    d.style.animationDuration=(3.2+Math.random()*2.5)+'s';
    b.appendChild(d);
  }
}
function buildLeaves(){
  var b=document.getElementById('leafBox');b.innerHTML='';
  var cols=['#d97f3e','#c9622d','#e0955a'];
  for(var i=0;i<5;i++){
    var d=document.createElement('div');d.className='lf';
    d.style.top=(10+Math.random()*70)+'%';
    d.style.left=(Math.random()*30)+'%';
    d.style.animationDelay=(Math.random()*4.4)+'s';
    d.style.animationDuration=(3.6+Math.random()*2)+'s';
    d.innerHTML='<svg width="15" height="15" viewBox="0 0 24 24"><path d="M12 2 L14 8 L20 5 L17 11 L23 13 L17 15 L19 21 L13 18 L12 23 L11 18 L5 21 L7 15 L1 13 L7 11 L4 5 L10 8 Z" fill="'+cols[i%3]+'" opacity=".92"/></svg>';
    b.appendChild(d);
  }
}
function applyWeather(w){
  if(!w||!w.type||w.type==='unknown'){petZone.className='pet-zone';return;}
  petZone.className='pet-zone w-'+w.type;
  var chip=document.getElementById('wChip');
  var temp=(w.temp!==''&&w.temp!=null)?(' '+w.temp+'°C'):'';
  chip.textContent=(w.city||'')+' · '+(w.label||'')+temp;
  if(w.type==='rain')buildRain();
  if(w.type==='snow')buildSnow();
  if(w.type==='wind')buildLeaves();
  if(!weatherLoaded){
    weatherLoaded=true;
    var say={sun:'今天天气真好，晒得我毛茸茸的～',
             rain:'外面下雨啦！我撑好伞、穿好雨靴，不怕不怕～',
             wind:'风好大！还好我围上了围脖，暖呼呼的～',
             snow:'下雪啦！羽绒服和耳包都安排上了，冷不着我～'};
    if(say[w.type])petSay(say[w.type],5400);
  }
}
var weatherAllowed=false, weatherTimer=null;
function fillWeatherCity(){
  /* 预填：优先本浏览器记忆，没有则取本机记住的默认城市 */
  var w=document.getElementById('wCity');if(!w)return;
  var c='';try{c=localStorage.getItem('dm_wcity')||'';}catch(e){}
  if(c){w.value=c;return;}
  fetch('/api/pet/weather/city').then(function(r){return r.json()}).then(function(s){
    if(s&&s.city&&!w.value)w.value=s.city;
  }).catch(function(){});
}
function getWeatherCity(){
  var c=(document.getElementById('wCity').value||'').trim();
  try{localStorage.setItem('dm_wcity',c);}catch(e){}
  return c;
}
function loadWeather(){
  /* 仅在用户当次会话授权后联网查询；拒绝则完全不发请求 */
  if(!weatherAllowed)return;
  var c=getWeatherCity();
  var url='/api/pet/weather'+(c?('?city='+encodeURIComponent(c)):'');
  fetch(url).then(function(r){return r.json()}).then(function(w){
    if(w&&w.error){petSay(w.error,5200);applyWeather(null);return;}
    applyWeather(w);
  }).catch(function(){});
  clearTimeout(weatherTimer);
  weatherTimer=setTimeout(loadWeather,30*60*1000);
}
function weatherAgree(){
  weatherAllowed=true;
  document.getElementById('wAuthMask').classList.remove('show');
  var c=getWeatherCity();
  /* 同意时把城市记到本机（换浏览器/重装界面也不丢），拒绝则不记 */
  fetch('/api/pet/weather/city',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({city:c})}).catch(function(){});
  loadWeather();
}
function weatherDeny(){
  weatherAllowed=false;
  clearTimeout(weatherTimer);
  document.getElementById('wAuthMask').classList.remove('show');
  petSay('好的～不查天气，我就不穿装扮了，安静帮你找文件！',5200);
}

/* ---------- 回收站（连接系统回收站） ---------- */
var recycleZone=document.getElementById('recycleZone');
var rcCount=0;
function dupDragStart(ev,el){
  ev.dataTransfer.setData('text/plain',decodeURIComponent(el.dataset.p));
  ev.dataTransfer.effectAllowed='move';
}
recycleZone.addEventListener('dblclick',function(ev){
  ev.stopPropagation();
  fetch('/api/recycle/open',{method:'POST'}).then(function(){
    petSay('已打开系统回收站，删除的文件都在那里可以找回～',5000);});
});
recycleZone.addEventListener('click',function(ev){ev.stopPropagation();});
recycleZone.addEventListener('dragover',function(ev){
  ev.preventDefault(); ev.dataTransfer.dropEffect='move';
  recycleZone.classList.add('dragover');
});
recycleZone.addEventListener('dragleave',function(ev){
  recycleZone.classList.remove('dragover');
});
recycleZone.addEventListener('drop',function(ev){
  ev.preventDefault();
  recycleZone.classList.remove('dragover');
  var p=ev.dataTransfer.getData('text/plain');
  if(p)recyclePath(p,null);
});
function recycleOne(btn){
  recyclePath(decodeURIComponent(btn.dataset.p),btn.closest('.dup-item')||btn);
}
function recyclePath(p,row){
  fetch('/api/recycle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:p})})
    .then(function(r){return r.json()}).then(function(s){
      if(!s.ok){showModal('无法移入回收站',s.error||'未知错误');return;}
      rcCount++;var b=document.getElementById('rcBadge');
      b.textContent=rcCount;b.classList.add('show');
      recycleZone.classList.add('rc-done');
      setTimeout(function(){recycleZone.classList.remove('rc-done');},600);
      petSay('已把「'+(s.name||'文件')+'」送入回收站，桌面回收站里可以还原～',5200);
      if(row)row.classList.add('recycled');
      setTimeout(function(){
        if(row)row.remove();
        /* 重刷当前搜索以更新重复计数 */
        if(document.getElementById('kw').value.trim()&&tab==='search')doSearchQuiet();
      },520);
    }).catch(function(e){showModal('请求失败',String(e));});
}
function doSearchQuiet(){
  var kw=document.getElementById('kw').value.trim();if(!kw)return;
  var q=new URLSearchParams();
  kw.split(/\s+/).forEach(function(k){q.append('kw',k);});
  q.set('mode',mode);q.set('sort',sort);
  var t=document.getElementById('fType').value;if(t)q.set('type',t);
  var n=document.getElementById('fNew').value;if(n)q.set('new',n);
  var w=document.getElementById('fWithin').value;if(w)q.set('within',w);
  var d=document.getElementById('fDir').value.trim();if(d)q.set('dir',d);
  var lg=document.getElementById('fLang').value;if(lg)q.set('lang',lg);
  if(yearSel)q.set('year',yearSel);
  q.set('limit',document.getElementById('fLimit').value);
  fetch('/api/search?'+q.toString()).then(function(r){return r.json()}).then(function(out){
    if(!out.error)renderResults(out.results);});
}

/* ---------- 喂宠物（拖拽删除） ---------- */
var dragCard=null;
function dragStart(ev,i){
  ev.dataTransfer.setData('text/plain',window__results[i]?window__results[i].path:'');
  ev.dataTransfer.effectAllowed='move';
  dragCard=document.getElementById('rc-'+i);
}
petZone.addEventListener('dragover',function(ev){
  ev.preventDefault(); ev.dataTransfer.dropEffect='move';
  petZone.classList.add('dragover');
  if(petEl.className.indexOf('hungry')<0){petEl.classList.add('hungry');petSay(pick(L.hungry),6000);}
});
petZone.addEventListener('dragleave',function(ev){
  if(ev.target===petZone||!petZone.contains(ev.relatedTarget)){
    petZone.classList.remove('dragover');petEl.classList.remove('hungry');}
});
petZone.addEventListener('drop',function(ev){
  ev.preventDefault();
  petZone.classList.remove('dragover');petEl.classList.remove('hungry');
  var files=ev.dataTransfer.files;
  if(files&&files.length){
    var f=files[0];
    fetch('/api/pet/eatmatch?name='+encodeURIComponent(f.name)+'&size='+f.size,{method:'POST'})
      .then(function(r){return r.json()}).then(function(s){
        if(s.ok){eatAnim(null,s.name,s.count);}
        else{petEl.classList.remove('hungry');showModal('喂不了',s.error||'未知错误');setPet('sad');}
      });
    return;
  }
  var p=ev.dataTransfer.getData('text/plain');
  if(p)eatFile(p);
});

function eatFile(path){
  fetch('/api/pet/eat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:path})})
    .then(function(r){return r.json()}).then(function(s){
      if(s.ok){eatAnim(dragCard,s.name,s.count);}
      else{if(dragCard)dragCard.classList.remove('eaten');
        petEl.classList.remove('hungry','eating');
        showModal('喂不了',s.error||'未知错误');setPet('sad');}
    }).catch(function(e){showModal('请求失败',String(e));});
}
function eatAnim(card,name,count){
  petEl.classList.add('eating');
  if(card)card.classList.add('eaten');
  sparks();
  setTimeout(function(){
    petEl.classList.remove('eating');
    petEl.classList.add('gulp');
    setTimeout(function(){petEl.classList.remove('gulp');petEl.classList.add('full');},550);
  },560);
  setTimeout(function(){
    petEl.classList.remove('full');petEl.classList.add('happy');
    petSay(pick(L.eatOk),4600);
    burpFx();
    if(card)card.remove();
    checkListEmpty();
    updateBelly(true);
    if(typeof count==='number'&&count>=90&&count<99){
      setTimeout(function(){petSay(pick(L.bellyNearFull).replace('{n}',99-count),5000);},4800);
    }
    setTimeout(petBackToIdle,5200);
  },1350);
}
function sparks(){
  for(var i=0;i<16;i++){
    var s=document.createElement('span');s.className='spark';
    var a=Math.random()*Math.PI*2, d=44+Math.random()*56;
    s.style.setProperty('--sx',(Math.cos(a)*d)+'px');
    s.style.setProperty('--sy',(Math.sin(a)*d*0.65-24)+'px');
    s.style.animationDelay=(Math.random()*0.14)+'s';
    petZone.appendChild(s);
    setTimeout(function(x){return function(){x.remove();}}(s),850);
  }
}
function burpFx(){
  var b=document.createElement('span');b.className='burp-fx';b.textContent='嗝～';
  petZone.appendChild(b);setTimeout(function(){b.remove();},1200);
}
function checkListEmpty(){
  var list=document.getElementById('list');
  if(list.children.length===0){
    var empty=document.getElementById('empty');
    empty.style.display='';
    empty.innerHTML='文件都被宠物吃掉啦～<br>双击它的肚子还能吐回来（24 小时内）';
  }
}

/* ---------- 肚子：清单 / 吐出 ---------- */
document.getElementById('bellyHot').addEventListener('dblclick',function(ev){
  ev.stopPropagation();spitAll();
});
document.getElementById('bellyHot').addEventListener('click',function(ev){ev.stopPropagation();});
function toggleBellyPanel(ev){ev.stopPropagation();bellyPanel.classList.toggle('show');}
function updateBelly(afterEat){
  fetch('/api/pet/belly').then(function(r){return r.json()}).then(function(s){
    var items=s.items||[];
    bellyBadge.textContent=items.length+(items.length>=99?'':'');
    bellyBadge.classList.toggle('show',items.length>0);
    bellyBadge.classList.toggle('full99',items.length>=99);
    bellyBadge.title=items.length>=99?'肚子满啦（99/99）':'点开肚子清单（'+items.length+'/99）';
    if(afterEat){bellyBadge.classList.add('pulse');
      setTimeout(function(){bellyBadge.classList.remove('pulse');},650);}
    document.getElementById('bpCount').textContent=items.length;
    var bpList=document.getElementById('bpList');
    bpList.innerHTML=items.length?items.map(function(it){
      return '<div class="bp-item"><span class="nm" title="'+esc(it.orig)+'">'+esc(it.name)+'</span>'+
        '<span class="cd">'+it.left+' 后删除</span></div>';
    }).join(''):'<div class="bp-empty">肚子里空空的，什么都没吃～</div>';
    if(afterEat){bellyPanel.classList.add('show');
      setTimeout(function(){bellyPanel.classList.remove('show');},5000);}
  });
}
function spitAll(){
  fetch('/api/pet/spit',{method:'POST'}).then(function(r){return r.json()}).then(function(s){
    var restored=s.restored||[];
    if(!restored.length){
      petSay(L.spitEmpty,4200);
      bellyPanel.classList.remove('show');
      return;}
    petEl.classList.add('spit');
    petSay('唔…稍等，我把吃进去的都吐出来…',restored.length*300+1200);
    restored.forEach(function(f,i){
      setTimeout(function(){flyFile(f.name);},i*300);
    });
    setTimeout(function(){
      petEl.classList.remove('spit');
      petEl.classList.add('happy');
      petSay(L.spitDone,5200);
      burpFx();
      updateBelly(false);
      if(document.getElementById('kw').value.trim())doSearch();
      setTimeout(petBackToIdle,5600);
    },restored.length*300+800);
    var failed=s.failed||[];
    if(failed.length){
      showModal('部分文件没吐出来',failed.map(function(f){return f.name+'：'+f.error;}).join('\n'));
    }
  });
}
/* 抛物线飞回 */
function flyFile(name){
  var petR=petEl.getBoundingClientRect();
  var listEl=document.getElementById('list');
  var listR=listEl.getBoundingClientRect();
  var sx=petR.left+petR.width*0.5, sy=petR.top+26;
  var tx, ty;
  if(listR.width>0){tx=listR.left+Math.min(280,listR.width*0.4);ty=listR.top+30;}
  else{tx=window.innerWidth*0.5;ty=window.innerHeight*0.35;}
  var outer=document.createElement('div');outer.className='fly-outer';
  outer.style.left=sx+'px';outer.style.top=sy+'px';
  outer.style.setProperty('--dx',(tx-sx)+'px');
  var inner=document.createElement('div');inner.className='fly-inner';
  inner.style.setProperty('--dy',(ty-sy)+'px');
  var chip=document.createElement('div');chip.className='fly-chip';chip.textContent=name;
  chip.title=name;
  inner.appendChild(chip);outer.appendChild(inner);document.body.appendChild(outer);
  setTimeout(function(){outer.remove();},1000);
}

/* ---------- 打开 / 复制 ---------- */
function openP(enc,m,btn){
  fetch('/api/open?path='+enc+'&mode='+m).then(function(r){return r.json()}).then(function(s){
    if(s.ok){
      if(s.action==='fallback_folder'){markDone(btn,'已定位文件夹');petSay(s.message||'已为你定位到所在文件夹～',5200);}
      else if(s.action==='located'){markDone(btn,'已定位');}
      else{markDone(btn,m==='file'?'已打开':'已定位');}
    }else{showModal('无法打开',s.error||'未知错误');petSay('呜，这个路径好像失效了…');}
  }).catch(function(e){showModal('请求失败',String(e));});
}
function copyP(enc,btn){
  var p=decodeURIComponent(enc);
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(p).then(function(){markDone(btn,'已复制');});
  }else{showModal('完整路径',p);}
}

/* ---------- 扫描 ---------- */
function startScan(silent){
  fetch('/api/scan',{method:'POST'}).then(function(r){return r.json()}).then(function(s){
    if(!s.started){showModal('扫描进行中','已有一次增量扫描在后台运行，请稍候。');return;}
    petSay('开始翻找新文件啦，稍等一下下～');
    if(!silent){showModal('增量更新已启动','正在后台扫描新增与修改的文件，完成后本提示自动关闭。');}
    pollScan();});
}
function pollScan(){
  fetch('/api/scan/status').then(function(r){return r.json()}).then(function(s){
    if(s.running){setTimeout(pollScan,2000);return;}
    loadStatus();hideModal();petSay('索引更新完毕！现在搜索能找到最新文件啦～');
  });
}

/* ---------- 标签页：检索（默认）/ 资料库 / 发现 / 设置 ---------- */
function setTab(t){tab=t;
  var tabs={'search':'tabSearch','lib':'tabLib','discover':'tabDiscover','settings':'tabSettings'};
  var panes={'search':'paneSearch','lib':'paneLib','discover':'paneDiscover','settings':'paneSettings'};
  for(var k in tabs){document.getElementById(tabs[k]).className=(t===k)?'on':'';}
  for(var k2 in panes){document.getElementById(panes[k2]).style.display=(t===k2)?'':'none';}
  if(t==='discover'&&!document.getElementById('snapDate').options.length)loadSnapList();
  if(t==='discover'||t==='lib')loadStats();
  if(t==='search')loadRecentKws();
  if(t==='settings')loadSignin();}
function loadSnapList(){
  fetch('/api/snapshots').then(function(r){return r.json()}).then(function(s){
    var sel=document.getElementById('snapDate');
    sel.innerHTML='<option value="">选择日期…</option>'+
      (s.dates||[]).map(function(d){return '<option>'+d+'</option>';}).join('');
    if(s.dates&&s.dates.length){sel.value=s.dates[0];loadSnap();}
  });
}
function loadSnap(){
  var d=document.getElementById('snapDate').value, c=document.getElementById('snapContent');
  if(!d){c.textContent='选择日期查看当天新增与修改的文件…';return;}
  c.textContent='加载中…';
  fetch('/api/snapshot?date='+encodeURIComponent(d)).then(function(r){return r.json()})
    .then(function(s){c.textContent=s.error?s.error:s.content;});
}

/* ---------- 数据库面板 ---------- */
function fmtGB(n){
  if(n>=1073741824)return (n/1073741824).toFixed(1)+' GB';
  if(n>=1048576)return (n/1048576).toFixed(1)+' MB';
  if(n>=1024)return (n/1024).toFixed(1)+' KB';
  return n+' B';
}
function fmtChars(n){
  if(n>=100000000)return (n/100000000).toFixed(1)+'亿';
  if(n>=10000)return (n/10000).toFixed(1)+'万';
  return String(n);
}
function loadStats(){
  fetch('/api/stats').then(function(r){return r.json()}).then(function(s){
    if(s.error){document.getElementById('dbCards').innerHTML=
      '<div class="db-empty">'+s.error+'</div>';return;}
    /* 统计卡 */
    document.getElementById('dbCards').innerHTML=
      '<div class="db-card"><div class="v">'+s.total.toLocaleString()+'</div><div class="k">收录文件总数</div></div>'+
      '<div class="db-card"><div class="v">'+s.with_text.toLocaleString()+'<small>('+s.text_rate+'%)</small></div><div class="k">已提取正文</div></div>'+
      '<div class="db-card"><div class="v">'+fmtGB(s.total_size)+'</div><div class="k">索引覆盖文件总量</div></div>'+
      '<div class="db-card"><div class="v">'+fmtChars(s.content_chars)+'<small>字符</small></div><div class="k">索引正文累计</div></div>'+
      '<div class="db-card"><div class="v">'+s.db_mb+'<small>MB</small></div><div class="k">数据库体积</div></div>'+
      '<div class="db-card"><div class="v">'+s.today_new.toLocaleString()+'</div><div class="k">今日新收录</div></div>';
    /* 类型分布 */
    var mx=0;var tps=s.by_type||[];
    tps.forEach(function(t){if(t.count>mx)mx=t.count;});
    document.getElementById('dbTypes').innerHTML=tps.map(function(t){
      var w=mx?(t.count/mx*100):0;
      return '<div class="tp-row"><span class="nm">'+esc(t.ext)+'</span>'+
        '<div class="bar-wrap"><div class="bar" style="width:'+w.toFixed(1)+'%"></div></div>'+
        '<span class="val">'+t.count.toLocaleString()+'</span></div>';
    }).join('')||'<div class="db-empty">暂无数据</div>';
    /* 录入趋势 */
    var tr=s.trend||[];var tmax=0;
    tr.forEach(function(t){if(t.count>tmax)tmax=t.count;});
    document.getElementById('dbTrend').innerHTML=tr.map(function(t){
      var h=tmax?(t.count/tmax*76):0;
      var lab=(t.date||'').slice(5);
      return '<div class="tr-col" title="'+esc(t.date)+' 新收录 '+t.count.toLocaleString()+' 个">'+
        '<span class="tr-val">'+(t.count>=1000?(t.count/1000).toFixed(1)+'k':t.count)+'</span>'+
        '<div class="tr-bar" style="height:'+h.toFixed(1)+'px"></div>'+
        '<span class="tr-lab">'+esc(lab)+'</span></div>';
    }).join('')||'<div class="db-empty">暂无趋势数据</div>';
    /* 重复榜 */
    var dups=s.dups||[];
    document.getElementById('dbDups').innerHTML=dups.map(function(d,i){
      return '<div class="dup-row"><span class="rk">'+(i+1)+'</span>'+
        '<span class="dn" title="'+esc(d.name)+'">'+esc(d.name)+'</span>'+
        '<span class="dc">重复 '+d.copies+' 份</span>'+
        '<span class="dw">可释放 '+fmtGB(d.waste)+'</span></div>';
    }).join('')||'<div class="db-empty">没有发现重复文件，很干净！</div>';
  }).catch(function(e){showModal('请求失败',String(e));});
}

document.getElementById('kw').addEventListener('keydown',function(e){
  if(e.key==='Enter'){yearSel=null;doSearch();}});
window.addEventListener('keydown',function(e){if(e.key==='Escape'){hideModal();closeSim();}});

/* 自愈：极端情况下浏览器回退到缓存的旧版页面（旧嘴特征），自动强刷一次 */
(function(){
  try{
    var stale=document.getElementById('gMouth')||document.querySelector('.mouth-big ellipse[fill="#ef9a8a"]')
      ||document.querySelector('.mouth-yawn');
    if(stale&&!location.search.match(/[?&]_f=/)){
      location.replace(location.pathname+'?_f=1&'+Date.now());
    }
  }catch(e){}
})();

/* 进页面：宠物打招呼 */
setPet('welcome');
setTimeout(petBackToIdle,5200);
loadStatus();
loadRecentKws();
loadHistory(true);
updateBelly(false);
updateFbBadge();
/* 天气：每次进入界面询问一次授权，同意才联网查天气温度
   首次使用的授权弹窗还没处理完时先等待其关闭，避免两层弹窗互相遮挡（按钮点不动） */
function askWeatherAuth(){
  fillWeatherCity();
  document.getElementById('wAuthMask').classList.add('show');
}
setTimeout(function(){
  var wm=document.getElementById('welcomeMask');
  if(wm&&wm.classList.contains('show')){
    var t=setInterval(function(){
      if(!wm.classList.contains('show')){clearInterval(t);askWeatherAuth();}
    },600);
  }else{askWeatherAuth();}
},2400);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description="文档拾忆 · 独立检索界面")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # 快捷方式零窗口复用：端口可连通说明服务已在跑，直接开浏览器退出
    # （Windows 下 bind 探测不可靠：SO_REUSEADDR 语义允许多绑定，故改用连接探测）
    import socket
    url = "http://127.0.0.1:{}/?v=smile3".format(args.port)
    probe = socket.socket()
    probe.settimeout(0.6)
    try:
        probe.connect(("127.0.0.1", args.port))
    except OSError:
        probe.close()
    else:
        probe.close()
        if not args.no_browser:
            # 异步开浏览器，避免某些环境下阻塞进程退出
            try:
                subprocess.Popen(["cmd", "/c", "start", "", url],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError:
                webbrowser.open(url)
        print("已有实例在运行，已直接打开浏览器。")
        # pythonw 环境下可能有非守护线程残留，强制退出避免僵尸进程
        sys.stdout.flush()
        os._exit(0)

    threading.Thread(target=_sweeper, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("文档拾忆独立界面已启动：" + url)
    print("仅本机可访问（127.0.0.1），Ctrl+C 退出。")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
