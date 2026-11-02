---
name: doc-memory
description: "Local file content search skill (Document Memory): helps users find files whose name or location they forgot, by searching file CONTENT keywords instead of file names. Builds a local SQLite index of Documents/Desktop/Downloads/WeChat-QQ folders and fixed drives, supports incremental updates, relevance/time sorting, time/type/dir filters, one-click jump to file or its folder from the web UI, search history (auto-recorded, restorable with one click), first-use authorized full-scan onboarding, a built-in pet assistant (emotional feedback + pet-belly safe delete: drag files to pet to store up to 99 items for 24h, double-click belly to restore), daily file snapshot reports, PDF content extraction (bundled pypdf in scripts/vendor), and a standalone local web UI (web_ui.py). Trigger when: user says 找不到文件/忘了存哪/记得内容忘了文件名/帮我找文件/之前写过的文档/前一天保存的文件/每日快照/昨天存的文件/打开文档拾忆界面/搜索界面/检索历史, or asks to 建立文件索引/更新索引/扫描文件/删除文件/恢复删除; also when user describes file content vaguely and expects the file to be located on this computer."
name_cn: 文档拾忆
description_cn: "按内容找回文件：忘了文件名和位置时，用大概内容关键词搜出本机文件。网页界面支持一键跳转、检索历史防丢失、首次授权扫描引导、宠物肚子安全删除（99个上限24小时吐出恢复），数据全程留在本机。"
create_source: super-agent-skill-creator
---

# 文档拾忆（Document Memory）

按文件**内容**搜索本机文件，解决"昨天存的文件今天忘了在哪、也忘了文件名"的问题。两种使用形态：

1. **TeleAgent 对话**（主入口）：用户模糊描述内容，智能体拆词多轮检索
2. **独立网页界面**：`python scripts/web_ui.py`（或双击 `run_web_ui.bat`）本地启动检索界面，浏览器直接搜

网页界面亮点：结果**默认按匹配度排序**（可切时间）、**中英文文件名筛选**（下拉一键只看中文名/英文名，按文件名是否含中文判定）、**时间分布轴**（结果按年份分布柱状图，点击年份即过滤、再点取消，分布始终展示全量）、**找相似**（每条结果一键推荐同目录邻居+全库文件名相似文件）、**AI 总结桥接**（一键复制路径到 TeleAgent 对话总结文档）、每个结果可**一键打开文件 / 打开所在文件夹**（资源管理器定位）、**检索历史**（每次搜索自动记录去重，最近 50 条，「检索历史」标签页一键重搜，防止意外关闭丢失）、**首次使用引导**（未建索引时弹出授权确认，用户同意才开始全盘扫描识别）、内置**界面宠物小助手**（进页面打招呼、搜索时思考、找到开心、没找到安慰、点击互动），以及**宠物式安全删除**：把结果卡片拖到宠物身上即被吃掉（移入肚子暂存目录，上限 99 个，24 小时后彻底删除，后台每 10 分钟自动清扫），**双击宠物肚子**可把文件吐回原位（原位被占则存为「 (恢复-xxxx)」副本，吐出后自动回写索引）。桌面已创建「文档拾忆」快捷方式（指向 run_web_ui.bat，双击即启动界面并自动打开浏览器）。

**宠物养成与拓展模块**（2026-10 新增）：左下角新增**每日签到**（积分 10+连签加成，面板含本周日历/积分卡/入口徽标）与**拓展模块**入口（集中聚合宠物商店/意见反馈/文件卫士等扩展功能，后续新功能也放这里）；**宠物商店**（5 款粮食不同价位：小鱼干 10/营养猫粮 20/鸡肉冻干 30/丰盛罐头 50/豪华妙鲜包 100，买后可喂食，每款粮食吃完宠物有不同表情/动作/话语与粒子特效）；**睡觉机制**（长时间不操作先犯困打哈欠、再睡着冒 Zzz，点击界面唤醒继续工作）；**天气装扮**（**每次进入界面弹一次联网授权**，同意才联网：授权弹窗可**填写所在城市**（支持中英文，如“郑州”，Open-Meteo 地理编码+按坐标精确查询；城市记在浏览器 localStorage 不上传），留空则按 IP 自动定位——开 VPN/代理时 IP 会漂移导致城市温度不准，已明确提示用户填城市；晴天宠物左上角旋转小太阳、雨天头顶乌云+实时雨滴+撑伞+雨靴、大风天枫叶绕飞+同色系围脖、下雪天雪花飘落+羽绒服+耳包，顶部显示“城市 · 天气 温度”标签，拒绝则完全不发请求不装扮；查询按城市独立缓存 30 分钟、失败静默降级）；**文件卫士界面内嵌**（拓展模块点击即在弹窗内直接使用，服务未运行时自动后台拉起）。

