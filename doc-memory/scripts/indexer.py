#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文档拾忆 · 索引器
扫描本机常用目录与本地磁盘，提取文件正文，建立/增量更新本地 SQLite 搜索索引。
零外部依赖（纯 Python 标准库；若 scripts/vendor/ 下内置 pypdf，则额外支持 PDF 正文）。
索引数据保存在本脚本同目录的 data/ 下。

用法:
  python indexer.py scan  [--roots "路径1;路径2"] [--report] [--max-size-mb 20] [--quiet]
  python indexer.py status
  python indexer.py rescan-ext pdf[,docx]   # 只重新提取指定类型的正文（不重走全盘）
"""

import argparse
import ctypes
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from pathlib import Path

# 抑制 pypdf 字体解析的刷屏警告
logging.getLogger("pypdf").setLevel(logging.ERROR)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent


def _resolve_data_dir():
    """数据目录解析：若脚本同目录存在 data_dir.txt（一行路径）则使用该目录作为共享数据目录，
    否则使用脚本同目录下的 data/。用于让多份代码副本（如参赛备份）共享同一份索引。
    容错：平台可能向文件行尾追加水印尾注（如「AI生成」），逐行解析并剥离行尾尾注，
    仅接受以盘符开头的真实路径。"""
    link = SCRIPT_DIR / "data_dir.txt"
    if link.is_file():
        try:
            raw = link.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raw = ""
        for line in raw.splitlines():
            s = line.strip().strip('"').strip()
            if not s or not re.match(r"^[A-Za-z]:\\", s):
                continue
            while s:
                if Path(s).is_dir():
                    return Path(s)
                # 剥离行尾可能的「AI…」类水印尾注（仅当剥离后仍是路径时才继续）
                s2 = re.sub(r"\s*AI\S*\s*$", "", s)
                if s2 == s or not s2:
                    break
                s = s2.rstrip()
    return SCRIPT_DIR / "data"


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "index.db"
CONFIG_PATH = DATA_DIR / "config.json"
SNAPSHOT_DIR = DATA_DIR / "snapshots"

# 内置第三方依赖目录（pypdf 等，pip --target 安装，不影响全局环境）
VENDOR_DIR = SCRIPT_DIR / "vendor"
if VENDOR_DIR.is_dir():
    sys.path.insert(0, str(VENDOR_DIR))

DOC_MAX_CHARS = 12000           # 文档类（txt/md/docx/pdf…）最多索引正文前 1.2 万字符
CODE_MAX_CHARS = 4000           # 代码/数据类（py/js/hpp/json…）截断更短，控体积
REPORT_MAX_ROWS = 200           # 每日快照报告最多列出的文件条数

# ---------- 可提取正文的文件类型 ----------

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".log", ".json", ".xml",
    ".html", ".htm", ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".go", ".rs", ".sql", ".r", ".m", ".tex", ".bib",
    ".ini", ".cfg", ".conf", ".yaml", ".yml", ".bat", ".ps1", ".sh", ".srt",
    ".lrc", ".vbs", ".ipynb",
}
DOCX_EXTS = {".docx"}
PPTX_EXTS = {".pptx"}
XLSX_EXTS = {".xlsx"}
PDF_EXTS = {".pdf"}
# v1.3.0：图片走本地 OCR（Windows 自带引擎，识别结果作为可搜索正文）
IMAGE_OCR_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".gif"}
OCR_SCRIPT = SCRIPT_DIR / "ocr.ps1"
OCR_TIMEOUT = 20        # 单张图片 OCR 限时（秒）
OCR_MAX_BYTES = 8 * 1024 * 1024   # 超过 8MB 的图不 OCR（大图耗时过长）

# 代码/数据/标记类：截断更短（CODE_MAX_CHARS），其余文档类用 DOC_MAX_CHARS
CODE_TRUNC_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".sql", ".r", ".m", ".ipynb", ".bat", ".ps1",
    ".sh", ".vbs", ".json", ".xml", ".html", ".htm", ".ini", ".cfg",
    ".conf", ".yaml", ".yml", ".csv", ".tsv", ".log",
}


def trunc_limit(ext):
    """按类型返回正文截断上限。"""
    return CODE_MAX_CHARS if ext in CODE_TRUNC_EXTS else DOC_MAX_CHARS

SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "windows", "program files",
    "program files (x86)", "programdata", "$windows.~bt", "$windows.~ws",
    "recovery", "perflogs", "appdata",
    "node_modules", ".git", ".svn", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".cache", ".npm", ".nuget", ".gradle", ".m2",
    "anaconda3", "miniconda3", "site-packages", "__macosx",
    # v1.1.0：补充 Python 发行版/包缓存（不带版本号命名）与常见缓存目录
    "anaconda", "anaconda2", "miniconda", "miniforge", "mambaforge", "pkgs",
    "conda-meta", ".conda", "dist-info", "__pypackages__",
}

# v1.1.0：默认忽略的扩展名——纯二进制/安装包/字体/字节码，对"按内容找回文档"零价值，
# 收录只会膨胀索引（实测 .dll×7千+.enc×6千+.pyi×6千 占数万条）。不收录文件名也不记。
DEFAULT_IGNORE_EXTS = {
    # 可执行/库/构建产物
    ".exe", ".dll", ".sys", ".drv", ".efi", ".so", ".dylib", ".lib", ".a",
    ".o", ".obj", ".pyd", ".pdb", ".bin",
    # 安装包/磁盘镜像
    ".msi", ".msix", ".appx", ".appxbundle", ".cab", ".wim", ".iso",
    ".img", ".dmg", ".vhd", ".vdi",
    # 压缩包
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".jar",
    ".war", ".apk", ".ipa", ".whl", ".egg", ".nupkg",
    # 字体
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # 加密/缓存/字节码/类型存根（IDE 自带 typeshed 数以千计）
    ".enc", ".pyc", ".pyo", ".pyi", ".class", ".dek", ".dat",
}

# 运行时生效的忽略集合（DEFAULT + config.json 里用户自定义追加），resolve_roots 时合并
_active_skip_dirs = set(SKIP_DIR_NAMES)
_active_ignore_exts = set(DEFAULT_IGNORE_EXTS)
# 本次扫描被扩展名规则跳过的文件计数（仅日志展示用）
_ignored_file_count = 0


def log(msg):
    print(msg, flush=True)


# ---------- 默认扫描范围 ----------

def fixed_fixed_drives():
    """返回本机固定磁盘盘符列表（DRIVE_FIXED=3）。"""
    drives = []
    try:
        bits = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if bits >> i & 1:
                letter = chr(ord("A") + i) + ":\\"
                if ctypes.windll.kernel32.GetDriveTypeW(letter) == 3:
                    drives.append(letter)
    except Exception:
        pass
    return drives


def default_roots():
    """探测默认扫描范围：用户常用目录 + 微信/QQ 文件目录 + 非C盘固定磁盘。"""
    user = Path.home()
    candidates = [
        user / "Documents", user / "Desktop", user / "Downloads",
        user / "Documents" / "WeChat Files",
        user / "Documents" / "xwechat_files",
        user / "Documents" / "Tencent Files",
        user / "Documents" / "WXWork Files",
    ]
    for d in fixed_fixed_drives():
        if not d.upper().startswith("C"):
            candidates.append(Path(d))
    return [str(p) for p in candidates if p.exists()]


def _norm_ext(e):
    """把用户写的扩展名归一为带点小写（"dll"→".dll"）。"""
    e = e.strip().lower()
    return e if e.startswith(".") else "." + e


def resolve_roots(roots_arg):
    """解析扫描范围：优先命令行参数，其次已有配置，最后自动探测并持久化。
    v1.1.0：同时读取 config.json 的 ignore_dirs / ignore_exts（用户自定义追加，
    与内置忽略集合合并生效），写回时保留这两个字段。"""
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    if roots_arg:
        roots = [s.strip() for s in re.split(r"[;；]", roots_arg) if s.strip()]
    else:
        roots = cfg.get("roots", [])
    if not roots:
        roots = default_roots()
    # 合并用户自定义忽略项（大小写不敏感）
    for d in cfg.get("ignore_dirs", []):
        _active_skip_dirs.add(str(d).lower())
    for e in cfg.get("ignore_exts", []):
        _active_ignore_exts.add(_norm_ext(str(e)))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = {"roots": roots}
    if cfg.get("ignore_dirs"):
        out["ignore_dirs"] = cfg["ignore_dirs"]
    if cfg.get("ignore_exts"):
        out["ignore_exts"] = cfg["ignore_exts"]
    CONFIG_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    return roots


# ---------- 文本提取 ----------

def sanitize_text(t):
    """清洗无法写入 SQLite 的孤立代理字符等非法码点（PDF 提取常见）。"""
    if not t:
        return t
    try:
        t.encode("utf-8")
        return t
    except UnicodeEncodeError:
        return t.encode("utf-8", "ignore").decode("utf-8", "ignore")


def read_text_file(path, max_bytes=2_000_000):
    """读取纯文本文件，返回文本；疑似二进制返回 None。"""
    try:
        with open(path, "rb") as f:
            raw = f.read(max_bytes)
    except OSError:
        return None
    if not raw:
        return ""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    if raw.count(0) > len(raw) * 0.1:          # 大量 0 字节 → UTF-16 或二进制
        if raw[:1] not in (b"\x00",):
            try:
                return raw.decode("utf-16-le", "replace")
            except Exception:
                return None
        return None
    if raw[:4096].count(b"\x00") > 0:          # 疑似二进制
        return None
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _w(name):
    return "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}" + name


def extract_docx(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(_w("p")):
        texts = [t.text or "" for t in p.iter(_w("t"))]
        line = "".join(texts).strip()
        if line:
            paras.append(line)
    return "\n".join(paras)


_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def extract_pptx(path):
    parts = []
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist()
                         if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for n in slides:
            root = ET.fromstring(z.read(n))
            texts = [t.text or "" for t in root.iter(_A + "t")]
            parts.append("\n".join(t for t in texts if t.strip()))
    return "\n".join(parts)


def extract_xlsx(path):
    shared = []
    with zipfile.ZipFile(path) as z:
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.iter(_S + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
        parts = []
        for n in sorted(z.namelist()):
            if not re.match(r"xl/worksheets/sheet\d+\.xml$", n):
                continue
            root = ET.fromstring(z.read(n))
            for c in root.iter(_S + "c"):
                t = c.get("t")
                if t == "s":
                    v = c.find(_S + "v")
                    if v is not None and v.text and v.text.isdigit():
                        idx = int(v.text)
                        if idx < len(shared):
                            parts.append(shared[idx])
                elif t == "inlineStr":
                    parts.append("".join(x.text or "" for x in c.iter(_S + "t")))
                else:
                    v = c.find(_S + "v")
                    if v is not None and v.text:
                        parts.append(v.text)
    return "\n".join(parts)


def extract_pdf(path, max_pages=40):
    """PDF 提取：优先使用已安装的 pypdf / PyPDF2 / fitz；都没有则返回 None（仅索引元数据）。
    只提取前 max_pages 页，避免大 PDF 解析过慢（关键词多在文档前部）。"""
    for mod in ("pypdf", "PyPDF2", "fitz"):
        try:
            m = __import__(mod)
        except ImportError:
            continue
        try:
            if mod == "fitz":
                doc = m.open(str(path))
                texts = [page.get_text() for page in doc]
                doc.close()
            else:
                reader = m.PdfReader(str(path))
                pages = reader.pages[:max_pages]
                texts = [(page.extract_text() or "") for page in pages]
            text = "\n".join(t for t in texts if t and t.strip())
            return text or None
        except Exception:
            return None
    return None


def _normalize_ocr_text(t):
    """OCR 结果后处理：Windows OCR 会在字符间插空格（“热 失 控”“45 ． 6”），
    去掉与中文/数字相邻的空格并把全角标点转半角，保证关键词可命中。"""
    if not t:
        return t
    t = t.replace("\uff0e", ".").replace("\uff0c", ",").replace("\uff1a", ":")
    t = re.sub(r"(?<=\d)\s+\.", ".", t)                          # "45 ." → "45."
    t = re.sub(r"\.\s+(?=\d)", ".", t)                           # ". 6"  → ".6"
    t = re.sub(r"(?<=[\u4e00-\u9fff\d])\s+(?=[\u4e00-\u9fff\d])", "", t)  # 中文/数字相邻空格
    return t.strip()


def extract_image_ocr(path):
    """调用 Windows 自带 OCR 识别图片文字（本地识别，不出网）。
    成功返回文本（可能为空串），失败/无引擎返回 None。"""
    if not OCR_SCRIPT.exists():
        return None
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass",
             "-File", str(OCR_SCRIPT), "-ImagePath", str(path)],
            capture_output=True, timeout=OCR_TIMEOUT)
        if proc.returncode != 0:
            return None
        out = proc.stdout.decode("utf-8", "replace").strip()
        return _normalize_ocr_text(out) if out else ""
    except Exception:
        return None


def extract_content(path, ext, max_bytes, allow_ocr=True):
    """按类型提取正文。返回 (content, status)。"""
    try:
        if ext in TEXT_EXTS:
            t = read_text_file(path, max_bytes * 1024 * 1024)
            return (sanitize_text(t), "text") if t is not None else (None, "name-only")
        if ext in IMAGE_OCR_EXTS and allow_ocr:
            # 增量扫描时对新图片做本地 OCR；首次全量跳过（数万张存量图全 OCR 耗时过长）
            try:
                if Path(path).stat().st_size <= OCR_MAX_BYTES:
                    t = extract_image_ocr(path)
                    if t:
                        return sanitize_text(t), "text"
            except OSError:
                pass
            return None, "name-only"
        if ext in DOCX_EXTS:
            return sanitize_text(extract_docx(path)), "text"
        if ext in PPTX_EXTS:
            return sanitize_text(extract_pptx(path)), "text"
        if ext in XLSX_EXTS:
            return sanitize_text(extract_xlsx(path)), "text"
        if ext in PDF_EXTS:
            t = extract_pdf(path)
            return (sanitize_text(t), "text") if t else (None, "name-only")
    except Exception:
        return None, "name-only"
    return None, "name-only"


# ---------- 数据库 ----------

def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            name TEXT, ext TEXT, dir TEXT,
            size INTEGER, mtime REAL,
            first_seen TEXT, last_seen TEXT,
            content TEXT, status TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_first_seen ON files(first_seen)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")


def skip_dir(name):
    n = name.lower()
    return n in _active_skip_dirs or n.startswith(".")


def skip_file(fp):
    """v1.1.0：按扩展名忽略纯二进制文件（dll/安装包/字体等，无文本价值）。"""
    return fp.suffix.lower() in _active_ignore_exts


def iter_files(root):
    global _ignored_file_count
    rp = Path(root)
    if not rp.exists():
        return
    if rp.is_file():
        yield rp
        return
    for dirpath, dirnames, filenames in os.walk(rp):
        dirnames[:] = [d for d in dirnames if not skip_dir(d)]
        for fn in filenames:
            fp = Path(dirpath) / fn
            if skip_file(fp):
                _ignored_file_count += 1
                continue
            yield fp


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------- 每日快照报告 ----------

def write_snapshot_report(conn, is_first_scan):
    today = date.today().isoformat()
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    report = SNAPSHOT_DIR / f"{today}.md"

    new_rows = conn.execute(
        "SELECT path, size, mtime, status FROM files WHERE first_seen=? ORDER BY mtime DESC",
        (today,)).fetchall()
    mod_rows = conn.execute(
        "SELECT path, size, mtime, status FROM files "
        "WHERE first_seen<? AND mtime>=? ORDER BY mtime DESC",
        (today, time.mktime(time.strptime(today, "%Y-%m-%d")))).fetchall()

    lines = [f"# 每日文件快照 · {today}", ""]
    if is_first_scan:
        lines.append("> 本次为首次全量扫描，当前清单作为初始基线，之后的快照只记录当天新增/修改的文件。")
        lines.append("")
    lines.append(f"- 新增文件：{len(new_rows)} 个")
    lines.append(f"- 当天修改：{len(mod_rows)} 个")
    lines.append("")

    def add_section(title, rows):
        lines.append(f"## {title}")
        lines.append("")
        if not rows:
            lines.append("（无）")
            lines.append("")
            return
        lines.append("| 文件名 | 位置 | 大小 | 修改时间 | 状态 |")
        lines.append("|---|---|---|---|---|")
        for p, size, mtime, status in rows[:REPORT_MAX_ROWS]:
            fp = Path(p)
            status_cn = {"text": "已提取正文", "name-only": "仅文件名"}.get(status, status)
            lines.append(f"| {fp.name} | {fp.parent} | {fmt_size(size)} | "
                         f"{datetime.fromtimestamp(mtime):%m-%d %H:%M} | {status_cn} |")
        if len(rows) > REPORT_MAX_ROWS:
            lines.append(f"| …… | 其余 {len(rows) - REPORT_MAX_ROWS} 个略 | | | |")
        lines.append("")

    add_section("今天新增的文件", new_rows)
    add_section("今天修改过的文件", mod_rows)
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


# ---------- 命令 ----------

def cmd_scan(args):
    t0 = time.time()
    roots = resolve_roots(args.roots)
    max_bytes = args.max_size_mb
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)

    first_scan = conn.execute(
        "SELECT COUNT(*) FROM files").fetchone()[0] == 0

    if not args.quiet:
        log("扫描范围：")
        for r in roots:
            log(f"  {r}")

    known = {row[0]: (row[1], row[2])
             for row in conn.execute("SELECT path, size, mtime FROM files")}
    conn.execute("CREATE TEMP TABLE seen_paths (path TEXT PRIMARY KEY)")

    today = date.today().isoformat()
    stats = {"scanned": 0, "added": 0, "updated": 0, "skipped": 0, "name_only": 0}
    buf = []

    def flush():
        if buf:
            conn.executemany("""
                INSERT INTO files (path,name,ext,dir,size,mtime,first_seen,last_seen,content,status)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    name=excluded.name, ext=excluded.ext, dir=excluded.dir,
                    size=excluded.size, mtime=excluded.mtime, last_seen=excluded.last_seen,
                    content=excluded.content, status=excluded.status
            """, buf)
            buf.clear()

    for fp in _walk_all(roots):
        sp = str(fp)
        try:
            st = fp.stat()
        except OSError:
            # 文件损坏/无法访问（如磁盘错误文件）：静默跳过，避免系统弹窗中断批量扫描
            stats.setdefault("corrupt_skipped", 0)
            stats["corrupt_skipped"] = stats.get("corrupt_skipped", 0) + 1
            continue
        if st.st_size > max_bytes * 1024 * 1024:
            content, status = None, "name-only"
            need_extract = False
        else:
            need_extract = True

        old = known.get(sp)
        if old and not first_scan and need_extract:
            if old[0] == st.st_size and abs(old[1] - st.st_mtime) < 2.0:
                stats["skipped"] += 1
                conn.execute("UPDATE files SET last_seen=? WHERE path=?", (today, sp))
                conn.execute("INSERT OR IGNORE INTO seen_paths VALUES (?)", (sp,))
                continue

        content, status = (None, "name-only")
        if need_extract:
            ext = fp.suffix.lower()
            content, status = extract_content(sp, ext, max_bytes,
                                             allow_ocr=not first_scan)
            if content:
                content = content[:trunc_limit(ext)]
            else:
                status = "name-only"

        if status == "name-only":
            stats["name_only"] += 1

        buf.append((sp, fp.name, fp.suffix.lower(), str(fp.parent),
                    st.st_size, st.st_mtime, today, today,
                    content, status))
        if old:
            stats["updated"] += 1
        else:
            stats["added"] += 1
        conn.execute("INSERT OR IGNORE INTO seen_paths VALUES (?)", (sp,))
        stats["scanned"] += 1
        if stats["scanned"] % 1000 == 0:
            flush()
            if not args.quiet:
                log(f"  ... 已处理 {stats['scanned']} 个文件")

    flush()

    removed = conn.execute(
        "SELECT COUNT(*) FROM files WHERE path NOT IN (SELECT path FROM seen_paths)"
    ).fetchone()[0]
    if removed:
        conn.execute("DELETE FROM files WHERE path NOT IN (SELECT path FROM seen_paths)")

    conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_scan', ?)",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('roots', ?)",
                 (json.dumps(roots, ensure_ascii=False),))
    conn.commit()

    report_path = None
    if args.report:
        report_path = write_snapshot_report(conn, first_scan)

    log("")
    log(f"索引完成，耗时 {time.time() - t0:.1f} 秒")
    log(f"  本次扫描 {stats['scanned']} 个文件：新增 {stats['added']}，更新 {stats['updated']}，"
        f"未变化跳过 {stats['skipped']}")
    log(f"  其中已提取正文 {stats['scanned'] - stats['name_only']} 个，仅记文件名 {stats['name_only']} 个")
    if _ignored_file_count:
        log(f"  按忽略规则跳过二进制/依赖文件 {_ignored_file_count} 个（不收录）")
    if removed:
        log(f"  清理已不存在/移出范围的记录 {removed} 条")
    if report_path:
        log(f"  每日快照报告：{report_path}")
    conn.close()
    return 0


def _walk_all(roots):
    for r in roots:
        yield from iter_files(r)


def cmd_status(args):
    return _print_status()


def _print_status():
    if not DB_PATH.exists():
        log("索引尚未建立，请先运行: python indexer.py scan")
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    with_text = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='text'").fetchone()[0]
    last_scan = conn.execute(
        "SELECT value FROM meta WHERE key='last_scan'").fetchone()
    roots_row = conn.execute(
        "SELECT value FROM meta WHERE key='roots'").fetchone()
    by_type = conn.execute(
        "SELECT ext, COUNT(*) FROM files GROUP BY ext ORDER BY COUNT(*) DESC LIMIT 12"
    ).fetchall()
    today_new = conn.execute(
        "SELECT COUNT(*) FROM files WHERE first_seen=?",
        (date.today().isoformat(),)).fetchone()[0]

    log(f"索引文件：{DB_PATH}（{fmt_size(DB_PATH.stat().st_size) if DB_PATH.exists() else 0}）")
    log(f"已收录文件：{total} 个，其中已提取正文 {with_text} 个")
    if today_new:
        log(f"今天新增：{today_new} 个")
    if last_scan:
        log(f"上次扫描：{last_scan[0]}")
    if roots_row:
        log("扫描范围：" + " ; ".join(json.loads(roots_row[0])))
    # v1.1.0：显示生效的忽略规则（先读 config 合并，与 scan 保持一致）
    cfg = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    for d in cfg.get("ignore_dirs", []):
        _active_skip_dirs.add(str(d).lower())
    for e in cfg.get("ignore_exts", []):
        _active_ignore_exts.add(_norm_ext(str(e)))
    custom_dirs = len(_active_skip_dirs) - len(SKIP_DIR_NAMES)
    custom_exts = len(_active_ignore_exts) - len(DEFAULT_IGNORE_EXTS)
    log(f"忽略规则：目录 {len(_active_skip_dirs)} 类（含自定义 {custom_dirs}），"
        f"扩展名 {len(_active_ignore_exts)} 类（含自定义 {custom_exts}）—— 二进制/依赖库不收录")
    log("文件类型分布（前12）：" + ", ".join(f"{e or '(无扩展名)'}×{c}" for e, c in by_type))
    conn.close()
    return 0


def cmd_rescan_ext(args):
    """只重新提取指定扩展名的正文（如内置 pypdf 后补扫 PDF），不重走全盘。"""
    if not DB_PATH.exists():
        log("索引尚未建立，请先运行: python indexer.py scan")
        return 1
    exts = ["." + e.lower().lstrip(".") for e in args.ext.split(",") if e.strip()]
    if not exts:
        log("未指定扩展名，如: python indexer.py rescan-ext pdf")
        return 1
    t0 = time.time()
    conn = sqlite3.connect(str(DB_PATH))
    init_db(conn)
    rows = conn.execute(
        f"SELECT path, ext, size FROM files WHERE ext IN ({','.join('?' * len(exts))})",
        exts).fetchall()
    log(f"待重扫 {len(rows)} 个 {','.join(exts)} 文件（当前正文中 {sum(1 for r in rows if r[1])} 条记录）")

    ok = fail = gone = 0
    buf = []
    for idx, (path, ext, size) in enumerate(rows, 1):
        if not Path(path).exists():
            gone += 1
            continue
        if size > args.max_size_mb * 1024 * 1024:
            content, status = None, "name-only"
        else:
            content, status = extract_content(path, ext, args.max_size_mb)
            if content:
                content = content[:trunc_limit(ext)]
            else:
                status = "name-only"
        if status == "text":
            ok += 1
        else:
            fail += 1
        buf.append((content, status, path))
        if len(buf) >= 200:
            conn.executemany(
                "UPDATE files SET content=?, status=? WHERE path=?", buf)
            conn.commit()
            buf.clear()
        if idx % 50 == 0:
            log(f"  ... {idx}/{len(rows)}")
    if buf:
        conn.executemany("UPDATE files SET content=?, status=? WHERE path=?", buf)
    conn.commit()
    conn.close()
    log(f"重扫完成，耗时 {time.time() - t0:.1f} 秒：提取正文 {ok} 个，仅文件名 {fail} 个，"
        f"文件已不存在 {gone} 个")
    return 0


def main():
    ap = argparse.ArgumentParser(description="文档拾忆 · 索引器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="扫描并建立/更新索引")
    sp.add_argument("--roots", help="扫描范围，分号分隔，如 \"C:\\Users\\me\\Documents;D:\\\"")
    sp.add_argument("--report", action="store_true", help="生成今日快照报告（每日快照用）")
    sp.add_argument("--max-size-mb", type=float, default=20,
                    help="超过该大小(MB)的文件只记文件名，默认 20")
    sp.add_argument("--quiet", action="store_true", help="精简输出")
    sp.set_defaults(func=cmd_scan)

    st = sub.add_parser("status", help="查看索引统计")
    st.set_defaults(func=cmd_status)

    rs = sub.add_parser("rescan-ext", help="只重新提取指定类型正文（如 pdf），不重扫全盘")
    rs.add_argument("ext", help="扩展名，逗号分隔，如 pdf 或 pdf,docx")
    rs.add_argument("--max-size-mb", type=float, default=20)
    rs.set_defaults(func=cmd_rescan_ext)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
