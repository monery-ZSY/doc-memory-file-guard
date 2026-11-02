#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档拾忆 · 每日自检（配合凌晨 00:00 定时任务）
检查项与自动修复：
1. 索引完整性（PRAGMA quick_check）——异常仅报告
2. 孤儿记录（文件已不存在的索引条目）——自动清理
3. 索引新鲜度（距上次全量扫描的天数）——超过 3 天提示应扫描
4. 服务日志错误扫描（scan-web.log 中的 Traceback/Error）——截断超大日志
5. 宠物肚子过期文件清理（超 24 小时）——自动彻底删除
6. 待处理用户反馈统计——报告给智能体，由智能体读取反馈内容并优化界面
输出 JSON 结果，供定时任务中的智能体决策。

用法:
  python selfcheck.py [--fix]     # --fix 启用自动修复（默认启用）
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from indexer import DB_PATH, DATA_DIR, init_db  # noqa: E402
import pet_belly  # noqa: E402

LOG_PATH = DATA_DIR / "scan-web.log"
FEEDBACK_PATH = DATA_DIR / "feedback.json"
ORPHAN_FULL = True        # 孤儿全量核对（零依赖 os.path.exists，约 1 分钟，凌晨可接受）
LOG_KEEP_CHARS = 40000   # 日志截断保留量


def check_db(fix):
    """索引完整性 + 孤儿记录 + 新鲜度。"""
    out = {"name": "索引完整性"}
    if not DB_PATH.exists():
        out.update(status="fail", detail="索引文件不存在，需要先运行 indexer scan")
        return out
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    with_text = conn.execute("SELECT COUNT(*) FROM files WHERE status='text'").fetchone()[0]
    last_scan = conn.execute("SELECT value FROM meta WHERE key='last_scan'").fetchone()
    out["detail"] = f"收录 {total} 个文件，正文 {with_text} 个"

    # quick_check
    ok = conn.execute("PRAGMA quick_check").fetchone()[0]
    if ok != "ok":
        out.update(status="warn", detail=out["detail"] + f"；完整性检查异常：{ok}")
    else:
        out["status"] = "pass"

    # 新鲜度
    if last_scan:
        out["detail"] += f"；上次扫描 {last_scan[0]}"
        try:
            d = date.fromisoformat(last_scan[0][:10])
            age = (date.today() - d).days
            if age >= 3:
                out.update(status="warn", detail=out["detail"] + f"；已 {age} 天未扫描，建议增量更新")
        except ValueError:
            pass

    # 孤儿记录（全量核对：索引有、磁盘无的失效记录，搜出来会打不开）
    orphans = [r[0] for r in conn.execute("SELECT path FROM files")]
    gone = [p for p in orphans if not Path(p).exists()]
    if gone:
        out["detail"] += f"；全量核对 {len(orphans)} 条，发现 {len(gone)} 条孤儿记录（文件已不存在）"
        if fix:
            conn.executemany("DELETE FROM files WHERE path=?",
                             [(p,) for p in gone])
            conn.commit()
            out["fixed"] = f"已清理孤儿记录 {len(gone)} 条（全量）"
            out["status"] = out.get("status", "pass")
        else:
            out["status"] = "warn"
    else:
        out["detail"] += f"；全量核对 {len(orphans)} 条，无孤儿记录"
    conn.close()
    return out


def check_log(fix):
    """扫描日志错误 + 超大截断。"""
    out = {"name": "运行日志"}
    if not LOG_PATH.exists():
        out.update(status="pass", detail="无日志（尚未触发过网页端扫描）")
        return out
    size = LOG_PATH.stat().st_size
    try:
        tail = LOG_PATH.read_text(encoding="utf-8", errors="replace")[-LOG_KEEP_CHARS:]
    except OSError:
        out.update(status="warn", detail="日志读取失败")
        return out
    errs = re.findall(r"(Traceback[^\n]*|ERROR[^\n]*)", tail)
    if errs:
        out.update(status="warn", detail=f"日志中发现 {len(errs)} 处错误痕迹，最近：{errs[-1][:90]}")
    else:
        out.update(status="pass", detail=f"日志 {size // 1024}KB 无错误痕迹")
    if fix and size > 512 * 1024:
        LOG_PATH.write_text(tail, encoding="utf-8")
        out["fixed"] = f"超大日志已截断至 {LOG_KEEP_CHARS // 1000}K 字符"
    return out


def check_belly(fix):
    """宠物肚子过期文件清理。"""
    out = {"name": "宠物肚子暂存"}
    try:
        items = pet_belly.list_items()
        n = items["count"]
        if n == 0:
            out.update(status="pass", detail="肚子为空")
        else:
            out.update(status="pass", detail=f"暂存 {n} 个文件（24 小时后自动清理）")
        if fix:
            purged = pet_belly.purge_expired()
            if purged:
                out["fixed"] = f"已彻底删除过期暂存文件 {purged} 个"
    except Exception as exc:
        out.update(status="warn", detail=f"检查失败：{exc}")
    return out


def check_feedback():
    """待处理反馈统计（内容由智能体在定时任务里读取并优化界面）。"""
    out = {"name": "用户反馈"}
    if not FEEDBACK_PATH.exists():
        out.update(status="pass", detail="暂无反馈")
        return out
    try:
        items = json.loads(FEEDBACK_PATH.read_text(encoding="utf-8")).get("items", [])
        pending = [it for it in items if it.get("status") == "pending"]
        if pending:
            out.update(status="action", detail=(
                f"有 {len(pending)} 条待处理反馈："
                + "；".join(f"[{it.get('type_cn')}] {it.get('text', '')[:40]}"
                            for it in pending[:5])))
            out["pending"] = pending
        else:
            out.update(status="pass", detail=f"共 {len(items)} 条反馈，全部已处理")
    except Exception as exc:
        out.update(status="warn", detail=f"反馈文件解析失败：{exc}")
    return out


def run(fix=True):
    checks = [check_db(fix), check_log(fix), check_belly(fix), check_feedback()]
    overall = "pass"
    for c in checks:
        if c["status"] == "warn" and overall == "pass":
            overall = "warn"
        if c["status"] == "fail":
            overall = "fail"
        if c["status"] == "action" and overall == "pass":
            overall = "action"
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "fix_enabled": fix,
            "overall": overall, "checks": checks}


def main():
    ap = argparse.ArgumentParser(description="文档拾忆 · 每日自检")
    ap.add_argument("--fix", action="store_true", default=True,
                    help="启用自动修复（默认启用）")
    ap.add_argument("--no-fix", dest="fix", action="store_false")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()
    result = run(fix=args.fix)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return
    print(f"自检完成（{result['time']}）总体：{result['overall']}")
    for c in result["checks"]:
        line = f"  [{c['status']:>6}] {c['name']}：{c['detail']}"
        if c.get("fixed"):
            line += f" → {c['fixed']}"
        print(line)
    if result["overall"] == "action":
        print("存在待处理用户反馈，请智能体读取 data/feedback.json 识别问题并优化界面。")


if __name__ == "__main__":
    main()
