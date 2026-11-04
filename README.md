# 文档拾忆 & 文件卫士

两个跑在本机的 TeleAgent 技能：一个用**内容关键词**找回忘了位置的文件，一个识别电脑里**长得像病毒的文件**并修复破损文档。数据全程留在本机，不上传、不联网（唯一的联网请求是天气查询，且需要你手动授权）。

TeleAgent（星辰超级智能体）是桌面端 AI 智能体，技能（Skill）是它能加载的能力包。这两个技能都做了双入口设计：装进 TeleAgent 可以对话使用，不装 TeleAgent 也能双击独立运行。

## 它们解决什么问题

**文档拾忆（doc-memory）**——"昨天明明存了，今天死活找不到"：论文、方案、截图存了一堆，过两天只记得内容大概讲什么。按内容搜，直接出结果：文件在哪、什么时候改的、命中了哪句话。忘了文件名也没关系。

**文件卫士（file-guard）**——"这个文件能不能点"：`发票.pdf.exe` 这种伪装成文档的可执行文件、"破解版"诱导命名、指向临时目录的开机启动项、被篡改的 hosts、下载中断打不开的 PDF/ZIP。快扫秒级出结果，风险文件一键送回收站（可还原），破损文件可尝试修复。

## 环境要求

- Windows 10 / 11
- Python 3.8 或更高（[python.org](https://www.python.org/downloads/) 下载，安装时勾选 **Add Python to PATH**）
- 零第三方依赖：文档拾忆所需的 pypdf 已内置在 `doc-memory/scripts/vendor/`，其余全部用 Python 标准库实现，不需要 `pip` 装任何东西

## 安装方式一：装进 TeleAgent（对话 + 界面双入口）

1. 找到 TeleAgent 的技能目录（一般在 `C:\Users\<你的用户名>\.config\TeleAgent\users\<你的用户ID>\skills\`）
2. 把 `doc-memory`、`file-guard` 两个文件夹整个复制进去
3. 重启 TeleAgent

之后对话里直接说这些话就能触发：

- "帮我找一下昨天存的那份文档，内容好像是讲……"
- "我忘了文件名了，只记得里面写了……"
- "帮我扫一下电脑有没有可疑文件"
- "这个 PDF 打不开了，能修吗"

也可以用命令行注册（效果相同，`add_skill_remote.py` 在 TeleAgent 自带技能 skill-creator 的 scripts 目录下）：

```
python add_skill_remote.py <技能目录>\doc-memory\SKILL.md
python add_skill_remote.py <技能目录>\file-guard\SKILL.md
```

## 安装方式二：不装 TeleAgent，双击直接用

1. 下载 zip（或 clone 本仓库），解压到任意目录，比如 `D:\`
2. 双击 `doc-memory\scripts\run_web_ui.bat` → 浏览器自动打开 `http://127.0.0.1:8765`
3. 双击 `file-guard\scripts\run_web_ui.bat` → `http://127.0.0.1:8766`

文档拾忆第一次启动会弹出授权确认，同意后开始建立索引（视文件数量需要几分钟到半小时，之后增量更新很快）。首次授权时可以勾选"创建桌面快捷方式"，以后双击桌面图标即可。

## 命令行用法

文档拾忆（在 `doc-memory\scripts\` 下执行）：

```
python indexer.py scan               # 建立/增量更新索引
python searcher.py "关键词"           # 搜索
python searcher.py "关键词" --json    # JSON 输出（供程序解析）
python selfcheck.py                  # 索引自检
```

文件卫士（在 `file-guard\scripts\` 下执行）：

```
python scanner.py            # 重点目录快扫（秒级）
python scanner.py --full     # 全盘深扫
python scanner.py --json     # JSON 输出
```

## 目录结构

```
├── README.md
├── .gitignore
├── doc-memory/             # 文档拾忆
│   ├── SKILL.md            # 技能描述（TeleAgent 读取）
│   ├── README.md           # 详细使用说明
│   └── scripts/
│       ├── web_ui.py       # 网页界面（含宠物/签到/商店/天气装扮）
│       ├── indexer.py      # 索引器
│       ├── searcher.py     # 搜索器
│       ├── selfcheck.py    # 自检
│       ├── pet_belly.py    # 宠物肚子（安全删除暂存/恢复）
│       ├── pet_shop.py     # 签到/商店/天气
│       ├── run_web_ui.bat  # 双击启动
│       ├── doclogo.ico     # 桌面快捷方式图标（文档+∞ 透明底）
│       ├── ocr.ps1         # 图片文字识别（Windows 自带引擎，本地不出网）
│       ├── meteor2.ico
│       └── vendor/pypdf/   # 内置 PDF 解析库
└── file-guard/             # 文件卫士
    ├── SKILL.md
    ├── README.md
    └── scripts/
        ├── web_ui.py       # 网页界面
        ├── scanner.py      # 启发式扫描引擎
        ├── repair.py       # 破损文件修复
        ├── run_web_ui.bat
        └── shield.ico
```

## 隐私

- 所有数据（索引、扫描记录、配置）保存在各自技能的 `scripts/data/` 目录，删掉该目录即彻底清除
- 服务只监听 `127.0.0.1`，本机之外谁也访问不到
- 不上传任何数据。唯一的联网请求是文档拾忆的天气装扮查询：需要你在弹窗里明确授权，可以填城市也可以拒绝；拒绝后完全不发任何请求
- 文件卫士的隔离走 Windows 系统回收站，误报随时还原

## 常见问题

**搜不到图片/视频/压缩包的内容？** 正常。这类文件没有可提取的文本，只能按文件名搜索。

**索引里全是软件依赖文件？** 不会。文档拾忆自动跳过 Anaconda / node_modules 等依赖目录和 dll / 字体等纯二进制（38 类目录 + 52 类扩展名内置忽略，可在 config.json 自定义追加），只收值得搜的文档与代码——索引小一半以上、扫描更快，`python indexer.py status` 可查看生效的忽略规则。

**PDF 只搜前 40 页？** 是的，为了保证索引速度，绝大多数文档的核心内容都在前 40 页。

**文件卫士报了可疑文件，我该删吗？** 先看风险卡片上的原因说明。隔离是送回收站不是直接删除，不确定就先放着，或用专业杀软复扫确认。反复出现的已知项可以加入忽略名单，之后扫描与统计都不再打扰。

**支持 Mac / Linux 吗？** 目前只支持 Windows（启动脚本、回收站隔离、启动项检查都依赖 Windows 机制）。

> AI生成
