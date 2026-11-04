#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档拾忆 · 搜索器
在本地索引中按内容关键词搜索文件，支持多关键词、按时间/类型/目录过滤、相关度打分。

用法:
  python searcher.py "关键词1" "关键词2" [--all] [--type docx,pdf] [--within 7d|2026-10-01..2026-10-28]
                     [--new today|yesterday|7d|2026-10-27] [--dir 子串] [--limit 20] [--json]

模块复用:
  from searcher import run_search
  run_search(["关键词"], require_all=True, limit=20)  → dict（与 --json 输出同构）
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from indexer import DB_PATH  # noqa: E402  (共享数据目录解析逻辑与索引器一致)

STATUS_CN = {"text": "已提取正文", "name-only": "仅文件名(内容不可提取)"}


def log(msg):
    print(msg, flush=True)


def escape_like(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_within(v):
    """7d / 30d / 2026-10-01 / 2026-10-01..2026-10-28 → (start_ts, end_ts)"""
    today = date.today()
    if re.fullmatch(r"(\d+)d", v):
        days = int(re.match(r"(\d+)d", v).group(1))
        return time.mktime((today - timedelta(days=days)).timetuple()), time.time() + 86400
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        d0 = time.mktime(datetime.strptime(v, "%Y-%m-%d").timetuple())
        return d0, d0 + 86399
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})", v)
    if m:
        s = time.mktime(datetime.strptime(m.group(1), "%Y-%m-%d").timetuple())
        e = time.mktime(datetime.strptime(m.group(2), "%Y-%m-%d").timetuple()) + 86399
        return s, e
    raise ValueError(f"--within 无法解析: {v}（支持 7d / 2026-10-01 / 2026-10-01..2026-10-28）")


def parse_new(v):
    """today / yesterday / 7d / 2026-10-27 → (first_seen>=s[, <=e])"""
    today = date.today()
    if v == "today":
        return today.isoformat(), today.isoformat()
    if v == "yesterday":
        y = (today - timedelta(days=1)).isoformat()
        return y, y
    if re.fullmatch(r"(\d+)d", v):
        days = int(re.match(r"(\d+)d", v).group(1))
        return (today - timedelta(days=days - 1)).isoformat(), None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v, v
    raise ValueError(f"--new 无法解析: {v}（支持 today / yesterday / 7d / 2026-10-27）")


def score_row(name, dirpath, content, kws, require_all):
    """纯匹配质量分（不含任何时间因素，时间只作排序次级依据）：
    文件名命中 +10/词、目录命中 +5/词、正文每次命中 +2 封顶 10 次（+20/词）；
    全部关键词都命中时额外 +5×(词数-1)，把“全覆盖”与“部分命中”拉开差距。"""
    s = 0
    hits = 0
    nl, dl = name.lower(), dirpath.lower()
    cl = (content or "").lower()
    for kw in kws:
        k = kw.lower()
        h = 0
        if k in nl:
            s += 10
            h = 1
        if k in dl:
            s += 5
            h = 1
        cnt = cl.count(k)
        if cnt:
            s += min(cnt, 10) * 2
            h = 1
        if h:
            hits += 1
        elif require_all:
            return -1
    if len(kws) > 1 and hits == len(kws):
        s += 5 * (len(kws) - 1)
    return s


def make_snippets(content, kws, ctx=40, max_snips=3):
    if not content:
        return []
    cl = content.lower()
    snips = []
    for kw in kws:
        k = kw.lower()
        pos = cl.find(k)
        if pos < 0:
            continue
        start = max(0, pos - ctx)
        end = min(len(content), pos + len(k) + ctx)
        frag = content[start:end].replace("\n", " ").replace("\r", " ")
        frag = re.sub(re.escape(kw), lambda m: f"【{m.group(0)}】", frag, count=1,
                      flags=re.IGNORECASE)
        snips.append(("…" if start > 0 else "") + frag + ("…" if end < len(content) else ""))
        if len(snips) >= max_snips:
            break
    return snips


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------- 核心搜索（供 CLI 与 web_ui 共用） ----------------

