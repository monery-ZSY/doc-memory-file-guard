# 文件卫士（file-guard）

Windows Defender 之外的第二道防线：用启发式规则识别"长得像病毒"的文件。

它不是专业杀软（没有病毒特征库，加壳变种可能漏报），专抓那些一眼就该怀疑的东西，和专业杀软并用效果最好。纯本地运行，不联网，数据不出本机。

## 检测什么

| 特征 | 级别 | 例子 |
|---|---|---|
| 双扩展名伪装 | 高危 | `发票.pdf.exe`——看着是文档，实际是可执行程序 |
| 盘符根目录 autorun.inf | 高危 | U 盘蠕虫残留 |
| 启动项指向临时目录 | 高危 | 注册表 run 键指向 temp，典型驻留手法 |
| 高危扩展名出现在下载/桌面 | 中危 | `.scr` / `.hta` / `.vbs` 等 |
| 诱导性命名 | 中危 | 名字带"破解""激活""注册机""外挂" |
| 可疑快捷方式 | 中危 | 调用 `powershell -enc` / `mshta` 或指向临时目录 |
| 无签名的可执行 | 低-中危 | 投递位置里的小型未签名 PE |
| 带宏文档 | 提示 | docm / xlsm 等 |
| 商业壳特征 | 低危 | VMP / Themida 加壳 |
| hosts 篡改 | 低危 | 非本地映射条目 |
| **文件破损** | 中危 | 下载中断 / 存储损坏导致打不开的 PDF / ZIP / Office / 图片 |

## 特色：破损文件修复

下载到 99% 断掉的 PDF、解压报错的 ZIP/docx/xlsx/pptx 打不开？可以尝试修复——重新扫描文件内部结构，重建交叉引用表或中央目录，**另存为「原名(修复).ext」新文件，原文件永远不动**。不保证 100% 完整（损坏点的数据无法凭空恢复），但多数结构破损能救回来。

## 环境要求

- Windows 10 / 11
- Python 3.8 或更高（安装时勾选 **Add Python to PATH**）
- 纯 Python 标准库实现，零第三方依赖

## 安装

### 方式一：装进 TeleAgent

把整个 `file-guard` 文件夹复制到 TeleAgent 的技能目录（一般在 `C:\Users\<你的用户名>\.config\TeleAgent\users\<你的用户ID>\skills\`），重启 TeleAgent。之后直接对话：

- "帮我扫一下电脑有没有可疑文件"
- "检查下载目录里的东西安全吗"
- "这个 PDF 打不开了，能修吗"

命令行注册（可选，效果相同）：

```
python add_skill_remote.py <技能目录>\file-guard\SKILL.md
```

`add_skill_remote.py` 在 TeleAgent 自带技能 skill-creator 的 scripts 目录下。

### 方式二：不装 TeleAgent，双击直接用

解压到任意目录（比如 `D:\file-guard`），双击 `scripts\run_web_ui.bat`，浏览器自动打开 `http://127.0.0.1:8766`。

## 网页界面怎么用

- **快扫**：秒级，覆盖下载/桌面/启动项/hosts 等重点位置，日常体检够用
- **全盘深扫**：所有本地磁盘
- 风险卡片用大白话解释**为什么可疑**（命中了哪条规则），单个或批量**一键送回收站**（可还原，不是直接删除）
- 破损文件点"尝试修复"，修复记录可查
- 不想再被某个文件打扰，加**忽略名单**——之后扫描与统计都不再出现（v1.1.0 起计数口径与清单一致，已忽略项不计入）

## 命令行

在 `scripts\` 目录下执行：

```
python scanner.py            # 重点目录快扫
python scanner.py --full     # 全盘深扫
python scanner.py --json     # JSON 输出（供程序解析）
python scanner.py --level high   # 只看高危及以上
```

## 隐私

- 扫描记录、隔离记录、忽略名单全部保存在 `scripts/data/`，删除该目录即彻底清除
- 服务只监听 `127.0.0.1`，本机之外访问不到
- 隔离走 Windows 系统回收站（SHFileOperation + ALLOWUNDO），误报随时从回收站还原

> AI生成
