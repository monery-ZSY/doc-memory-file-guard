#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件卫士 · 破损文件修复引擎（尽力修复，不保证完整）
原理：重建容器结构（索引/目录），保住未损坏部分的内容。
  - PDF：扫描全部对象 → 重建交叉引用表(xref) + trailer → 追加到原内容后
  - ZIP/docx/xlsx/pptx：扫描全部本地文件头 → 重建中央目录 + EOCD
重要约束：
  - 原文件永远不动，修复结果另存为「原名(修复).扩展名」
  - 无法凭空恢复已丢失的字节：损坏点所在内容可能缺失
  - PNG/JPEG 等仅头尾校验格式不做修复
"""

import re
import struct
import zipfile
from pathlib import Path

# 可修复扩展名（与 scanner.BROKEN_REPAIRABLE 保持一致）
REPAIRABLE = {".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".jar", ".odt", ".ods", ".odp"}


def _out_path(fp: Path):
    """生成不冲突的输出路径：原名(修复).ext → (修复2) → (修复3)…"""
    stem, ext = fp.stem, fp.suffix
    cand = fp.with_name(f"{stem}(修复){ext}")
    n = 2
    while cand.exists():
        cand = fp.with_name(f"{stem}(修复{n}){ext}")
        n += 1
    return cand


# ---------------- PDF 修复 ----------------

def repair_pdf(fp: Path):
    """重建 xref 表与 trailer：追加到原字节之后（原内容原样保留）。"""
    data = fp.read_bytes()
    if b"%PDF-" not in data[:1024]:
        return {"error": "文件头没有 PDF 标记，不是可识别的 PDF"}

    # 1. 扫描所有对象：『N M obj』
    objs = {}
    for m in re.finditer(rb"(\d+)\s+\d+\s+obj\b", data):
        num = int(m.group(1))
        if num not in objs:          # 重复编号取首个
            objs[num] = m.start()
    if not objs:
        return {"error": "未找到任何 PDF 对象，内容损坏过于严重"}

    # 2. 找 Root（Catalog）：优先用原 trailer 的声明，否则扫描 /Type /Catalog
    root = None
    trailers = re.findall(rb"trailer\s*<<(.{0,3000}?)>>", data, re.S)
    encrypt = False
    if trailers:
        t = trailers[-1]
        m = re.search(rb"/Root\s+(\d+)\s+\d+\s+R", t)
        if m:
            root = int(m.group(1))
        encrypt = b"/Encrypt" in t
    if root is None or root not in objs:
        for num, off in objs.items():
            chunk = data[off:off + 512]
            if re.search(rb"/Type\s*/Catalog", chunk):
                root = num
                break
    if root is None:
        return {"error": "找不到文档目录对象（Catalog），无法重建"}

    # 3. 重建：原字节 + 新 xref + 新 trailer
    out = bytearray(data)
    if not out.endswith(b"\n"):
        out += b"\n"
    max_num = max(objs)
    xref_off = len(out)
    lines = [b"xref", b"0 %d" % (max_num + 1), b"0000000000 65535 f "]
    for i in range(1, max_num + 1):
        if i in objs:
            lines.append(b"%010d 00000 n " % objs[i])
        else:
            lines.append(b"0000000000 65535 f ")
    out += b"\n".join(lines) + b"\n"
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\n" % (max_num + 1, root)
    out += b"startxref\n%d\n%%%%EOF\n" % xref_off

    # 4. 自验：结构完整性粗检
    if out.count(b" obj") == 0 or b"%%EOF" not in out[-64:]:
        return {"error": "重建后自验未通过，修复失败"}

    dst = _out_path(fp)
    dst.write_bytes(bytes(out))
    notes = [f"重建交叉引用表（{len(objs)} 个对象）",
             "原文件未改动，请打开新文件确认内容完整"]
    if encrypt:
        notes.append("该 PDF 带加密，打开时仍需原密码")
    return {"ok": True, "new_path": str(dst), "objects": len(objs),
            "notes": notes}


# ---------------- ZIP 容器修复（docx/xlsx/pptx 同为 ZIP） ----------------

_LFH = struct.Struct(b"<4sHHHHHIIIHH")     # local file header(除文件名)
_CDH = struct.Struct(b"<4sHHHHHHIIIHHHHHII")  # central directory header(除文件名)


def repair_zip(fp: Path):
    """扫描全部本地文件头，重建中央目录与 EOCD，追加到原字节之后。"""
    data = fp.read_bytes()
    if data[:2] != b"PK":
        return {"error": "文件头不是 PK 签名，不是可识别的 ZIP 容器"}

    # 1. 扫描所有 local file header
    entries = []
    for m in re.finditer(re.escape(b"PK\x03\x04"), data):
        p = m.start()
        if p + 30 > len(data):
            continue
        try:
            (_sig, ver, flag, method, tm, dt, crc, csize, usize,
             nlen, elen) = _LFH.unpack(data[p:p + 30])
        except struct.error:
            continue
        if nlen == 0 or nlen > 512:
            continue
        name = data[p + 30:p + 30 + nlen]
        entries.append({"lho": p, "ver": ver, "flag": flag, "method": method,
                        "tm": tm, "dt": dt, "crc": crc, "csize": csize,
                        "usize": usize, "name": name})
    if not entries:
        return {"error": "未找到任何 ZIP 条目，内容损坏过于严重"}

    # 去重（同一偏移只算一次）并按偏移排序
    entries.sort(key=lambda e: e["lho"])

    # 2. 重建中央目录
    cd = bytearray()
    dd_count = 0
    for e in entries:
        if e["flag"] & 0x8:             # data descriptor 模式：local 头里 size/crc 为 0
            dd_count += 1
        cd += _CDH.pack(b"PK\x01\x02", 0x14, e["ver"], e["flag"], e["method"],
                        e["tm"], e["dt"], e["crc"], e["csize"], e["usize"],
                        len(e["name"]), 0, 0, 0, 0, 0, e["lho"])
        cd += e["name"]
    cd_off = len(data)
    eocd = (b"PK\x05\x06" + struct.pack(b"<HHHHIIH", 0, 0,
                                        len(entries), len(entries),
                                        len(cd), cd_off, 0))

    # 3. 输出：原字节 + 新 CD + 新 EOCD
    dst = _out_path(fp)
    dst.write_bytes(data + bytes(cd) + eocd)

    # 4. 自验：标准库能列出条目
    try:
        names = zipfile.ZipFile(str(dst)).namelist()
        ok_names = len(names)
    except Exception:
        return {"error": "重建后自验未通过（标准库仍无法解析），修复失败"}
    if ok_names == 0:
        return {"error": "重建后条目数为 0，修复失败"}

    notes = [f"重建中央目录（{ok_names} 个条目）",
             "原文件未改动，请打开新文件确认内容完整"]
    if dd_count:
        notes.append(f"{dd_count} 个条目使用后置校验（CRC/大小未知），个别文件解压时可能报错")
    return {"ok": True, "new_path": str(dst), "entries": ok_names, "notes": notes}


# ---------------- 统一入口 ----------------

def repair_file(path):
    """按扩展名分派修复。返回 {ok,new_path,notes,...} 或 {error}。"""
    fp = Path(path)
    if not fp.is_file():
        return {"error": "文件不存在"}
    ext = fp.suffix.lower()
    if ext not in REPAIRABLE:
        return {"error": f"{ext} 格式仅支持破损检测，暂不支持自动修复"}
    try:
        if ext == ".pdf":
            return repair_pdf(fp)
        return repair_zip(fp)
    except OSError as exc:
        return {"error": "读写失败：" + str(exc)}


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        print(arg, "->", repair_file(arg))
