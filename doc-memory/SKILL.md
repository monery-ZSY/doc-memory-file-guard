---
name: doc-memory
description: "Local file content search skill (Document Memory): find files by CONTENT keywords when name/location is forgotten. Local SQLite index over Documents/Desktop/Downloads/WeChat folders and fixed drives, incremental updates, relevance/time sort, type/time/dir filters, one-click open/locate from web UI, search history, first-use authorized scan, pet assistant (belly safe-delete: store up to 99 items 24h, expired items go to Recycle Bin, double-click belly to restore), daily snapshots, PDF extraction (bundled pypdf), standalone local web UI with 4-tab layout (search/library/discover/settings), checkbox+summary+recycle-bin delete flow, no-result diagnostics. Trigger: 找不到文件/忘了存哪/记得内容忘了文件名/帮我找文件/之前写过的文档/前一天保存的文件/每日快照/昨天存的文件/打开文档拾忆界面/检索历史, or 建立文件索引/更新索引/扫描文件/删除文件/恢复删除, or vague content descriptions expecting a local file."
name_cn: 文档拾忆
description_cn: "按内容找回文件：忘了文件名和位置时，用大概内容关键词搜出本机文件。网页界面检索优先（四分区：检索/资料库/发现/设置），支持勾选批量移入回收站（影响摘要+可还原）、无结果诊断提示、宠物肚子安全删除（到期转回收站不直接删），数据全程留在本机。"
create_source: super-agent-skill-creator
---

# 文档拾忆（Document Memory）

按文件**内容**搜索本机文件，解决"昨天存的文件今天忘了在哪、也忘了文件名"的问题。两种使用形态：

1. **TeleAgent 对话**（主入口）：用户模糊描述内容，智能体拆词多轮检索
2. **独立网页界面**：`python scripts/web_ui.py`（或双击 `run_web_ui.bat`）本地启动检索界面，浏览器直接搜