_ZH_RE = re.compile(r"[\u4e00-\u9fff]")   # 文件名含中文字符判定


def run_search(keywords, require_all=False, type_=None, within=None, new=None,
                dir_sub=None, limit=20, context=40, sort="score", year=None,
                name_lang=None):
    """执行检索并返回结构化结果 dict。参数校验失败抛 ValueError。
    sort: "score" 按相关度，"time" 按修改时间倒序。
    year: 仅保留该自然年（如 2024）的文件；by_year 始终返回全量年份分布（过滤前）。
    name_lang: "zh" 只看含中文名的 / "en" 只看纯英文数字符号名的 / None 不过滤。"""
    if not keywords:
        raise ValueError("缺少关键词")
    if sort not in ("score", "time"):
        raise ValueError(f"sort 仅支持 score/time，收到: {sort}")
    if year is not None:
        if not (isinstance(year, int) and 1990 <= year <= 2100):
            raise ValueError("year 仅支持 1990-2100 的年份数字")
    if name_lang not in (None, "", "zh", "en"):
        raise ValueError("name_lang 仅支持 zh/en")
    name_lang = name_lang or None
    if not DB_PATH.exists():
        raise FileNotFoundError("索引尚未建立：请先运行 indexer.py scan")

    conn = sqlite3.connect(str(DB_PATH), timeout=3)
    where, params = [], []

    like_parts = []
    for kw in keywords:
        e = escape_like(kw.lower())
        like_parts.append("(lower(name) LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' "
                          "OR lower(dir) LIKE ? ESCAPE '\\')")
        params += [f"%{e}%", f"%{e}%", f"%{e}%"]
    where.append("(" + " OR ".join(like_parts) + ")")

    if type_:
        exts = ["." + t.lower().lstrip(".") for t in type_.split(",") if t.strip()]
        if exts:
            where.append(f"ext IN ({','.join('?' * len(exts))})")
            params += exts
    if within:
        s, e = parse_within(within)
        where.append("mtime BETWEEN ? AND ?")
        params += [s, e]
    if new:
        s, e = parse_new(new)
        if e is None:
            where.append("first_seen >= ?")
            params.append(s)
        else:
            where.append("first_seen BETWEEN ? AND ?")
            params += [s, e]
    if dir_sub:
        where.append("dir LIKE ? ESCAPE '\\'")
        params.append(f"%{escape_like(dir_sub)}%")

    sql = ("SELECT path, name, ext, dir, size, mtime, first_seen, content, status "
           f"FROM files WHERE {' AND '.join(where)}")
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    # 年份分布（对全部命中行聚合，先于 year 过滤，保证分布图始终展示全量）
    by_year = Counter()
    for _p, _n, _e, _d, _s, mt, _f, _c, _st in rows:
        by_year[datetime.fromtimestamp(mt).year] += 1

    scored = []
    for path, name, ext, dirpath, size, mtime, first_seen, content, status in rows:
        if year is not None and datetime.fromtimestamp(mtime).year != year:
            continue
        # 中英文文件名筛选：按文件名是否含中文字符判定
        if name_lang:
            if (name_lang == "zh") != bool(_ZH_RE.search(name)):
                continue
        sc = score_row(name, dirpath, content, keywords, require_all)
        if sc < 0:
            continue
        scored.append({
            "path": path, "name": name, "ext": ext, "dir": dirpath,
            "size": size, "mtime": mtime, "first_seen": first_seen,
            "status": status, "score": sc,
            "snippets": make_snippets(content, keywords, context),
        })

    # 排序：score=匹配度优先（分数相同再看谁更新），time=修改时间优先
    if sort == "time":
        scored.sort(key=lambda r: (-r["mtime"], -r["score"]))
    else:
        scored.sort(key=lambda r: (-r["score"], -r["mtime"]))
    total = len(scored)
    # 匹配度百分比：本组结果内最高分为 100%，其余按分数占比直接换算
    max_score = max((r["score"] for r in scored), default=0)
    for r in scored:
        r["pct"] = min(100, round(r["score"] * 100 / max_score)) if max_score > 0 else 0
    return {
        "keywords": list(keywords), "mode": "all" if require_all else "any",
        "sort": sort, "year": year, "name_lang": name_lang,
        "by_year": [{"year": y, "n": n} for y, n in sorted(by_year.items(), reverse=True)],
        "total_hits": total, "returned": min(len(scored), limit),
        "results": scored[:limit],
    }


