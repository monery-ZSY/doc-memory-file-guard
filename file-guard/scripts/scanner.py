#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件卫士 · 启发式风险扫描器
在本地检测「长得像病毒」的可执行伪装与广告/流氓软件痕迹。

规则覆盖：
  1. 双扩展名伪装        发票.pdf.exe / 照片.jpg.scr
  2. 高危脚本扩展名      .scr/.pif/.hta/.vbs/.js 等出现在用户目录
  3. 诱导性文件名        破解版/激活工具/注册机/外挂 等结合可执行
  4. 可疑快捷方式        .lnk 指向 powershell -enc / mshta / 隐藏 temp 目录
  5. 带宏文档            .docm/.xlsm/.pptm
  6. autorun.inf         各盘根目录 U 盘蠕虫残留
  7. 未签名程序          下载/桌面等来源不明的无数字签名 exe
  8. 可疑 PE 区段名      VMP/Themida 等加壳特征
  9. 启动项异常          Run 注册表指向 temp/公共可写目录
 10. hosts 篡改          非本地映射条目

用法:
  python scanner.py              # 重点目录快扫
  python scanner.py --full       # 全盘深扫
  python scanner.py --json       # JSON 输出
  python scanner.py --level high # 只看高危

模块复用:
  from scanner import run_scan
  run_scan("quick", on_progress=cb) → dict
