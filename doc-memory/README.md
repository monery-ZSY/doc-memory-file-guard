# 文档拾忆（doc-memory）

忘了文件名、忘了存哪，只记得内容大概讲什么？用内容关键词把文件找回来。

昨天保存的论文、上周写的方案、不知道谁发来的文件——只要正文里出现过的词，都能搜到。结果直接告诉你：文件在哪、什么时候改的、命中了哪句话。

## 环境要求

- Windows 10 / 11
- Python 3.8 或更高（安装时勾选 **Add Python to PATH**）
- 不需要安装任何第三方库：PDF 解析用的 pypdf 已内置在 `scripts/vendor/`，其余全部用 Python 标准库实现

## 安装

### 方式一：装进 TeleAgent

把整个 `doc-memory` 文件夹复制到 TeleAgent 的技能目录（一般在 `C:\Users\<你的用户名>\.config\TeleAgent\users\<你的用户ID>\skills\`），重启 TeleAgent。之后直接对话：

- "帮我找昨天存的那份文档，内容好像是讲……"
- "我忘了文件名了，只记得里面写了……"

命令行注册（可选，效果相同）：

```
python add_skill_remote.py <技能目录>\doc-memory\SKILL.md
```

`add_skill_remote.py` 在 TeleAgent 自带技能 skill-creator 的 scripts 目录下。

### 方式二：不装 TeleAgent，双击直接用

解压到任意目录（比如 `D:\doc-memory`），双击 `scripts\run_web_ui.bat`，浏览器自动打开 `http://127.0.0.1:8765`。

## 网页界面怎么用

1. **首次启动**会弹出授权确认，同意后开始建立索引。全盘扫描视文件数量需要几分钟到半小时，之后每天增量更新，很快。授权时可以勾选"创建桌面快捷方式"。
2. **搜索**：输入内容关键词即可，结果按匹配度排序，卡片上显示路径、修改时间、命中片段，可一键打开文件或定位到所在文件夹。
3. **删错了不怕**：把结果卡片拖到左下角宠物身上，文件会被"吃掉"暂存 24 小时，期间双击宠物肚子就能吐回原位；超过 24 小时才彻底删除（上限 99 个）。
4. 其他功能：**检索历史**（防意外关闭丢关键词）、**找相似**（同目录邻居 + 全库文件名相似）、**中英文文件名筛选**、**年份分布轴**、每日签到 / 宠物商店 / 天气装扮、**数据库面板**（索引统计、重复文件榜）。

## 索引保持精简（v1.1.0）

自动忽略对"找回文档"没有价值的东西，索引又快又小（实测从 24.6 万条瘦身到 11.2 万条，体积缩小 63%）：

- **依赖目录不收录**：Anaconda / pkgs / node_modules / .git / venv / site-packages 等 38 类
- **纯二进制不收录**：dll / exe / enc / zip / 字体 / pyc 等 52 类

想追加自己的忽略项，编辑 `scripts/data/config.json`：

```json
{
  "roots": ["D:\\你的目录"],
  "ignore_dirs": ["MyHugeSDK"],
  "ignore_exts": ["psd", ".avi"]
}
```

大小写不敏感，扩展名带不带点都行，下次扫描生效；`python indexer.py status` 可查看当前生效的忽略规则统计。

## 命令行

在 `scripts\` 目录下执行：

```
python indexer.py scan               # 建立/增量更新索引
python indexer.py status             # 查看索引与忽略规则统计
python searcher.py "关键词"           # 搜索
python searcher.py "热失控 预警" --all # 多词 AND
python searcher.py "报告" --type pdf --within 7d   # 限类型/时间
python selfcheck.py                  # 索引自检
```

## 能搜到什么

| 类型 | 搜索方式 |
|---|---|
| txt / md / 代码 / csv / json / html / tex | 全文 |
| docx / pptx / xlsx | 正文 |
| pdf | 前 40 页正文 |
| 图片 / 视频 / 压缩包 | 只按文件名（无法提取内容属正常） |
| dll / exe / enc / 字体等纯二进制 | 不收录（可在 config.json 调整） |

## 隐私

- 索引与快照全部保存在 `scripts/data/`，删除该目录即彻底清除
- 服务只监听 `127.0.0.1`，本机之外访问不到
- 唯一的联网请求是天气装扮查询，需要你在弹窗里明确授权（可填城市、可拒绝）；拒绝后完全不发任何请求

> AI生成