# ---------------- CLI ----------------

def _print_human(out):
    if not out["results"]:
        log(f"未找到与 {' + '.join(out['keywords'])} 相关的文件。")
        log("建议：")
        log("  1. 换更短、更核心的关键词（如把「锂电池热管理调研」拆成 热管理）")
        log("  2. 降低限定条件（去掉 --type/--within/--dir）")
        log("  3. 若文件是新存的，先运行 python indexer.py scan 更新索引")
        return
    log(f"按内容「{'、'.join(out['keywords'])}」找到 {out['total_hits']} 个相关文件"
        f"（模式：{'全部命中' if out['mode'] == 'all' else '任一命中'}），"
        f"按相关度显示前 {out['returned']} 个：\n")
    for i, r in enumerate(out["results"], 1):
        mt = datetime.fromtimestamp(r["mtime"]).strftime("%Y-%m-%d %H:%M")
        log(f"{i}. [匹配度 {r['pct']}%] {r['name']}  ({fmt_size(r['size'])} · 修改于 {mt})")
        log(f"   路径: {r['path']}")
        log(f"   状态: {STATUS_CN.get(r['status'], r['status'])} · 首次记录: {r['first_seen']}")
        for sn in r["snippets"]:
            log(f"   片段: …{sn}…")
        log("")
    log("提示：结果不符可换关键词 / 加 --all 要求全部命中 / 用 --within 限定时间范围。")


def main():
    ap = argparse.ArgumentParser(description="文档拾忆 · 搜索器")
    ap.add_argument("keywords", nargs="+", help="内容关键词（可多个，默认任一命中即返回）")
    ap.add_argument("--all", action="store_true", help="要求所有关键词都命中（默认任一命中）")
    ap.add_argument("--type", help="扩展名过滤，逗号分隔，如 docx,pdf,md")
    ap.add_argument("--within", help="修改时间过滤：7d / 2026-10-01 / 2026-10-01..2026-10-28")
    ap.add_argument("--new", help="只看某天首次收录：today / yesterday / 7d / 2026-10-27")
    ap.add_argument("--dir", help="路径包含子串过滤，如 Documents 或 D:\\论文")
    ap.add_argument("--limit", type=int, default=20, help="返回条数上限，默认 20")
    ap.add_argument("--context", type=int, default=40, help="命中片段上下文字符数，默认 40")
    ap.add_argument("--sort", choices=("score", "time"), default="score",
                    help="排序方式：score 相关度（默认）/ time 修改时间倒序")
    ap.add_argument("--year", type=int, default=None,
                    help="仅保留该自然年的文件，如 2024（结果仍带全量年份分布 by_year）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（便于程序处理）")
    args = ap.parse_args()

    try:
        out = run_search(args.keywords, require_all=args.all, type_=args.type,
                         within=args.within, new=args.new, dir_sub=args.dir,
                         limit=args.limit, context=args.context, sort=args.sort,
                         year=args.year)
    except (ValueError, FileNotFoundError) as exc:
        log(str(exc))
        sys.exit(2)

    if args.json:
        log(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_human(out)


if __name__ == "__main__":
    main()