纯 Python 标准库实现；`scripts/vendor/` 内置 pypdf（pip --target 安装，不污染全局环境），PDF 正文开箱支持。索引与快照全部保存在本机 `scripts/data/` 目录下，不联网不上传。

## 脚本

- `scripts/indexer.py` — 建立与增量更新索引，生成每日快照报告；`rescan-ext` 补扫指定类型
- `scripts/searcher.py` — 关键词检索（CLI 与模块 `run_search()` 双入口，含重复副本识别）
- `scripts/web_ui.py` — 独立检索界面（127.0.0.1 服务 + 浏览器 UI + 宠物 + 检索历史 + 首次引导 + 重复文件清理 + 回收站 + 意见反馈 + 签到/商店/喂食/天气装扮）
- `scripts/pet_belly.py` — 宠物肚子：吃掉的文件暂存（上限 99）/吐出恢复/24 小时过期清扫
- `scripts/pet_shop.py` — 签到积分/宠物商店/喂食/天气查询（数据存 data/pet_points.json；天气仅获取当地气象，失败静默降级）
- `scripts/selfcheck.py` — 每日自检：索引完整性/孤儿记录/日志错误/过期暂存自动修复 + 待处理反馈统计
- `scripts/run_web_ui.bat` — 双击启动网页界面（桌面有同名快捷方式，pythonw 零窗口）

## 工作流

### 1. 用户想找文件（对话式，标准流程）

1. 确认索引存在：运行 `python scripts/searcher.py "关键词"`。若提示"索引尚未建立"，先执行 `python scripts/indexer.py scan`（首次全量扫描可能需要数分钟到半小时，告知用户耐心等待）。
2. **把用户的模糊描述拆成多组关键词，分多轮搜索**（核心增值步骤，见下节）。
3. 合并去重结果（按 path 去重，保留最高分），按分数排序后向用户呈现：文件名、路径、修改时间、命中片段。
4. 结果为空时降级：换关键词 → 放宽过滤 → 提示扫描更新索引。
5. 用户想要图形界面时：启动 `python scripts/web_ui.py`（自动打开浏览器），告知访问地址与退出方式（Ctrl+C）。
6. 用户问"怎么删文件/恢复删除的文件"：指引网页界面的宠物删除——拖拽结果卡片到宠物身上即吃掉（暂存 24 小时后彻底删除），双击宠物肚子吐出恢复；也可用肚子徽标查看暂存清单与倒计时。

### 2. 关键词拆解策略（LLM 增值，必须执行）

用户往往说不出文件名，只记得内容。将描述转成 2~5 组检索词，逐组搜索：

| 用户说 | 拆解出的关键词组 |
|---|---|
| "写了液冷板流道优化的文档" | 液冷板；流道；拓扑优化 |
| "那篇讲热失控预警的综述" | 热失控 预警（--all）；热失控；气体传感 |
| "之前算循环寿命的脚本" | 循环寿命 --type py；容量保持率；retention |
| "上周发我的修改意见" | 修改意见 --within 7d |

规则：
- 优先核心名词/术语，去掉虚词；每轮 1~2 个词即可，命中过多时再加词收窄。
- 中文描述可补英文术语、缩写、拼音（如 EIS、SEM、PCM）；代码类文件补函数名或库名。
- 用户提到时间线索（"上周""前几天"）时加 `--within`；提到文件类型时加 `--type`；知道大致位置时加 `--dir`。
- 多个词都用上仍无结果时，退回单关键词扩大召回。

