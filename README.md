# B站用户评论爬虫

输入昵称 → 自动找到 UID → 抓取候选视频评论区（含楼中楼、图片评论）→ 筛出**该用户本人发的评论**，
支持时间段过滤、多线程并发，导出 CSV / JSON。仅用 Python 标准库，无需装任何包。

---

## 图形界面（推荐）

双击 **`run_gui.bat`**，或：

```bat
python gui.py
```

打开图形界面后：填昵称（或 UID/链接）→ 选时间范围和数据源 → 点「开始爬取」，
下方实时显示日志，完成后可一键打开输出文件夹。

命令行版仍可用，见下。

---

## 快速开始（命令行）

```bat
:: 双击 run.bat，或：
cd /d D:\Projects\bilibili-comment-crawler
python bili_comments.py            # 交互式：输入昵称
python bili_comments.py 某用户      # 直接按昵称
python bili_comments.py --uid 12345
python bili_comments.py --url https://space.bilibili.com/12345
```

结果在 `output\` 目录：`昵称_UID_时间.csv`（Excel 直接打开）和 `.json`。

---

## 能抓到 / 抓不到什么

**能抓到的候选视频池**（从目标用户主页公开信息得到）：

| 来源 | 状态 |
| --- | --- |
| 他的投稿视频 | ✅ |
| 他的公开收藏视频 | ✅ |
| 他转发过的视频 | ✅ |
| 他点赞过的视频 | ❌ 私密，接口返回 -400 |
| 他投币过的视频 | ❌ 平台不公开投币记录 |

**重要限制**：B 站没有"按用户查其全部评论"的接口，也没有评论全局索引，所以**无法遍历全站所有视频**。
本脚本只能在这三类 + 你手动指定的视频里，按 UID 筛出该用户的评论。

---

## 常用用法

```bat
:: 限定时间段（推荐：时间越窄越稳、越不容易漏）
python bili_comments.py 某用户 --start 2026-09-06 --end 2026-09-06
python bili_comments.py 某用户 --start "2026-01-01 00:00:00" --end "2026-06-30 23:59:59"
python bili_comments.py 某用户 --days 30          # 最近 30 天

:: 只扫某类来源
python bili_comments.py 某用户 --no-own-videos --no-dynamic     # 只扫公开收藏
python bili_comments.py 某用户 --no-own-videos --no-fav         # 只扫转发

:: 额外指定任意视频
python bili_comments.py 某用户 --bvid BV1xxxx --bvid BV1yyyy
python bili_comments.py 某用户 --bvid-file videos.txt           # 文件每行一个视频
```

---

## 参数速查

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `昵称` / `--uid` / `--url` | 定位目标用户 | — |
| `--bvid` / `--bvid-file` | 额外指定任意视频 | — |
| `--no-own-videos` / `--no-fav` / `--no-dynamic` | 关闭某类来源 | 关 |
| `--start` / `--end` / `--days N` | 时间段过滤 | 无 |
| `--max-videos` / `--max-fav-videos` / `--max-dynamic` | 各类来源数量上限 | 30 / 100 / 50 |
| `--max-comment-pages` | 每个视频最多翻几页（每页 20 条） | 20 |
| `--max-sub-threads` / `--max-sub-pages` | 楼中楼展开上限 | 100 / 5 |
| `--workers` | 并发线程数 | 3 |
| `--rate` | 全局限速：每秒最多请求数 | 1.5 |
| `--cooldown` | 风控命中后全局冷却秒数 | 20 |
| `--sleep` | 额外请求间隔秒数 | 0.3 |
| `--early-stop` | 开启按时间提前停止（更快，可能漏老楼中楼） | 关 |
| `--cookie` | 登录 Cookie | 无 |
| `--out` | 输出目录 | `./output` |

---

## 输出字段

CSV 列：`UID、昵称、评论时间、评论内容、评论图片、点赞数、评论层级、稿件标题、稿件链接、评论ID、楼层`

- **评论图片**：该评论带的图片 URL（多张换行分隔）。纯图片评论的"评论内容"会标记为 `[图片评论]`。
- 评论内容里的 `[doge]`、`[大哭]` 等是 B 站表情的文本转义，网页上显示为表情图。

---

## 完整性与提前停止

- 默认**完整模式**：翻完每个视频的评论页，并展开楼中楼全量，尽量不漏。
- 加 `--early-stop` 可提速：扫到早于开始时间的评论就停止翻页，代价是可能漏掉"老楼层下最近新增的楼中楼"。

---

## 抗风控 / 提速要点（重要）

脚本已内置：自动生成 `buvid3/buvid4` 指纹 Cookie、**全局限速**（所有线程共享一个速率上限，避免并发把 B 站打爆）、**风控命中后全局冷却**（暂停所有请求几十秒再恢复）、指数退避重试、失败自动刷新 Cookie。

**想要"快 + 少限流"，只有一条正路：提供登录 Cookie。** 匿名请求有很低的硬限速（约 1~2 次/秒），再快必然被 `-799/-412` 拦截；登录态（SESSDATA）限速高得多，能开到每秒 5+ 次。

推荐组合：

```bat
:: 匿名（稳，较慢）
python bili_comments.py 某用户 --rate 1.5 --cooldown 20

:: 有登录 Cookie（快很多）
python bili_comments.py 某用户 --cookie "SESSDATA=xxx; buvid3=xxx" --rate 5 --workers 4 --sleep 0 --cooldown 10
```

被限流时的处理顺序：

1. 加 `--cookie`（最有效）
2. 降 `--rate`（如 0.8）、`--workers 1`
3. 缩小 `--max-*` 和时间范围
4. 过几分钟再试（限流是短时效的）

### 获取 Cookie

浏览器登录 bilibili.com → `F12` → **Network** 标签 → 刷新页面 → 点任意 `api.bilibili.com` 请求 →
**Headers** → 复制 `Cookie:` 整串（至少含 `SESSDATA`）。

---

## 免责声明

本项目仅供个人学习研究，禁止商业用途和侵犯他人权益的行为。使用者应自行遵守法律法规及平台规则，妥善保管 Cookie，不得传播抓取到的他人信息。本项目按“现状”提供，作者不对使用后果承担责任。
