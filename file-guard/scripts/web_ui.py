#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件卫士 · 本地 Web 界面
仅监听 127.0.0.1:8766，数据不出本机。提供快扫/全盘扫描、风险清单、
一键送回收站（可还原）、扫描历史。

用法: python web_ui.py [--port 8766] [--no-browser]
"""

import argparse
import json
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import scanner  # noqa: E402
import repair  # noqa: E402

PORT = 8766
HOSTS_PATH = Path("C:\\Windows\\System32\\drivers\\etc\\hosts")

# ---------- 全局扫描状态 ----------
SCAN = {
    "status": "idle",        # idle | running | done
    "mode": None,
    "start_ts": 0,
    "start": "",
    "scanned": 0,
    "current": "",
    "result": None,          # run_scan 的完整结果
}


def _ignored_set():
    try:
        return set(json.loads((scanner.DATA_DIR / "ignored.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _save_ignored(ig):
    try:
        scanner.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (scanner.DATA_DIR / "ignored.json").write_text(
            json.dumps(sorted(ig), ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _scan_worker(mode):
    def cb(n, path):
        SCAN["scanned"] = n
        SCAN["current"] = path

    try:
        result = scanner.run_scan(mode, on_progress=cb)
        SCAN["result"] = result
        SCAN["status"] = "done"
    except Exception as exc:  # 扫描线程兜底：任何异常都不允许悬挂
        SCAN["status"] = "done"
        SCAN["result"] = {"mode": mode, "start": SCAN["start"], "duration": 0,
                          "scanned": SCAN["scanned"], "errors": [f"扫描异常: {exc}"],
                          "counts": {"high": 0, "medium": 0, "low": 0, "info": 0},
                          "findings": []}


# ---------- API 实现 ----------

def api_state():
    r = SCAN["result"]
    out = {
        "status": SCAN["status"], "mode": SCAN["mode"],
        "start_ts": SCAN["start_ts"], "start": SCAN["start"],
        "scanned": SCAN["scanned"], "current": SCAN["current"],
        "running": SCAN["status"] == "running",
    }
    if r:
        out["counts"] = r.get("counts", {})
        out["duration"] = r.get("duration")
        out["errors"] = r.get("errors", [])
        out["done_scanned"] = r.get("scanned")
    return out


def api_results():
    """结果列表（应用忽略名单，并标注可隔离性）。"""
    if SCAN["status"] != "done" or not SCAN["result"]:
        return {"error": "尚无扫描结果，请先扫描"}
    ig = _ignored_set()
    quarantined = _quarantined_set()
    repaired = _repaired_map()
    items = []
    for f in SCAN["result"].get("findings", []):
        p = f["path"]
        if p in ig and p not in quarantined:
            continue
        is_hosts = p.lower() == str(HOSTS_PATH).lower()
        can_q = (not f.get("reg")) and (not is_hosts) and Path(p).is_file()
        broken = "broken_file" in (f.get("rules") or [])
        can_fix = broken and (Path(p).suffix.lower() in repair.REPAIRABLE) and Path(p).is_file()
        items.append({
            "path": p, "name": f.get("name"), "level": f["level"],
            "reasons": f.get("reasons", []), "rules": f.get("rules", []),
            "size": f.get("size"), "mtime": f.get("mtime"),
            "reg": f.get("reg"), "is_hosts": is_hosts,
            "can_quarantine": can_q,
            "quarantined": p in quarantined,
            "broken": broken, "can_repair": can_fix,
            "repaired_to": repaired.get(p) if p in repaired else None,
        })
    return {"items": items, "counts": {lv: sum(1 for i in items if i["level"] == lv)
                                       for lv in ("high", "medium", "low", "info")}}


def _quarantined_set():
    try:
        return set(json.loads((scanner.DATA_DIR / "quarantined.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def _repaired_map():
    """原路径 → 最近一次修复产物路径。"""
    try:
        return json.loads((scanner.DATA_DIR / "repaired.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_repaired(m):
    try:
        scanner.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (scanner.DATA_DIR / "repaired.json").write_text(
            json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _save_quarantined(q):
    try:
        scanner.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (scanner.DATA_DIR / "quarantined.json").write_text(
            json.dumps(sorted(q)[-500:], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def api_scan(body):
    if SCAN["status"] == "running":
        return {"error": "扫描进行中，请稍候"}
    mode = body.get("mode") or "quick"
    if mode not in ("quick", "full"):
        return {"error": "mode 仅支持 quick/full"}
    SCAN.update({"status": "running", "mode": mode, "start_ts": time.time(),
                 "start": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "scanned": 0, "current": "", "result": None})
    threading.Thread(target=_scan_worker, args=(mode,), daemon=True).start()
    return {"ok": True, "mode": mode}


def api_quarantine(body):
    """把文件送入 Windows 回收站（可还原）。"""
    import ctypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint),
                    ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    paths = body.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return {"error": "缺少路径"}
    q = _quarantined_set()
    results = []
    for p in paths:
        p = str(p).strip()
        fp = Path(p)
        if not p or fp.is_dir():
            results.append({"path": p, "error": "文件不存在或为目录"})
            continue
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = 3                    # FO_DELETE
        op.pFrom = str(fp) + "\x00"     # 双零结尾
        op.pTo = None
        op.fFlags = 0x40 | 0x10 | 0x4   # ALLOWUNDO | NOCONFIRMATION | SILENT
        try:
            res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        except OSError as exc:
            results.append({"path": p, "error": "移入回收站失败：" + str(exc)})
            continue
        if res != 0:
            results.append({"path": p, "error": f"移入回收站失败（代码 {res}）"})
            continue
        q.add(p)
        results.append({"path": p, "ok": True, "name": fp.name})
    _save_quarantined(q)
    ok = sum(1 for r in results if r.get("ok"))
    return {"ok": ok > 0, "count": ok, "total": len(results), "results": results}


def api_ignore(body):
    p = (body.get("path") or "").strip()
    if not p:
        return {"error": "缺少路径"}
    ig = _ignored_set()
    ig.add(p)
    _save_ignored(ig)
    return {"ok": True, "count": len(ig)}


def api_repair(body):
    """授权修复：重建结构另存新文件，原文件不动。"""
    p = (body.get("path") or "").strip()
    if not p:
        return {"error": "缺少路径"}
    fp = Path(p)
    if not fp.is_file():
        return {"error": "文件不存在"}
    out = repair.repair_file(p)
    if out.get("ok"):
        m = _repaired_map()
        m[p] = out["new_path"]
        _save_repaired(m)
    return out


def api_open_folder(body):
    p = (body.get("path") or "").strip()
    if not p or not Path(p).exists():
        return {"error": "路径不存在"}
    import ctypes
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "open", "explorer.exe", '/select,"' + p + '"', None, 1)
        return {"ok": ret > 32}
    except Exception:
        return {"error": "打开失败"}


def api_open_recyclebin():
    subprocess.Popen(["explorer", "shell:RecycleBinFolder"])
    return {"ok": True}


def api_history():
    try:
        return {"items": json.loads((scanner.DATA_DIR / "scan_history.json").read_text(encoding="utf-8"))[:10]}
    except (OSError, ValueError):
        return {"items": []}


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "FileGuard/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            self._send(200, api_state())
        elif u.path == "/api/results":
            self._send(200, api_results())
        elif u.path == "/api/history":
            self._send(200, api_history())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/api/scan":
            self._send(200, api_scan(body))
        elif u.path == "/api/quarantine":
            self._send(200, api_quarantine(body))
        elif u.path == "/api/repair":
            self._send(200, api_repair(body))
        elif u.path == "/api/ignore":
            self._send(200, api_ignore(body))
        elif u.path == "/api/open_folder":
            self._send(200, api_open_folder(body))
        elif u.path == "/api/open_recyclebin":
            self._send(200, api_open_recyclebin())
        else:
            self._send(404, {"error": "not found"})


# ================= 页面 =================

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>文件卫士 · 本地风险扫描</title>
<style>
:root{
  --pri-deep:#173a5e; --pri:#2f6da8; --pri-b:#a8c9e6; --pri-l:#e8f4fd;
  --line:#e3eaf2; --sub:#6b7a8c; --bg:#f4f8fc;
  --hi:#d9534f; --hi-bg:#fdecea; --hi-deep:#c9302c;
  --md:#f0a04b; --md-bg:#fef3e2;
  --lo:#5a8fd8; --lo-bg:#eaf2fc;
  --inf:#8a94a0; --inf-bg:#f0f3f6;
  --ok:#3a9a5f; --ok-bg:#e8f7ee;
  --r:14px;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:var(--bg);color:#2b3644;
  min-height:100vh;font-size:14px}

/* ===== 顶栏 ===== */
header{background:linear-gradient(120deg,#0e2a47 0%,#173a5e 45%,#2f6da8 100%);
  color:#fff;padding:0 0 46px;position:relative;overflow:hidden}
.hwrap{max-width:980px;margin:0 auto;padding:22px 22px 0;position:relative;z-index:2;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{width:46px;height:46px;flex-shrink:0}
.title h1{font-size:21px;font-weight:700;letter-spacing:1px;display:flex;align-items:center;gap:8px}
.title p{font-size:12px;opacity:.75;margin-top:3px}
.hbadges{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap}
.hbadges span{font-size:11px;padding:4px 11px;border-radius:12px;
  background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);backdrop-filter:blur(4px)}
/* 流星 */
.meteor{position:absolute;background:linear-gradient(90deg,transparent,#bfe0ff,transparent);
  height:1.5px;width:120px;opacity:.55;border-radius:2px;animation:shoot 5.5s linear infinite;z-index:1}
.meteor:nth-child(1){top:26%;animation-delay:0s}
.meteor:nth-child(2){top:52%;width:90px;animation-delay:2.1s}
.meteor:nth-child(3){top:72%;width:140px;animation-delay:3.8s;opacity:.35}
.meteor:nth-child(4){top:12%;width:70px;animation-delay:1.4s;opacity:.4}
@keyframes shoot{from{transform:translateX(-160px)}to{transform:translateX(1100px)}}
.star{position:absolute;width:3px;height:3px;border-radius:50%;background:#cfe4f7;
  animation:twinkle 3s ease-in-out infinite;z-index:1}
@keyframes twinkle{0%,100%{opacity:.15}50%{opacity:.8}}

main{max-width:980px;margin:-32px auto 40px;padding:0 22px;position:relative;z-index:3}

/* ===== 扫描控制卡 ===== */
.scan-card{background:rgba(255,255,255,.97);backdrop-filter:blur(12px);
  border-radius:var(--r);padding:24px 26px;box-shadow:0 10px 36px rgba(23,58,94,.16);
  border:1px solid #fff}
.scan-head{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.scan-head h2{font-size:16.5px;color:var(--pri-deep)}
.scan-head .sub{font-size:12px;color:var(--sub)}
.scan-btns{display:flex;gap:14px;flex-wrap:wrap}
.scanbtn{flex:1;min-width:260px;border:none;cursor:pointer;border-radius:12px;padding:16px 18px;
  display:flex;align-items:center;gap:14px;text-align:left;transition:.18s;color:#fff}
.scanbtn .bic{width:46px;height:46px;border-radius:12px;background:rgba(255,255,255,.18);
  display:flex;align-items:center;justify-content:center;flex-shrink:0}
.scanbtn .bic svg{width:26px;height:26px;stroke:#fff;stroke-width:1.8;fill:none;
  stroke-linecap:round;stroke-linejoin:round}
.scanbtn b{display:block;font-size:15px;margin-bottom:3px}
.scanbtn small{font-size:11.5px;opacity:.85;line-height:1.5;display:block}
.btn-quick{background:linear-gradient(135deg,#1d4e7e,#2f6da8);box-shadow:0 5px 16px rgba(36,86,140,.32)}
.btn-full{background:linear-gradient(135deg,#7a4a1d,#c07a2e);box-shadow:0 5px 16px rgba(192,122,46,.3)}
.scanbtn:hover{transform:translateY(-2px);filter:brightness(1.06)}
.scanbtn:disabled{opacity:.55;cursor:not-allowed;transform:none}
.hint{margin-top:13px;font-size:11.5px;color:var(--sub);line-height:1.7;
  background:var(--pri-l);border-radius:9px;padding:9px 13px}
.hint b{color:var(--pri)}

/* ===== 进度区 ===== */
.progress-box{display:none;margin-top:18px}
.progress-box.show{display:block}
.pbar{height:10px;border-radius:6px;background:var(--pri-l);overflow:hidden;position:relative}
.pbar .fill{position:absolute;left:-40%;width:40%;height:100%;border-radius:6px;
  background:linear-gradient(90deg,var(--pri-b),var(--pri));animation:slide 1.2s ease-in-out infinite}
@keyframes slide{to{left:100%}}
.pmeta{display:flex;gap:16px;margin-top:9px;font-size:12px;color:var(--sub);flex-wrap:wrap}
.pmeta b{color:var(--pri)}
.pcur{font-size:11px;color:var(--sub);margin-top:6px;font-family:Consolas,monospace;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:.8}

/* ===== 结果区 ===== */
.result-area{display:none;margin-top:20px}
.result-area.show{display:block}
.sumline{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.sumline .st{font-size:14px;color:var(--pri-deep);font-weight:600}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{font-size:12px;padding:5px 13px;border-radius:14px;font-weight:600;
  border:1px solid transparent}
.chip.hi{background:var(--hi-bg);color:var(--hi-deep);border-color:#f3c6c4}
.chip.md{background:var(--md-bg);color:#b26a1b;border-color:#f3d9b3}
.chip.lo{background:var(--lo-bg);color:var(--pri);border-color:#c9ddf3}
.chip.inf{background:var(--inf-bg);color:#6b7681;border-color:#d9dfe5}
.chip.ok{background:var(--ok-bg);color:var(--ok);border-color:#c2e5cf}
.batch-btn{margin-left:auto;border:none;border-radius:10px;padding:8px 16px;font-size:12.5px;
  font-weight:600;cursor:pointer;color:#fff;background:linear-gradient(135deg,#c9302c,#e05252);
  box-shadow:0 4px 12px rgba(201,48,44,.3);display:none}
.batch-btn.show{display:inline-block}
.batch-btn:hover{filter:brightness(1.07)}

.tabs{display:flex;gap:8px;margin:6px 0 14px;flex-wrap:wrap}
.tabs span{padding:7px 20px;font-size:13px;cursor:pointer;color:var(--sub);border-radius:20px;
  border:1px solid var(--line);background:#fff;transition:.15s}
.tabs span.on{color:#fff;background:linear-gradient(135deg,var(--pri-deep),var(--pri));
  border-color:transparent;font-weight:600;box-shadow:0 3px 10px rgba(36,86,140,.25)}

.finding{background:#fff;border:1px solid var(--line);border-radius:var(--r);
  padding:14px 17px 14px 20px;margin-bottom:12px;position:relative;overflow:hidden;
  animation:cardIn .3s ease both}
@keyframes cardIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.finding::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px}
.finding[data-l="high"]::before{background:var(--hi)}
.finding[data-l="medium"]::before{background:var(--md)}
.finding[data-l="low"]::before{background:var(--lo)}
.finding[data-l="info"]::before{background:var(--inf)}
.finding.quarantined{opacity:.75}
.ftop{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.fname{font-size:14px;font-weight:600;word-break:break-all}
.lbadge{font-size:10.5px;font-weight:700;padding:2px 10px;border-radius:9px;flex-shrink:0}
.lbadge.high{background:var(--hi-bg);color:var(--hi-deep)}
.lbadge.medium{background:var(--md-bg);color:#b26a1b}
.lbadge.low{background:var(--lo-bg);color:var(--pri)}
.lbadge.info{background:var(--inf-bg);color:#6b7681}
.lbadge.broken{background:#f6e9dc;color:#a05a2c;border:1px solid #ecd9c4}
.chip.brk{background:#f6e9dc;color:#a05a2c;border-color:#ecd9c4}
.rtag{font-size:11.5px;color:var(--ok);background:var(--ok-bg);
  border:1px solid #c2e5cf;border-radius:8px;padding:3px 11px;font-weight:600}
.act.fix{border-color:#b5e0c4;color:var(--ok)}
.act.fix:hover{background:var(--ok-bg)}
.qtag{margin-left:auto;font-size:11.5px;color:var(--ok);background:var(--ok-bg);
  border:1px solid #c2e5cf;border-radius:8px;padding:3px 11px;font-weight:600}
.reasons{margin-top:9px;display:flex;flex-direction:column;gap:5px}
.reason{font-size:12px;color:#4d5a68;line-height:1.6;display:flex;gap:7px;align-items:flex-start}
.reason .ric{width:14px;height:14px;flex-shrink:0;margin-top:2px;stroke-width:2;fill:none;
  stroke-linecap:round;stroke-linejoin:round}
.finding[data-l="high"] .ric{stroke:var(--hi)}
.finding[data-l="medium"] .ric{stroke:#d98a2b}
.finding[data-l="low"] .ric{stroke:var(--lo)}
.finding[data-l="info"] .ric{stroke:var(--inf)}
.fmeta{font-size:11px;color:var(--sub);margin-top:9px;display:flex;gap:14px;flex-wrap:wrap}
.oprow{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}
.fpath{flex:1;min-width:220px;font-size:11px;color:var(--pri-deep);background:var(--pri-l);
  border-radius:7px;padding:6px 11px;font-family:Consolas,monospace;word-break:break-all}
.act{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--pri-b);background:#fff;
  color:var(--pri);border-radius:8px;padding:5px 12px;font-size:11.5px;cursor:pointer;
  transition:.15s;white-space:nowrap}
.act svg{width:13px;height:13px;stroke-width:1.8;fill:none;stroke:currentColor;
  stroke-linecap:round;stroke-linejoin:round}
.act:hover{background:var(--pri-l)}
.act.danger{border-color:#eab6b4;color:var(--hi-deep)}
.act.danger:hover{background:var(--hi-bg)}
.act.done{background:var(--ok-bg);border-color:#b5e0c4;color:var(--ok)}
.regnote{font-size:11.5px;color:#b26a1b;background:var(--md-bg);border-radius:8px;
  padding:6px 11px;line-height:1.6;margin-top:9px}
.more-btn{display:block;margin:16px auto;border:1px solid var(--pri-b);background:#fff;
  color:var(--pri);border-radius:10px;padding:9px 26px;font-size:13px;cursor:pointer}
.more-btn:hover{background:var(--pri-l)}
.empty{background:#fff;border:1px dashed var(--pri-b);border-radius:var(--r);
  padding:38px 20px;text-align:center;color:var(--sub);font-size:13px;line-height:2}

/* ===== 历史 ===== */
.history{display:none;margin-top:22px}
.history.show{display:block}
.hcard{background:#fff;border:1px solid var(--line);border-radius:var(--r);padding:18px 22px}
.hcard h3{font-size:14.5px;color:var(--pri-deep);margin-bottom:12px;display:flex;gap:8px;align-items:center}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:var(--sub);font-weight:600;padding:7px 9px;border-bottom:1px solid var(--line)}
td{padding:8px 9px;border-bottom:1px solid #f0f4f8;color:#3d4a58}
tr:last-child td{border-bottom:none}
.c-red{color:var(--hi-deep);font-weight:600}
.c-org{color:#b26a1b}
.c-blu{color:var(--pri)}
.c-gry{color:#6b7681}

footer{text-align:center;font-size:11.5px;color:var(--sub);padding:8px 20px 34px;line-height:1.9}
footer code{background:#e9eff6;border-radius:4px;padding:1px 6px;font-size:11px}

/* ===== 守卫吉祥物 ===== */
.guard-zone{position:fixed;right:22px;bottom:18px;z-index:60;width:150px;user-select:none}
.guard-bubble{background:rgba(255,255,255,.94);border:1.5px solid var(--pri-b);border-radius:12px;
  padding:8px 11px;font-size:11.5px;color:#3d4a58;line-height:1.6;margin-bottom:9px;
  box-shadow:0 4px 16px rgba(23,58,94,.12);min-height:36px}
.guard{width:96px;height:104px;margin:0 auto;cursor:pointer;transition:.2s;
  filter:drop-shadow(0 8px 16px rgba(36,86,140,.25))}
.guard:hover{transform:translateY(-3px)}
.guard svg{width:100%;height:100%}
.guard .body{fill:url(#gbody)}
.guard [state="alert"] .body,
.guard.alert .body{fill:url(#galert)}
.guard .eyes circle{fill:#173a5e}
.guard .blink{animation:blink 4.2s infinite}
@keyframes blink{0%,94%,100%{transform:scaleY(1)}96%,98%{transform:scaleY(.08)}}
.guard .float{animation:gfloat 3.2s ease-in-out infinite}
@keyframes gfloat{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
.guard .radar{transform-origin:60px 14px;animation:spin 2.4s linear infinite;display:none}
.guard.scanning .radar{display:block}
.guard .scanline{display:none}
.guard.scanning .scanline{display:block;animation:sweep 1.4s ease-in-out infinite}
@keyframes sweep{0%,100%{transform:translateY(0)}50%{transform:translateY(42px)}}
@keyframes spin{to{transform:rotate(360deg)}}
.guard.alert{animation:shake .5s ease-in-out infinite}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-3px)}75%{transform:translateX(3px)}}
.guard-cap{text-align:center;font-size:10.5px;color:var(--sub);margin-top:5px;
  background:rgba(255,255,255,.85);border-radius:8px;padding:2px 8px;width:fit-content;margin:5px auto 0}

@media (max-width:640px){
  .scanbtn{min-width:100%}
  .hbadges{margin-left:0}
  .guard-zone{right:10px;bottom:10px;width:110px}
}
</style>
</head>
<body>
<header>
  <span class="meteor"></span><span class="meteor"></span><span class="meteor"></span><span class="meteor"></span>
  <span class="star" style="top:18%;left:30%"></span>
  <span class="star" style="top:40%;left:70%"></span>
  <span class="star" style="top:64%;left:22%"></span>
  <span class="star" style="top:30%;left:52%"></span>
  <div class="hwrap">
    <svg class="logo" viewBox="0 0 48 48">
      <defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="#7fc3ff"/><stop offset="100%" stop-color="#2f6da8"/></linearGradient></defs>
      <path d="M24 3 L41 10 V24 C41 34 33 42 24 45 C15 42 7 34 7 24 V10 Z" fill="url(#lg)"/>
      <path d="M16.5 23.5 L22 29 L32 18.5" stroke="#fff" stroke-width="3.4" fill="none"
        stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
    <div class="title">
      <h1>文件卫士</h1>
      <p>TeleAgent 技能 · 病毒伪装与广告捆绑的本地启发式扫描</p>
    </div>
    <div class="hbadges"><span>仅本机 127.0.0.1</span><span>数据不出电脑</span></div>
  </div>
</header>

<main>
  <div class="scan-card">
    <div class="scan-head">
      <h2>风险扫描</h2>
      <span class="sub">病毒伪装 · 广告捆绑 · 启动项/hosts 篡改 · 文件破损检测与修复</span>
    </div>
    <div class="scan-btns">
      <button class="scanbtn btn-quick" id="btnQuick" onclick="startScan('quick')">
        <div class="bic"><svg viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></div>
        <div><b>重点快扫</b><small>下载 / 桌面 / 文档 / 临时 / 启动项等病毒常出没位置，秒级完成</small></div>
      </button>
      <button class="scanbtn btn-full" id="btnFull" onclick="startScan('full')">
        <div class="bic"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/><line x1="11" y1="8" x2="11" y2="14"/><line x1="8" y1="11" x2="14" y2="11"/></svg></div>
        <div><b>全盘深扫</b><small>所有固定磁盘 + 系统痕迹（启动项 / hosts / autorun），耗时数分钟</small></div>
      </button>
    </div>
    <div class="hint">
      定位说明：启发式规则抓「长得像病毒/广告捆绑」的可疑特征，并检测 PDF/ZIP/docx 等<b>文件破损</b>（可授权修复，另存新文件不动原件、不保证 100% 完整）；是 <b>Windows Defender 之外的第二道防线</b>，
      不能替代专业杀软；隔离 = 送入系统回收站，误报可随时还原。
    </div>
    <div class="progress-box" id="progressBox">
      <div class="pbar"><div class="fill"></div></div>
      <div class="pmeta">
        <span>已检查 <b id="pCount">0</b> 个文件</span>
        <span>耗时 <b id="pElapsed">0s</b></span>
        <span id="pMode">快扫进行中</span>
      </div>
      <div class="pcur" id="pCur"></div>
    </div>
  </div>

  <div class="result-area" id="resultArea">
    <div class="sumline">
      <span class="st" id="sumTitle">扫描完成</span>
      <div class="chips" id="chips"></div>
      <button class="batch-btn" id="batchBtn" onclick="batchHigh()">一键隔离全部高危</button>
    </div>
    <div class="tabs" id="levelTabs">
      <span class="on" data-l="all" onclick="setLevel('all')">全部</span>
      <span data-l="high" onclick="setLevel('high')">高危</span>
      <span data-l="medium" onclick="setLevel('medium')">中危</span>
      <span data-l="low" onclick="setLevel('low')">低危</span>
      <span data-l="info" onclick="setLevel('info')">提示</span>
    </div>
    <div id="list"></div>
    <button class="more-btn" id="moreBtn" onclick="renderMore()" style="display:none">加载更多</button>
  </div>

  <div class="history" id="historyBox">
    <div class="hcard">
      <h3><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#2f6da8" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>扫描历史（最近 10 次）</h3>
      <table>
        <thead><tr><th>时间</th><th>模式</th><th>检查文件</th><th>耗时</th><th>高危</th><th>中危</th><th>低危</th><th>提示</th></tr></thead>
        <tbody id="histBody"></tbody>
      </table>
    </div>
  </div>
</main>

<footer>
  引擎：TeleAgent 技能「文件卫士」（纯 Python 标准库 · 本地规则引擎 · 签名验证调用 Windows wintrust）<br>
  建议与 Windows Defender 并用 · 隔离的文件都在系统回收站，可随时还原 ·
  命令行可用 <code>python scanner.py</code>（<code>--full</code> 全盘）·
  对 TeleAgent 说「帮我扫一下电脑有没有可疑文件」也可以触发
</footer>

<!-- ===== 守卫吉祥物 ===== -->
<div class="guard-zone">
  <div class="guard-bubble" id="guardSay">你好呀！我是安全卫士，先来一次快扫吧～</div>
  <div class="guard" id="guardEl" onclick="guardTap()">
    <svg viewBox="0 0 120 130">
      <defs>
        <linearGradient id="gbody" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#8ecdf5"/><stop offset="100%" stop-color="#2f6da8"/></linearGradient>
        <linearGradient id="galert" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#ff9d99"/><stop offset="100%" stop-color="#c9302c"/></linearGradient>
      </defs>
      <g class="float">
        <g class="radar">
          <line x1="60" y1="14" x2="60" y2="4" stroke="#2f6da8" stroke-width="2.5" stroke-linecap="round"/>
          <path d="M60 4 L67 8 L60 12 Z" fill="#f0a04b"/>
        </g>
        <path class="body" d="M60 14 L104 30 V66 C104 94 84 112 60 122 C36 112 16 94 16 66 V30 Z"/>
        <path d="M60 24 L95 37 V65 C95 88 79 103 60 111 C41 103 25 88 25 65 V37 Z"
          fill="none" stroke="rgba(255,255,255,.45)" stroke-width="2"/>
        <rect class="scanline" x="30" y="44" width="60" height="4" rx="2" fill="rgba(255,255,255,.75)"/>
        <g class="eyes blink">
          <circle cx="46" cy="58" r="6.5"/><circle cx="74" cy="58" r="6.5"/>
          <circle cx="48" cy="56" r="2.2" fill="#fff"/><circle cx="76" cy="56" r="2.2" fill="#fff"/>
        </g>
        <g class="mood">
          <path id="mouthSmile" d="M50 76 Q60 84 70 76" stroke="#173a5e" stroke-width="2.6" fill="none" stroke-linecap="round"/>
          <path id="mouthFlat" d="M51 78 L69 78" stroke="#173a5e" stroke-width="2.6" fill="none" stroke-linecap="round" style="display:none"/>
          <path id="mouthO" d="M55 77 Q60 72 65 77 Q60 84 55 77" stroke="#173a5e" stroke-width="2.4" fill="#5b2620" style="display:none"/>
        </g>
        <circle cx="33" cy="71" r="5" fill="rgba(255,150,140,.55)"/>
        <circle cx="87" cy="71" r="5" fill="rgba(255,150,140,.55)"/>
      </g>
    </svg>
  </div>
  <div class="guard-cap">安全卫士</div>
</div>

<script>
var LEVEL_CN={high:'高危',medium:'中危',low:'低危',info:'提示'};
var LEVEL_ICON={
 high:'<svg class="ric" viewBox="0 0 24 24"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><line x1="12" y1="9" x2="12" y2="13"/><circle cx="12" cy="17" r=".7"/></svg>',
 medium:'<svg class="ric" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><circle cx="12" cy="15.5" r=".7"/></svg>',
 low:'<svg class="ric" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 15.5v.5"/></svg>',
 info:'<svg class="ric" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><circle cx="12" cy="8" r=".7"/></svg>'};
var state={status:'idle'},items=[],level='all',shown=0,timer=null,startTs=0;
var guardEl=document.getElementById('guardEl'),guardSay=document.getElementById('guardSay');

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function jsq(s){s=String(s);return "'"+s.replace(/&/g,'&amp;').replace(/\\/g,'\\\\').replace(/'/g,"\\'")+"'";}
function fmtSize(n){if(n==null)return '-';if(n>1048576)return (n/1048576).toFixed(1)+' MB';if(n>1024)return (n/1024).toFixed(0)+' KB';return n+' B';}
function fmtTime(t){if(!t)return '-';var d=new Date(t*1000);return d.getFullYear()+'-'+p2(d.getMonth()+1)+'-'+p2(d.getDate())+' '+p2(d.getHours())+':'+p2(d.getMinutes());}
function p2(n){return n<10?'0'+n:''+n;}

/* ---------- 守卫 ---------- */
function setGuard(state2,text){
  guardEl.className='guard '+(state2==='scanning'?'scanning':(state2==='alert'?'alert':''));
  document.getElementById('mouthSmile').style.display=(state2==='happy'||state2==='idle')?'':'none';
  document.getElementById('mouthFlat').style.display=(state2==='scanning')?'':'none';
  document.getElementById('mouthO').style.display=(state2==='alert')?'':'none';
  if(text)guardSay.innerHTML=text;
}
function guardTap(){
  if(state.status==='running'){setGuard('scanning','正在巡逻，别急～眼睛都不敢眨');return;}
  var c=(state.counts||{});
  if(c.high>0){setGuard('alert','有 <b>'+c.high+'</b> 个高危文件！建议尽快隔离或确认！');return;}
  if(c.medium>0){setGuard('idle','发现 '+c.medium+' 个中危项，看看是不是自己装的');return;}
  setGuard('happy','看起来挺干净的，放心吧！');
}

/* ---------- 扫描 ---------- */
function startScan(mode){
  fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:mode})}).then(function(r){return r.json()}).then(function(o){
    if(o.error){alert(o.error);return;}
    state.status='running';startTs=Date.now();
    document.getElementById('resultArea').classList.remove('show');
    document.getElementById('progressBox').classList.add('show');
    document.getElementById('pMode').textContent=(mode==='quick'?'快扫进行中':'全盘深扫进行中');
    document.getElementById('btnQuick').disabled=true;document.getElementById('btnFull').disabled=true;
    setGuard('scanning',mode==='quick'?'巡逻中…正在检查重点区域':'全盘巡逻中…文件有点多，等我一会儿');
    if(timer)clearInterval(timer);
    timer=setInterval(poll,700);
    poll();
  });
}
function poll(){
  fetch('/api/state').then(function(r){return r.json()}).then(function(s){
    state=s;
    document.getElementById('pCount').textContent=s.scanned||0;
    document.getElementById('pCur').textContent=s.current||'';
    if(startTs)document.getElementById('pElapsed').textContent=Math.round((Date.now()-startTs)/1000)+'s';
    if(s.status!=='running'){finishScan(s);}
  }).catch(function(){});
}
function finishScan(s){
  if(timer){clearInterval(timer);timer=null;}
  document.getElementById('btnQuick').disabled=false;document.getElementById('btnFull').disabled=false;
  document.getElementById('progressBox').classList.remove('show');
  if(s.status!=='done')return;
  fetch('/api/results').then(function(r){return r.json()}).then(function(o){
    items=o.items||[];level='all';shown=0;
    document.getElementById('resultArea').classList.add('show');
    document.querySelectorAll('#levelTabs span').forEach(function(t){t.className=(t.dataset.l==='all')?'on':'';});
    var c=o.counts||{},tot=items.length;
    var broken=items.filter(function(i){return i.broken;}).length;
    document.getElementById('sumTitle').textContent=
      (s.mode==='quick'?'重点快扫完成':'全盘深扫完成')+' · 检查 '+(s.done_scanned||0)+' 个文件 · 耗时 '+(s.duration||0)+'s';
    var chips='';
    chips+='<span class="chip '+(c.high?'hi':'inf')+'">高危 '+(c.high||0)+'</span>';
    chips+='<span class="chip '+(c.medium?'md':'inf')+'">中危 '+(c.medium||0)+'</span>';
    chips+='<span class="chip '+(c.low?'lo':'inf')+'">低危 '+(c.low||0)+'</span>';
    chips+='<span class="chip inf">提示 '+(c.info||0)+'</span>';
    if(broken)chips+='<span class="chip brk">破损 '+broken+'</span>';
    chips+='<span class="chip ok">已隔离 '+items.filter(function(i){return i.quarantined;}).length+'</span>';
    document.getElementById('chips').innerHTML=chips;
    var bb=document.getElementById('batchBtn');
    var pending=items.filter(function(i){return i.level==='high'&&!i.quarantined&&i.can_quarantine;}).length;
    bb.className='batch-btn'+(pending?' show':'');
    renderList();
    if(c.high>0)setGuard('alert','抓到 <b>'+(c.high||0)+'</b> 个高危目标！点卡片上的「送回收站」处理它们！');
    else if(broken>0)setGuard('idle','发现 '+broken+' 个破损文件（卡片上有「已破损」标记），可以试试「尝试修复」');
    else if(tot>0)setGuard('idle','巡逻完成，发现 '+tot+' 个待确认项，多数是自己装的软件');
    else setGuard('happy','全部检查完毕，干干净净！');
    loadHistory();
  });
}
function setLevel(l){
  level=l;shown=0;
  document.querySelectorAll('#levelTabs span').forEach(function(t){t.className=(t.dataset.l===l)?'on':'';});
  renderList();
}
function visibleItems(){return items.filter(function(i){return level==='all'||i.level===level;});}
function renderList(){
  var list=document.getElementById('list'),vis=visibleItems();
  if(!vis.length){list.innerHTML='<div class="empty">当前级别下没有项目'+(items.length?'':'<br>本次扫描未发现风险特征，棒棒哒')+'</div>';
    document.getElementById('moreBtn').style.display='none';return;}
  shown=Math.min(vis.length,shown||60);
  if(shown===0)shown=Math.min(60,vis.length);
  list.innerHTML=vis.slice(0,shown).map(cardHtml).join('');
  document.getElementById('moreBtn').style.display=(shown<vis.length)?'block':'none';
  document.getElementById('moreBtn').textContent='还有 '+(vis.length-shown)+' 条，加载更多';
}
function renderMore(){var vis=visibleItems();shown=Math.min(vis.length,shown+60);renderList();}
function cardHtml(i){
  var acts='';
  if(i.quarantined)acts='<span class="qtag">已隔离 · 可从系统回收站还原</span>';
  else if(i.broken&&i.repaired_to){
    acts='<div class="fpath">已另存：'+esc(i.repaired_to)+'</div>'+
    '<span class="rtag">已修复 · 原文件未改动</span>'+
    '<button class="act" onclick="openFolder(this,'+jsq(i.repaired_to)+')">'+
    '<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>所在文件夹</button>';
  }
  else if(i.broken&&i.can_repair){
    acts='<div class="fpath">'+esc(i.path)+'</div>'+
    '<button class="act fix" onclick="repairOne(this,'+jsq(i.path)+')">'+
    '<svg viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>尝试修复</button>'+
    '<button class="act" onclick="openFolder(this,'+jsq(i.path)+')">'+
    '<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>所在文件夹</button>'+
    '<button class="act" onclick="ignoreOne(this,'+jsq(i.path)+')">'+
    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>忽略</button>';
  }
  else if(i.broken){
    acts='<div class="regnote">该格式（图片类）仅支持破损检测，暂不支持自动修复；若图片重要，建议从备份或原下载源重新获取。</div>';
  }
  else if(i.can_quarantine){
    acts='<div class="fpath">'+esc(i.path)+'</div>'+
    '<button class="act danger" onclick="quarantine(this,'+jsq(i.path)+')">'+
    '<svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/></svg>送回收站</button>'+
    '<button class="act" onclick="openFolder(this,'+jsq(i.path)+')">'+
    '<svg viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>所在文件夹</button>'+
    '<button class="act" onclick="ignoreOne(this,'+jsq(i.path)+')">'+
    '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12h8"/></svg>忽略</button>';
  }else{
    var note=i.reg?'该风险来自注册表启动项（'+i.reg+'）：请在「任务管理器 → 启动应用」里禁用对应项目，或用 autoruns 工具处理。':
      (i.is_hosts?'这是系统 hosts 文件，请人工核对内容后再决定是否清理；正常情况只应包含 localhost 映射。':
       '目标文件已不存在（可能已被安全软件清除或手动删除）。');
    acts='<div class="regnote">'+note+'</div>';
  }
  return '<div class="finding'+(i.quarantined?' quarantined':'')+'" data-l="'+i.level+'">'+
  '<div class="ftop"><span class="fname">'+esc(i.name||i.path)+'</span>'+
  '<span class="lbadge '+i.level+'">'+LEVEL_CN[i.level]+'</span>'+
  (i.broken?'<span class="lbadge broken">已破损</span>':'')+
  (i.quarantined?'<span class="qtag">已隔离</span>':'')+'</div>'+
  '<div class="reasons">'+i.reasons.map(function(r){return '<div class="reason">'+LEVEL_ICON[i.level]+'<span>'+esc(r)+'</span></div>';}).join('')+'</div>'+
  '<div class="fmeta"><span>大小 '+fmtSize(i.size)+'</span><span>修改于 '+fmtTime(i.mtime)+'</span></div>'+
  '<div class="oprow">'+acts+'</div></div>';
}

/* ---------- 处置 ---------- */
function repairOne(btn,p){
  if(!confirm('该文件结构已破损，授权尝试修复吗？\n\n修复说明：\n· 重建文件结构，另存为新文件（如「原名(修复).pdf」）\n· 原文件保持不动，可随时对比\n· 不保证内容 100% 完整：损坏点的数据可能已丢失\n· 修复后请打开新文件确认内容'))return;
  btn.disabled=true;btn.textContent='修复中…';
  fetch('/api/repair',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:p})}).then(function(r){return r.json()}).then(function(o){
    if(o.ok){
      items.forEach(function(it){if(it.path===p)it.repaired_to=o.new_path;});
      renderList();
      setGuard('happy','修复完成！新文件已另存，记得打开确认内容是否完整～');
    }else{
      alert(o.error||'修复失败');
      btn.disabled=false;
      btn.innerHTML='<svg viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>尝试修复';
    }
  });
}
function quarantine(btn,p){
  if(!confirm('确定把该文件送入回收站吗？（可随时从系统回收站还原）'))return;
  btn.disabled=true;btn.textContent='隔离中…';
  fetch('/api/quarantine',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:[p]})}).then(function(r){return r.json()}).then(function(o){
    if(o.ok){markQuarantined(p);setGuard('happy','搞定！文件已进回收站，安心了～');}
    else{alert((o.results&&o.results[0]&&o.results[0].error)||'隔离失败');btn.disabled=false;btn.innerHTML='送回收站';}
    refreshCounts();
  });
}
function batchHigh(){
  var ps=items.filter(function(i){return i.level==='high'&&!i.quarantined&&i.can_quarantine;}).map(function(i){return i.path;});
  if(!ps.length){alert('没有可隔离的高危文件');return;}
  if(!confirm('将把 '+ps.length+' 个高危文件送入系统回收站（可还原），确定执行？'))return;
  setGuard('scanning','正在批量隔离 '+ps.length+' 个目标…');
  fetch('/api/quarantine',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({paths:ps})}).then(function(r){return r.json()}).then(function(o){
    markQuarantined(ps);refreshCounts();
    setGuard('happy','批量隔离完成：'+o.count+'/'+o.total+' 个成功！');
  });
}
function markQuarantined(ps){
  if(!Array.isArray(ps))ps=[ps];
  items.forEach(function(i){if(ps.indexOf(i.path)>-1)i.quarantined=true;});
  renderList();
}
function ignoreOne(btn,p){
  fetch('/api/ignore',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:p})}).then(function(r){return r.json()}).then(function(){
    items=items.filter(function(i){return i.path!==p;});renderList();refreshCounts();
    setGuard('idle','已忽略，之后的扫描也不再提示它');
  });
}
function openFolder(btn,p){
  fetch('/api/open_folder',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:p})});
  btn.classList.add('done');setTimeout(function(){btn.classList.remove('done');},1600);
}
function refreshCounts(){
  var c={high:0,medium:0,low:0,info:0,q:0,brk:0};
  items.forEach(function(i){if(i.quarantined)c.q++;else{c[i.level]++;if(i.broken)c.brk++;}});
  state.counts=c;
  var chips='';
  chips+='<span class="chip '+(c.high?'hi':'inf')+'">高危 '+c.high+'</span>';
  chips+='<span class="chip '+(c.medium?'md':'inf')+'">中危 '+c.medium+'</span>';
  chips+='<span class="chip '+(c.low?'lo':'inf')+'">低危 '+c.low+'</span>';
  chips+='<span class="chip inf">提示 '+c.info+'</span>';
  if(c.brk)chips+='<span class="chip brk">破损 '+c.brk+'</span>';
  chips+='<span class="chip ok">已隔离 '+c.q+'</span>';
  document.getElementById('chips').innerHTML=chips;
  var pending=items.filter(function(i){return i.level==='high'&&!i.quarantined&&i.can_quarantine;}).length;
  document.getElementById('batchBtn').className='batch-btn'+(pending?' show':'');
}
function loadHistory(){
  fetch('/api/history').then(function(r){return r.json()}).then(function(s){
    var it=s.items||[];
    document.getElementById('historyBox').className='history'+(it.length?' show':'');
    document.getElementById('histBody').innerHTML=it.map(function(h){
      var c=h.counts||{};
      return '<tr><td>'+esc(h.start)+'</td><td>'+(h.mode==='quick'?'快扫':'全盘')+'</td>'+
      '<td>'+(h.scanned||0)+'</td><td>'+(h.duration||0)+'s</td>'+
      '<td class="'+(c.high?'c-red':'c-gry')+'">'+(c.high||0)+'</td>'+
      '<td class="'+(c.medium?'c-org':'c-gry')+'">'+(c.medium||0)+'</td>'+
      '<td class="'+(c.low?'c-blu':'c-gry')+'">'+(c.low||0)+'</td>'+
      '<td class="c-gry">'+(c.info||0)+'</td></tr>';
    }).join('');
  });
}

/* ---------- 初始化 ---------- */
fetch('/api/state').then(function(r){return r.json()}).then(function(s){
  state=s;
  if(s.status==='running'){startTs=(s.start_ts||0)*1000;
    document.getElementById('progressBox').classList.add('show');
    document.getElementById('pMode').textContent=(s.mode==='quick'?'快扫进行中':'全盘深扫进行中');
    document.getElementById('btnQuick').disabled=true;document.getElementById('btnFull').disabled=true;
    setGuard('scanning','接着上次的巡逻继续…');timer=setInterval(poll,700);return;}
  if(s.status==='done'&&s.counts){finishScan(s);return;}
}).catch(function(){});
loadHistory();
setTimeout(function(){if(state.status==='idle')setGuard('idle','今天还没有巡逻过，点上面「重点快扫」试试？');},900);
</script>
</body>
</html>
"""


# ---------- 入口 ----------

def _port_busy(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="文件卫士 · Web 界面")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if _port_busy(args.port):
        # 已有实例：直接打开浏览器复用
        url = f"http://127.0.0.1:{args.port}/"
        print(f"端口 {args.port} 已有实例运行，直接打开浏览器：{url}")
        webbrowser.open(url)
        return

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"文件卫士已启动：{url}（Ctrl+C 退出）")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