### 3. 索引维护

- 首次使用：`python scripts/indexer.py scan`（自动探测扫描范围并写入配置，含用户文档/桌面/下载/微信QQ文件夹及非C盘本地磁盘）。
- 找不到刚保存的文件：先 `scan` 增量更新再搜索（增量扫描只处理新增/修改文件，很快）。
- 自定义范围：`scan --roots "D:\论文;C:\Users\me\Documents"`（会持久化到 config.json，之后的扫描沿用）。
- 补扫某类型正文：`python scripts/indexer.py rescan-ext pdf`（只重新提取指定扩展名，不重走全盘；更换/新增提取器后用）。

### 4. 每日凌晨维护与自检（配合定时任务）

向用户建议用定时任务技能创建每日任务（例如每天 00:00）执行三步：

1. **增量扫描**：`python "<技能目录>/scripts/indexer.py" scan --report --quiet` → 更新索引并生成当日快照 `scripts/data/snapshots/YYYY-MM-DD.md`
2. **运行自检**：`python "<技能目录>/scripts/selfcheck.py"` → 检查索引完整性/孤儿记录/日志错误/过期暂存（能修的已自动修复），报告 warn/fail 项
3. **处理用户反馈**：若自检显示有待处理反馈，读取 `scripts/data/feedback.json`，逐条识别意图（功能建议/界面问题/体验/Bug），能改的直接修改 `web_ui.py` 等界面代码并重启本地服务，改完后把条目 status 改为 done 并附 handled_note；拿不准的留 pending 待用户确认。

用户白天在网页界面「意见反馈」入口提交的反馈都存在 feedback.json，形成「白天提反馈 → 凌晨自动优化」的闭环。用户也可以随时说「看看界面反馈并优化」立即触发处理。

## 命令参考

indexer.py：
```
scan  [--roots "路径;路径"] [--report] [--max-size-mb 20] [--quiet]
status
rescan-ext pdf[,docx] [--max-size-mb 20]
```

searcher.py：
```
"关键词" [更多关键词] [--all] [--type docx,pdf] [--within 7d|2026-10-01..2026-10-28]
         [--new today|yesterday|7d|日期] [--dir 子串] [--limit 20] [--year 2024]
         [--sort score|time] [--json]
```
web_ui.py 界面过滤参数同上，另有 `lang=zh|en`（中/英文文件名，仅界面与 API）。

web_ui.py：
```
python web_ui.py [--port 8765] [--no-browser]   # 本地检索界面，自动打开浏览器
```

`--json` 与模块函数 `run_search()` 返回同构结果（含 score/pct/snippets/sort/year/name_lang/by_year；pct 为 0-100 匹配度百分比，本组结果最高分为 100%）。打分为**纯匹配质量分**（文件名命中 +10/词、目录 +5/词、正文每次命中 +2 封顶 10 次、全关键词命中额外 +5×(词数-1)；不含时间加成，时间仅作同分排序次级依据），供程序化处理与去重合并。界面与 API 默认 sort=score（匹配度优先）。过滤参数：year（自然年）、name_lang（zh 中文名/en 纯英文名）。

## 能力边界与注意

- 正文可提取：txt/md/代码/csv/json/html/tex 等；docx/pptx/xlsx（零依赖解析）；pdf（内置 pypdf，仅提取前 40 页以保证速度）。
- 图片、视频、压缩包等只索引文件名、路径与时间，内容搜不到属正常。
- 正文截断：文档类前 1.2 万字符，代码/数据类前 4 千字符（控制索引体积；代码类对"找回文档"场景价值低）；超 20MB 的文件只记文件名（`--max-size-mb` 可调）。
- 全库数据（索引、快照、配置）都在技能目录 `scripts/data/` 下；vendor 依赖在 `scripts/vendor/`，不联网、不上传、不改全局 Python 环境。