"""

import argparse
import ctypes
import json
import os
import re
import struct
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(__file__).resolve().parent / "data"
try:
    # D 盘副本启动时共享 C 盘技能的 data 目录（与「文档拾忆」同机制）
    for line in (SCRIPT_DIR / "data_dir.txt").read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and Path(line).is_dir():
            DATA_DIR = Path(line)
            break
except OSError:
    pass

LEVEL_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3}
LEVEL_CN = {"high": "高危", "medium": "中危", "low": "低危", "info": "提示"}

# ---------- 规则元数据（人话解释） ----------
RULES = {
    "double_ext":     "双扩展名伪装：用文档/图片后缀做幌子，实际是可执行程序",
    "danger_ext":     "高危脚本/可执行扩展名出现在用户目录",
    "luring_name":    "诱导性文件名：破解/激活/外挂类命名常伴随捆绑与木马",
    "lnk_suspicious": "快捷方式指向可疑解释器或隐藏临时目录",
    "macro_doc":      "带宏文档：宏可执行任意代码，确认来源后再打开",
    "autorun":        "autorun.inf：U 盘蠕虫传播残留",
    "unsigned_pe":    "来源不明的未签名程序：无法验证发行方",
    "packed_pe":      "可执行文件带加壳特征（VMP/Themida 等），常见于混淆恶意代码",
    "run_key":        "启动项指向临时/公共可写目录：流氓软件常驻的典型手法",
    "hosts":          "hosts 文件含非本地映射条目：可能被推广/劫持软件篡改",
    "startup_dir":    "启动目录里的脚本/程序：开机自动执行，确认是否自己放的",
    "broken_file":    "文件结构破损：下载中断/存储损坏导致打不开，可尝试重建结构修复",
}

DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".txt", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".mp4",
            ".avi", ".mkv", ".zip", ".rar", ".7z", ".mp3"}
EXEC_EXTS = {".exe", ".scr", ".pif", ".hta", ".vbs", ".vbe", ".js", ".jse",
             ".wsf", ".wsh", ".bat", ".cmd", ".com", ".jar", ".ps1", ".lnk"}
DANGER_DELIVERY = {".scr", ".pif", ".hta", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh"}
DANGER_RARE = {".scr", ".pif", ".hta", ".jse", ".vbe", ".wsf", ".wsh"}
LURING_WORDS = ["破解", "激活", "注册机", "外挂", "辅助工具", "神器", "绿色版",
                "免费版", "全家桶", "卡密", "黑科技", "补丁"]
MACRO_EXTS = {".docm", ".xlsm", ".pptm", ".dotm", ".xlam"}
PE_EXTS = {".exe", ".dll", ".scr"}

# 破损检测：可修复格式（容器结构可重建）与仅检测格式（只提示）
BROKEN_REPAIRABLE = {".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".jar", ".odt", ".ods", ".odp"}
BROKEN_DETECT_ONLY = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
BROKEN_CHECK_EXTS = BROKEN_REPAIRABLE | BROKEN_DETECT_ONLY
BROKEN_MAX_SIZE = 200 * 1024 * 1024  # 超过 200MB 不做头尾校验（成本控制）

# 遍历时跳过的目录（无关或体量巨大）
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".vscode", "$RECYCLE.BIN",
             "System Volume Information", "vendor", "site-packages", ".temp"}
# 全盘模式下的系统信任区（正常软件所在地，不报脚本/未签名类规则）
TRUSTED_PREFIXES = ("c:\\windows\\", "c:\\program files\\", "c:\\program files (x86)\\",
                    "c:\\programdata\\microsoft\\")
# 自身与宿主运行时目录（避免扫到自己）
SELF_PREFIXES = [str(SCRIPT_DIR).lower()]


def fixed_drives():
    """本机固定磁盘盘符列表。"""
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
    return drives or ["C:\\"]


def quick_targets():
    """快扫目录与区域标签：downloads/desktop（投递位置）、user（用户目录）、startup（启动目录）。"""
    out, seen = [], set()

    def add(p, area):
        sp = str(p).lower()
        if sp not in seen and p.is_dir():
            seen.add(sp)
            out.append((p, area))

    up = os.environ.get("USERPROFILE")
    if up:
        b = Path(up)
        add(b / "Downloads", "downloads")
        add(b / "Desktop", "desktop")
        add(b / "Documents", "user")
        add(b / "Videos", "user")
        add(b / "Pictures", "user")
        add(b / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup", "startup")
        od = os.environ.get("OneDrive")
        if od:
            add(Path(od) / "Downloads", "downloads")
            add(Path(od) / "Desktop", "desktop")
            add(Path(od) / "Documents", "user")
    tmp = os.environ.get("TEMP")
    if tmp:
        add(Path(tmp), "user")
    allusers = os.environ.get("PROGRAMDATA")
    if allusers:
        add(Path(allusers) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup", "startup")
    return out


# ---------------- 检测原语 ----------------

def check_double_ext(name):
    m = re.search(r"\.([a-z0-9]{1,5})\.([a-z0-9]{1,5})$", name, re.IGNORECASE)
    if not m:
        return None
    outer, real = "." + m.group(1).lower(), "." + m.group(2).lower()
    if outer in DOC_EXTS and real in EXEC_EXTS:
        return f"双扩展名伪装（看似 {outer} 实为 {real}）"
    return None


def check_luring_name(name, ext):
    if ext not in EXEC_EXTS and ext not in PE_EXTS:
        return None
    for w in LURING_WORDS:
        if w in name.lower():
            return f"诱导性文件名（含「{w}」）"
    return None


def check_lnk(path, size):
    """读取 .lnk 二进制，粗提取目标与参数关键词。"""
    if size > 64 * 1024:
        return None
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    try:
        text = raw.decode("utf-16-le", errors="ignore").lower()
        # 叠加 ASCII 串（部分字段存单字节）
        text += raw.decode("latin-1", errors="ignore").lower()
    except Exception:
        return None
    hits = []
    high_kw = ("powershell", "mshta", "-encodedcommand", "-enc ", "wscript",
               "cscript", "cmd /c", "cmd.exe /c", "rundll32 javascript")
    for kw in high_kw:
        if kw in text:
            hits.append(f"目标调用 {kw.strip()}")
    if re.search(r"\\appdata\\local\\temp\\|\\temp\\|\\users\\public\\", text):
        hits.append("目标指向临时/公共可写目录")
    if hits:
        return "；".join(hits[:3])
    return None


# ---------------- Authenticode 签名验证（ctypes 调 wintrust） ----------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _WINTRUST_FILE_INFO(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", ctypes.c_wchar_p),
                ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p)]


class _WINTRUST_DATA(ctypes.Structure):
    _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                ("pSIPCallbackData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                ("pFile", ctypes.POINTER(_WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", ctypes.c_wchar_p),
                ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD)]


_WV_ACTION = _GUID(0x00AAC56B, 0xCD33, 0x11D0,
                   (ctypes.c_ubyte * 8)(0x8E, 0xD2, 0x00, 0xC0, 0x4F, 0xB6, 0x85, 0xAE))
_sig_cache = {}


def verify_authenticode(path):
    """True=有有效数字签名；False=无签名或无效。结果缓存。"""
    key = str(path)
    if key in _sig_cache:
        return _sig_cache[key]
    ok = None
    try:
        fi = _WINTRUST_FILE_INFO()
        fi.cbStruct = ctypes.sizeof(_WINTRUST_FILE_INFO)
        fi.pcwszFilePath = str(path)
        wd = _WINTRUST_DATA()
        wd.cbStruct = ctypes.sizeof(_WINTRUST_DATA)
        wd.dwUIChoice = 2           # WTD_UI_NONE
        wd.fdwRevocationChecks = 0  # WTD_REVOKE_NONE（离线可用）
        wd.dwUnionChoice = 1        # WTD_CHOICE_FILE
        wd.pFile = ctypes.pointer(fi)
        wd.dwStateAction = 1        # WTD_STATEACTION_VERIFY
        wt = ctypes.windll.wintrust
        ret = wt.WinVerifyTrust(0, ctypes.byref(_WV_ACTION), ctypes.byref(wd))
        ok = (ret == 0)
        wd.dwStateAction = 2        # WTD_STATEACTION_CLOSE
        wt.WinVerifyTrust(0, ctypes.byref(_WV_ACTION), ctypes.byref(wd))
    except Exception:
        ok = None
    _sig_cache[key] = ok
    return ok


# ---------------- PE 区段名（加壳粗查） ----------------

_PACKER_SECTIONS = {b".vmp0", b".vmp1", b".vmp2", b".themida", b".themid",
                    b"upx0", b"upx1", b"upx2", b".aspack", b".adata"}
_NORMAL_SECTIONS = {b".text", b".data", b".rdata", b".rsrc", b".reloc", b".bss",
                    b".idata", b".edata", b".tls", b".pdata", b".crt", b".didat",
                    b"UPX0", b"UPX1", b"UPX!", b".gfids", b".textb", b".idata2"}


def check_packed_pe(path, size):
    if size > 60 * 1024 * 1024:
        return None
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return None
            f.seek(0x3C)
            off = struct.unpack("<I", f.read(4))[0]
            f.seek(off)
            if f.read(4) != b"PE\x00\x00":
                return None
            f.seek(off + 6)
            n_sec = struct.unpack("<H", f.read(2))[0]
            f.seek(off + 20 + 240)  # 跳过可选头（按最大值），定位区段表
            weird = []
            for _ in range(min(n_sec, 96)):
                nm = f.read(8).rstrip(b"\x00")
                f.read(32)
                if not nm:
                    continue
                if nm.lower() in {s.lower() for s in _PACKER_SECTIONS}:
                    weird.append(nm.decode(errors="replace") + "（商业壳）")
                elif nm not in _NORMAL_SECTIONS and not nm.startswith(b"."):
                    weird.append(nm.decode(errors="replace"))
            if any("（商业壳）" in w for w in weird):
                return "区段名带商业壳特征：" + "、".join(weird[:4])
    except (OSError, struct.error):
        return None
    return None


# ---------------- 系统痕迹：启动项 / hosts / autorun ----------------

def _extract_path_from_cmd(cmd):
    """从启动项命令串提取疑似路径：优先取引号内，其次取首个含反斜杠/可执行后缀的 token。"""
    m = re.search(r'"([^"]+)"', cmd)
    if m:
        return m.group(1)
    for tok in cmd.split():
        if "\\" in tok or tok.lower().endswith((".exe", ".scr", ".hta", ".vbs", ".bat", ".cmd", ".dll")):
            return tok
    parts = cmd.split()
    return parts[0] if parts else cmd


def scan_run_keys(findings):
    """Run/RunOnce 启动项指向 temp/公共目录/高危扩展 → 高危。"""
    import winreg
    hive_map = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}
    roots = [("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
             ("HKCU", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
             ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"),
             ("HKLM", r"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce"),
             ("HKLM", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run")]
    for label, sub in roots:
        try:
            key = winreg.OpenKey(hive_map[label], sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, val, _t = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                cmd = os.path.expandvars(str(val or ""))
                raw_path = _extract_path_from_cmd(cmd)
                try:
                    rp = str(Path(raw_path).resolve()).lower()
                except Exception:
                    rp = raw_path.lower()
                reasons = []
                if re.search(r"\\appdata\\local\\temp\\|\\temp\\|\\users\\public\\", rp):
                    reasons.append("启动项指向临时/公共可写目录")
                if rp.endswith((".scr", ".hta", ".vbs", ".jse", ".pif")):
                    reasons.append("启动项指向高危可执行类型")
                if reasons:
                    _add(findings, rp, "high", "run_key", "；".join(reasons),
                         name=f"启动项「{name}」", reg=label)
        finally:
            winreg.CloseKey(key)


def scan_hosts(findings):
    hp = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
    try:
        lines = hp.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return
    bad = []
    for ln in lines:
        s = ln.split("#")[0].strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("127.0.0.1") or low.startswith("::1"):
            rest = low.split(None, 1)[1] if len(low.split(None, 1)) > 1 else ""
            if rest.strip() in ("localhost", "localhost.localdomain", ""):
                continue
        bad.append(s)
    if bad:
        preview = " / ".join(bad[:4]) + (f"（共 {len(bad)} 条）" if len(bad) > 4 else "")
        _add(findings, str(hp), "low", "hosts", f"非本地映射：{preview}")


def scan_autorun(findings, roots):
    for root in roots:
        inf = Path(root) / "autorun.inf"
        if inf.exists():
            _add(findings, str(inf), "high", "autorun", "autorun.inf 存在（U 盘蠕虫传播特征）")


# ---------------- 文件级规则（快扫 / 全盘共用） ----------------

def _other_image_sig(head):
    """头是另一种常见图片格式（如 WebP/PNG/GIF/BMP）：仅扩展名标错，不算破损。"""
    return ((head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head[:8] == b"\x89PNG\r\n\x1a\n"
            or head[:6] in (b"GIF87a", b"GIF89a")
            or head[:2] == b"BM")


def check_broken(path, ext, size):
    """结构校验：返回破损原因（人话）或 None。
    头尾校验读 1KB+64KB（成本恒定）；PDF 附加对象平衡深度校验（全文扫描，仅 ≤50MB）。"""
    if size < 16 or size > BROKEN_MAX_SIZE:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(1024)
            f.seek(max(0, size - 65536))
            tail = f.read(65600)
    except OSError:
        return None

    if ext == ".pdf":
        if b"%PDF-" not in head:
            return "PDF 头缺失：文件开头没有 PDF 标记（可能不是 PDF 或已被改写）"
        last = tail[-1024:] if len(tail) >= 1024 else tail
        if b"%%EOF" not in last:
            return "PDF 尾缺失：缺少结束标记（常见于下载中断/拷贝不完整），阅读器可能打不开"
        if b"startxref" not in last:
            return "PDF 交叉引用损坏：缺少 startxref 定位，可尝试重建修复"
        # 深度校验：对象/结束标记平衡（字节损坏常见表现）
        if size <= 50 * 1024 * 1024:
            try:
                with open(path, "rb") as f:
                    data = f.read()
                n_obj = len(re.findall(rb"\d+\s+\d+\s+obj\b", data))
                n_end = len(re.findall(rb"endobj\b", data))
                if n_obj and n_end < n_obj:
                    return (f"PDF 对象结构损坏：{n_obj - n_end} 个对象缺少 endobj 结束标记"
                            "（字节级损坏），可尝试重建修复")
            except OSError:
                pass
        return None

    if ext in (".zip", ".docx", ".xlsx", ".pptx", ".jar", ".odt", ".ods", ".odp"):
        if head[:2] != b"PK":
            return "ZIP 容器头缺失：开头不是 PK 签名（可能不是该格式或已被改写）"
        if b"PK\x05\x06" not in tail:
            return "ZIP 中央目录缺失：缺少 EOCD 结束标记（常见于下载中断/拷贝不完整），解压/打开会报错"
        return None

    if ext == ".png":
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            if _other_image_sig(head):
                return None
            return "PNG 头签名不匹配：可能已被改写或损坏"
        if b"IEND" not in tail[-16:]:
            return "PNG 尾缺失：缺少 IEND 块（图片可能打不开或显示不全）"
        return None

    if ext in (".jpg", ".jpeg"):
        if head[:2] != b"\xff\xd8":
            if _other_image_sig(head):
                return None
            return "JPEG 头签名不匹配：可能已被改写或损坏"
        if b"\xff\xd9" not in tail[-8:]:
            return "JPEG 尾缺失：缺少 EOI 结束标记（图片可能显示不全）"
        return None

    if ext == ".gif":
        if head[:6] not in (b"GIF87a", b"GIF89a"):
            if _other_image_sig(head):
                return None
            return "GIF 头签名不匹配：可能已被改写或损坏"
        return None

    if ext == ".bmp":
        if head[:2] != b"BM":
            if _other_image_sig(head):
                return None
            return "BMP 头签名不匹配：可能已被改写或损坏"
        return None
    return None


def check_file(fp: Path, ctx, findings):
    """ctx: {"area": downloads|desktop|user|startup|system}。"""
    name = fp.name
    try:
        st = fp.stat()
    except OSError:
        return
    ext = fp.suffix.lower()
    pstr = str(fp)
    plow = pstr.lower()
    area = ctx["area"]
    hit = []

    # 双扩展名伪装：系统信任区（Windows/Program Files）外的任何位置都算
    if area != "system" or not plow.startswith(TRUSTED_PREFIXES):
        r = check_double_ext(name)
        if r:
            hit.append(("double_ext", r, "high"))

    if area in ("downloads", "desktop"):
        # 投递位置：全部高危扩展按中危
        if ext in DANGER_DELIVERY:
            hit.append(("danger_ext", f"高危扩展名 {ext} 出现在下载/桌面", "medium"))
    elif area == "user":
        # 其他用户目录：仅罕见高危类型（js/vbs 在开发/应用目录过于常见，不在此区域报告；
        # 落地的持久化风险由启动项/Run 键规则兜底）
        if ext in DANGER_RARE:
            hit.append(("danger_ext", f"罕见高危扩展名 {ext}", "medium"))
    elif area == "startup":
        if ext in EXEC_EXTS and ext != ".lnk":
            hit.append(("startup_dir", f"启动目录里的 {ext}：开机自动执行，确认是否自己放的", "low"))

    if area != "system":
        if ext in MACRO_EXTS:
            hit.append(("macro_doc", f"带宏文档 {ext}", "info"))
        r = check_luring_name(name, ext)
        if r:
            hit.append(("luring_name", r, "medium"))
        if ext == ".lnk":
            r = check_lnk(pstr, st.st_size)
            if r:
                hit.append(("lnk_suspicious", r, "medium"))

    # 破损检测：用户区中小于 200MB 的常见格式做头尾结构校验
    # （~$ 开头是 Office 打开文档时的锁文件，非真实文档，跳过）
    if (area != "system" and ext in BROKEN_CHECK_EXTS
            and st.st_size <= BROKEN_MAX_SIZE and not name.startswith("~$")):
        r = check_broken(pstr, ext, st.st_size)
        if r:
            if ext in BROKEN_REPAIRABLE:
                hit.append(("broken_file", r + "，可尝试重建结构修复（另存新文件，不动原件）", "medium"))
            else:
                hit.append(("broken_file", r + "（该格式仅检测提示，无法自动修复）", "medium"))

    # 未签名 + 加壳：仅投递位置的小型 PE（控制耗时与误报）
    if ext in PE_EXTS and area in ("downloads", "desktop") and st.st_size < 60 * 1024 * 1024:
        sig = verify_authenticode(pstr)
        luring = any(w in name.lower() for w in LURING_WORDS)
        if sig is False:
            lvl = "medium" if luring else "low"
            hit.append(("unsigned_pe", "无数字签名（无法验证发行方）" + ("，且文件名具诱导性" if luring else ""), lvl))
        r = check_packed_pe(pstr, st.st_size)
        if r:
            hit.append(("packed_pe", r, "low"))

    for rule_id, reason, level in hit:
        _add(findings, pstr, level, rule_id, reason)


# ---------------- 结果聚合 ----------------

def _add(findings, path, level, rule_id, reason, name=None, reg=None):
    f = findings.setdefault(str(path), {"path": str(path), "name": name or Path(path).name,
                                        "size": None, "mtime": None, "level": level,
                                        "reasons": [], "rules": []})
    if LEVEL_ORDER[level] > LEVEL_ORDER[f["level"]]:
        f["level"] = level
    entry = reason
    if entry not in f["reasons"]:
        f["reasons"].append(entry)
        f["rules"].append(rule_id)
    if reg is not None:
        f["reg"] = reg


def walk(root: Path, area, findings, counter, on_progress):
    """递归遍历指定区域。"""
    ctx = {"area": area}
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in SKIP_DIRS:
                                continue
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        counter[0] += 1
                        if counter[0] % 200 == 0 and on_progress:
                            on_progress(counter[0], entry.path)
                        check_file(Path(entry.path), ctx, findings)
                        if len(findings) > 2000:
                            return
                    except OSError:
                        continue
        except OSError:
            continue


# ---------------- 主入口 ----------------

def run_scan(mode="quick", on_progress=None):
    """mode: "quick" 重点目录快扫 / "full" 全盘深扫。
    返回 dict：mode/start/end/duration/scanned/errors/findings(list)。"""
    t0 = time.time()
    findings = {}
    counter = [0]
    errors = []

    # 系统痕迹（两种模式都查）
    try:
        scan_run_keys(findings)
    except Exception as e:
        errors.append(f"启动项扫描失败: {e}")
    try:
        scan_hosts(findings)
    except Exception as e:
        errors.append(f"hosts 检查失败: {e}")

    drives = fixed_drives()
    try:
        scan_autorun(findings, drives)
    except Exception as e:
        errors.append(f"autorun 检查失败: {e}")

    if mode == "quick":
        for t, area in quick_targets():
            if on_progress:
                on_progress(counter[0], str(t))
            walk(t, area, findings, counter, on_progress)
        # 各盘根目录散落的可执行（不入递归，只看第一层）
        for d in drives:
            try:
                for entry in os.scandir(d):
                    if entry.is_file(follow_symlinks=False):
                        counter[0] += 1
                        check_file(Path(entry.path), {"area": "user"}, findings)
            except OSError:
                pass
    else:
        for d in drives:
            if on_progress:
                on_progress(counter[0], d)
            walk(Path(d), "system", findings, counter, on_progress)
        # 全盘模式下，用户目录区域再按用户区规则跑一遍
        for t, area in quick_targets():
            walk(t, area, findings, counter, on_progress)

    out = sorted(findings.values(),
                 key=lambda f: (-LEVEL_ORDER[f["level"]], f["path"]))
    # 补充 stat 信息（聚合时未取的）
    for f in out:
        if f["size"] is None:
            try:
                st = Path(f["path"]).stat()
                f["size"], f["mtime"] = st.st_size, st.st_mtime
            except OSError:
                pass
    duration = round(time.time() - t0, 1)
    result = {
        "mode": mode, "start": datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M:%S"),
        "duration": duration, "scanned": counter[0], "errors": errors,
        "counts": {lv: sum(1 for f in out if f["level"] == lv) for lv in LEVEL_ORDER},
        "findings": out,
    }
    _save_history(result)
    return result


def _save_history(result):
    """保留最近 20 次扫描摘要到 data/scan_history.json。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        hp = DATA_DIR / "scan_history.json"
        hist = []
        if hp.exists():
            try:
                hist = json.loads(hp.read_text(encoding="utf-8"))[:19]
            except (OSError, ValueError):
                hist = []
        hist.insert(0, {"mode": result["mode"], "start": result["start"],
                        "duration": result["duration"], "scanned": result["scanned"],
                        "counts": result["counts"]})
        hp.write_text(json.dumps(hist, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# ---------------- CLI ----------------

def _print_human(out, min_level="info"):
    thr = LEVEL_ORDER[min_level]
    c = out["counts"]
    print(f"扫描完成（{('重点目录快扫' if out['mode'] == 'quick' else '全盘深扫')}，"
          f"耗时 {out['duration']}s，检查 {out['scanned']} 个文件）")
    print(f"发现：高危 {c['high']} · 中危 {c['medium']} · 低危 {c['low']} · 提示 {c['info']}")
    print("定位：启发式扫描是第二道防线，不能替代 Windows Defender；建议两者并用。\n")
    shown = [f for f in out["findings"] if LEVEL_ORDER[f["level"]] >= thr]
    if not shown:
        print("（当前级别过滤下没有需要关注的项目）")
        return
    for f in shown[:200]:
        mt = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M") if f["mtime"] else "-"
        sz = f"{f['size']/1048576:.1f}MB" if f["size"] and f["size"] > 1048576 else (
            f"{f['size']/1024:.0f}KB" if f["size"] else "-")
        print(f"[{LEVEL_CN[f['level']]}] {f['name']}  ({sz} · {mt})")
        print(f"    路径: {f['path']}")
        for r in f["reasons"]:
            print(f"    原因: {r}")
    if len(shown) > 200:
        print(f"…另有 {len(shown) - 200} 条未显示（--json 查看全部）")


def main():
    ap = argparse.ArgumentParser(description="文件卫士 · 启发式风险扫描器")
    ap.add_argument("--full", action="store_true", help="全盘深扫（默认重点目录快扫）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--level", choices=LEVEL_ORDER.keys(), default="info",
                    help="人类可读输出的最低级别过滤")
    args = ap.parse_args()

    def cb(n, path):
        print(f"  … {n} 个文件，当前: {path}", end="\r", flush=True)

    out = run_scan("full" if args.full else "quick", on_progress=cb if not args.json else None)
    if not args.json:
        print("\n")
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        _print_human(out, args.level)


if __name__ == "__main__":
    main()
