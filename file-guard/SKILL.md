---
name: file-guard
name_cn: 文件卫士
description: 本地启发式安全扫描工具，检测伪装成文档/图片的可执行病毒文件（双扩展名）、诱导性命名（破解/激活类）、可疑快捷方式、带宏文档、未签名程序、加壳程序、启动项指向临时目录、hosts 篡改、autorun.inf 等"长得像病毒"的可疑特征，以及广告/流氓软件捆绑痕迹；同时检测 PDF/ZIP/docx/xlsx/pptx 及图片的文件破损（下载中断/存储损坏），支持授权修复：重建结构另存新文件（原文件不动，不保证 100% 完整）。提供重点目录快扫（秒级）与全盘深扫两种模式，Web 界面展示风险清单并支持一键送入系统回收站（可还原）与破损文件修复。触发场景：用户想扫描电脑有没有病毒/可疑文件、清理广告捆绑软件、检查下载的文件是否安全、电脑体检、排查流氓软件启动项、修复破损打不开的文件。
description_cn: 本地启发式安全扫描工具：检测双扩展名伪装病毒、诱导命名、可疑快捷方式、宏文档、未签名/加壳程序、启动项异常、hosts 篡改等可疑特征与广告捆绑痕迹；快扫秒级出结果，Web 界面一键隔离（送回收站可还原）。当用户说"帮我扫扫电脑有没有病毒""检查这个文件可不可疑""清理捆绑软件"时使用。
create_source: super-agent-skill-creator
---

# 文件卫士 · 本地风险扫描

定位是 **Windows Defender 之外的第二道防线**：用启发式规则抓"长得像病毒/广告捆绑"的可疑文件，纯本地运行、不联网、数据不出本机。不能替代专业杀软（无病毒特征库，加壳变种可能漏报），建议两者并用。

## 何时使用

用户说这类话时触发本技能：
- "帮我扫一下电脑有没有病毒 / 可疑文件"
- "检查下载目录里的东西安全吗"
- "电脑好像中了捆绑广告，帮我查查"
- "这个 .exe 会不会是病毒"
- "电脑体检一下"

## 两种用法

**Web 界面**（推荐给用户日常使用）：
```
python scripts/web_ui.py            # http://127.0.0.1:8766，自动打开浏览器
```
界面含：快扫/全盘按钮、进度轮询、风险卡片（级别徽章+人话原因）、单个/批量一键送回收站、忽略名单、扫描历史、盾牌守卫吉祥物。

**桌面快捷方式**（v1.4.0）：界面「桌面快捷方式」区块或 `POST /api/shortcut` 可在桌面创建「文件卫士.lnk」（pythonw 零窗口直启 + 透明底盾牌图标 guardlogo.ico）。与文档拾忆同套三重防护：①目标级去重——桌面任意名字的快捷方式只要已指向本程序就不再重复创建；②失效自动重建——同名快捷方式指向已不存在的旧目录（换目录解压后）自动删除重建；③指向另一份活副本时不劫持。

**命令行 / 模块**（供智能体直接调用）：
```
python scripts/scanner.py                    # 重点目录快扫
python scripts/scanner.py --full             # 全盘深扫
python scripts/scanner.py --json             # JSON 输出（供程序解析）
python scripts/scanner.py --level high       # 只显示高危及以上
```
```
from scanner import run_scan
out = run_scan("quick")          # 或 "full"
out["findings"]                  # [{path,name,level,reasons,rules,size,mtime,...}]
out["counts"]                    # {"high":n,"medium":n,"low":n,"info":n}
```

智能体典型工作流：`run_scan("quick")` → 汇总 counts 与高危项向用户报告 → 用户确认后再调 Web 界面或 `api_quarantine` 逻辑隔离。

## 检测规则（启发式）

| 规则 | 说明 | 级别 |
|---|---|---|
| double_ext | 双扩展名伪装：`发票.pdf.exe` 看似文档实为可执行 | 高危 |
| autorun | 各盘根目录 autorun.inf（U 盘蠕虫残留） | 高危 |
| run_key | 注册表启动项指向 temp/公共可写目录 | 高危 |
| danger_ext | 高危扩展名（scr/hta/vbs 等）出现在下载/桌面投递位置 | 中危 |
| luring_name | 诱导性文件名（破解/激活/注册机/外挂等） | 中危 |
| lnk_suspicious | 快捷方式调用 powershell -enc / mshta 或指向临时目录 | 中危 |
| unsigned_pe | 下载/桌面里的无数字签名可执行（结合诱导名升中危） | 低-中危 |
| macro_doc | 带宏文档（docm/xlsm 等） | 提示 |
| packed_pe | PE 区段带商业壳特征（VMP/Themida） | 低危 |
| startup_dir | 启动目录里的可执行 | 低危 |
| hosts | hosts 文件含非本地映射条目 | 低危 |
| broken_file | 文件结构破损：PDF 头尾/xref/对象平衡、ZIP 中央目录、图片头尾校验；可修复格式提供授权修复 | 中危 |

误报控制（实测调优）：`.js`/`.vbs` 在 Documents/TEMP 开发环境过于常见，仅下载/桌面投递位置报告；未签名检查仅对投递位置小型 PE 做，快扫保持秒级；`~$` Office 锁文件与扩展名标错的图片（如实为 WebP 的 .jpg）不算破损。

## 破损检测与修复

检测：头尾结构校验（头 1KB + 尾 64KB）+ PDF 对象平衡深度校验（≤50MB 全读）。
修复（repair.py，尽力而为）：
- **PDF**：扫描全部对象 → 重建交叉引用表与 trailer → 追加到原字节后
- **ZIP/docx/xlsx/pptx**：扫描全部本地文件头 → 重建中央目录 + EOCD
- 原文件**永远不动**，修复结果另存「原名(修复).ext」；损坏点数据可能丢失（无法凭空恢复）；修复记录存 `data/repaired.json`
- 图片类（PNG/JPEG 等）仅检测提示，不支持自动修复

智能体修复入口：
```
from repair import repair_file
repair_file(r"C:\path\file.pdf")   # → {ok,new_path,objects,notes} 或 {error}
```

## 隔离机制

送入 **Windows 系统回收站**（SHFileOperationW + ALLOWUNDO），非直接删除，误报可随时从桌面回收站还原。隔离记录存 `data/quarantined.json`，忽略名单存 `data/ignored.json`，扫描历史存 `data/scan_history.json`（最近 20 次）。

## 数据与部署

- 纯 Python 标准库实现（wintrust 签名验证走 ctypes），零第三方依赖，不联网。
- 数据目录：`scripts/data/`；D 盘副本通过 `scripts/data_dir.txt` 共享 C 盘技能的 data 目录（与「文档拾忆」同机制）。
- 桌面快捷方式「文件卫士」：pythonw 零窗口直启 + 盾牌图标；重复启动自动复用已有实例（直接开浏览器）。
- Web 界面仅监听 127.0.0.1:8766。