网页界面亮点（v1.2.0 起四分区信息架构，检索为第一优先级）：**检索**（默认首页）——搜索框+筛选+结果+顶部最近搜索胶囊（点击一键重搜，全部历史弹窗可查，防意外关闭丢失）；**资料库**——统计卡/类型分布/录入趋势/**检索能力边界说明**（PDF前40页/截断长度/20MB/忽略规则，解释为什么有的东西搜不到）；**发现**——每日快照/疑似重复榜（同名同大小判定，明确标注未经哈希验证）/找相似；**设置**——隐私与数据边界/个性化（签到、商店、天气重授权）/意见反馈/文件卫士外部入口。结果**默认按匹配度排序**（可切时间）、**中英文文件名筛选**、**时间分布轴**、**找相似**、**AI 总结桥接**、一键打开文件/所在文件夹、**首次使用引导**（未建索引时授权确认）。**无结果诊断**：搜不到时给出四类原因（未扫描/图片视频只能按文件名/二进制不收录/关键词太窄）+一键增量扫描入口。**安全删除主流程**：勾选结果卡片 → 批量操作条显示已选数量与体积 → 确认弹窗列出影响摘要（文件清单+总大小）→ 移入 **Windows 系统回收站**（随时可还原）并自动移出索引。内置**界面宠物小助手**（进页面打招呼、搜索时思考、找到开心、没找到安慰、点击互动），以及**宠物式暂存**：把结果卡片拖到宠物身上即被吃掉（移入肚子暂存目录，上限 99 个，24 小时后自动**转入系统回收站**而非直接删除，后台每 10 分钟自动清扫），**双击宠物肚子**可把文件吐回原位（原位被占则存为「 (恢复-xxxx)」副本，吐出后自动回写索引）。桌面快捷方式可在首次授权时勾选创建（**默认不勾选**，不动用户桌面；v1.2.1 起目标级去重：桌面任意名字的快捷方式只要已指向本程序就不再重复创建，换目录解压后的失效快捷方式会自动重建，设置页也有手动创建入口）。

**宠物养成**（2026-10 新增，v1.2.0 起收进「设置」分区，不再在首屏角落实出）：**每日签到**（积分 10+连签加成，面板含本周日历/积分卡）与**宠物商店**（5 款粮食不同价位：小鱼干 10/营养猫粮 20/鸡肉冻干 30/丰盛罐头 50/豪华妙鲜包 100，买后可喂食，每款粮食吃完宠物有不同表情/动作/话语与粒子特效）；**睡觉机制**（长时间不操作先犯困打哈欠、再睡着冒 Zzz，点击界面唤醒继续工作）；**天气装扮**（**每次进入界面弹一次联网授权**，同意才联网：授权弹窗可**填写所在城市**（支持中英文，如“郑州”，Open-Meteo 地理编码+按坐标精确查询；城市记在浏览器 localStorage 不上传），留空则按 IP 自动定位——开 VPN/代理时 IP 会漂移导致城市温度不准，已明确提示用户填城市；晴天宠物左上角旋转小太阳、雨天头顶乌云+实时雨滴+撑伞+雨靴、大风天枫叶绕飞+同色系围脖、下雪天雪花飘落+羽绒服+耳包，顶部显示“城市 · 天气 温度”标签，拒绝则完全不发请求不装扮；查询按城市独立缓存 30 分钟、失败静默降级；设置页可重新授权）；**文件卫士外部入口**（设置页点击新标签页打开独立界面，服务未运行时自动后台拉起，不再 iframe 内嵌——两个产品风险模型与任务节奏不同）。

纯 Python 标准库实现；`scripts/vendor/` 内置 pypdf（pip --target 安装，不污染全局环境），PDF 正文开箱支持。索引与快照全部保存在本机 `scripts/data/` 目录下，不联网不上传。

## 脚本

- `scripts/indexer.py` — 建立与增量更新索引，生成每日快照报告；`rescan-ext` 补扫指定类型
- `scripts/searcher.py` — 关键词检索（CLI 与模块 `run_search()` 双入口，含重复副本识别）
- `scripts/web_ui.py` — 独立检索界面（127.0.0.1 服务 + 四分区浏览器 UI + 宠物 + 检索历史 + 首次引导 + 勾选回收站删除 + 意见反馈 + 签到/商店/喂食/天气装扮）
- `scripts/pet_belly.py` — 宠物肚子：吃掉的文件暂存（上限 99）/吐出恢复/24 小时到期转系统回收站（可还原）
- `scripts/pet_shop.py` — 签到积分/宠物商店/喂食/天气查询（数据存 data/pet_points.json；天气仅获取当地气象，失败静默降级）
- `scripts/selfcheck.py` — 每日自检：索引完整性/孤儿记录/日志错误/过期暂存自动修复 + 待处理反馈统计
- `scripts/run_web_ui.bat` — 双击启动网页界面（pythonw 零窗口；桌面快捷方式可选创建，见上文）

## 工作流

### 1. 用户想找文件（对话式，标准流程）

1. 确认索引存在：运行 `python scripts/searcher.py "关键词"`。若提示"索引尚未建立"，先执行 `python scripts/indexer.py scan`（首次全量扫描可能需要数分钟到半小时，告知用户耐心等待）。
2. **把用户的模糊描述拆成多组关键词，分多轮搜索**（核心增值步骤，见下节）。
3. 合并去重结果（按 path 去重，保留最高分），按分数排序后向用户呈现：文件名、路径、修改时间、命中片段。
4. 结果为空时降级：换关键词 → 放宽过滤 → 提示扫描更新索引。
5. 用户想要图形界面时：启动 `python scripts/web_ui.py`（自动打开浏览器），告知访问地址与退出方式（Ctrl+C）。
6. 用户问"怎么删文件/恢复删除的文件"：指引网页界面的两种方式——**主流程**：勾选结果卡片 →「移入回收站」→ 确认影响摘要 → 送 Windows 回收站（随时可从桌面回收站还原）；**宠物暂存**：拖拽结果卡片到宠物身上即吃掉（暂存 24 小时，到期自动转回收站而非直接删除），双击宠物肚子吐出恢复原位。

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
- 补扫某类型正文：`python scripts/indexer.py rescan-ext pdf`（只重新提取指定扩展名，不重走全盘；更换/新增提取器后用）。**存量图片补 OCR**：`python scripts/indexer.py rescan-ext jpg` 或 `jpg,png`（约 0.5 秒/张，2 万张存量图约 3~4 小时，可按扩展名分批；只补 8MB 以内图片）。
- **忽略规则（v1.1.0）**：自动跳过对"找回文档"无价值的依赖与二进制——依赖目录（Anaconda/pkgs/node_modules/.git/venv/site-packages 等 38 类）与二进制扩展名（dll/exe/enc/zip/字体/pyc 等 52 类）不收录，索引体积可缩小一半以上且扫描更快。用户可在 `scripts/data/config.json` 追加 `"ignore_dirs": ["MySDK"]`、`"ignore_exts": ["psd"]`（大小写不敏感，扩展名带不带点均可），下次 scan 生效；`indexer.py status` 会显示当前生效的忽略规则统计。重扫时被忽略文件的旧记录会自动清出索引。

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
- **图片 OCR（v1.3.0）**：增量扫描遇到的新图片（jpg/png/bmp/tiff/webp/gif，8MB 内）自动调 Windows 自带引擎本地识别图中文字入索引，可直接按图片内容搜；纯图形无文字的图与首次全量的存量图仍按文件名索引（存量图可用 `rescan-ext jpg,png` 补扫，见上文）。视频/压缩包只索引文件名。OCR 语言跟随系统（中文系统识别中英文，英文系统仅识别英文，需系统已装对应 OCR 语言包）。
- 搜索无结果时界面会自动触发一次后台增量扫描（刚保存的文件最常见），完成后提示再搜一次；天气城市同意授权后记住在本机 `data/weather_city.txt`，不再受代理 IP 漂移影响。
- dll/exe/enc/字体等纯二进制与依赖目录（Anaconda 等）默认直接不收录（v1.1.0 起忽略，可在 config.json 调整）；视频/压缩包只索引文件名、路径与时间。
- 正文截断：文档类前 1.2 万字符，代码/数据类前 4 千字符（控制索引体积；代码类对"找回文档"场景价值低）；超 20MB 的文件只记文件名（`--max-size-mb` 可调）。
- 全库数据（索引、快照、配置）都在技能目录 `scripts/data/` 下；vendor 依赖在 `scripts/vendor/`，不联网、不上传、不改全局 Python 环境。
- 索引库为 SQLite **WAL 模式**（v1.3.1 起默认）：扫描写入期间搜索/状态查询并发不阻塞（实测首扫期间查询 20~80ms）；库旁会出现 `index.db-wal`/`index.db-shm` 辅助文件属正常现象，勿删。
