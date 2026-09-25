# EventsAutomation

[English](README.md) · **中文**

Tech Week 这类活动周一次有上千场周边活动，手动挑选、报名要花好几个小时。这个工具帮你做完整个流程：

1. **抓取**：拉下日历上的全部活动。目前支持 [a16z Tech Week](https://www.tech-week.com)，包括 SF、NYC、LA、Boston。
2. **打分分梯队**：按**你自己的**背景（兴趣方向、是否管饭、离住处远近）打分，再分成“真正能去的”几个梯队。
3. **网页勾选**：在本地网页上点选，不用读任何文件。
4. **自动报名**：在 Partiful 上自动报名，主办方的问卷按你的资料自动填写。
5. **核实与日历**：每场报名结果都对照你的 Partiful 账号核实，并跟踪审批状态；通过的活动可以写进日历。

---

## 快速开始

需要 Python 3.10 或以上。

```bash
git clone <本仓库> && cd EventsAutomation
pip install -r requirements.txt
playwright install chromium

mkdir SF_2026 && cp profile.example.yaml SF_2026/profile.yaml
# 编辑 SF_2026/profile.yaml：个人信息、兴趣关键词、常见问题的答案
```

**工作区**：每个活动周建一个文件夹，比如 `SF_2026/`。里面放你的 `profile.yaml` 和工具缓存的全部数据，整个文件夹都不会进 git。

### 第 1 步：抓取和打分（约 1 小时，无需值守）

```bash
python -m eventsauto fetch   --ws SF_2026 --city sf   # 抓全部日历（约 1700 场）
python -m eventsauto links   --ws SF_2026             # 对符合你背景的活动解析报名链接
python -m eventsauto details --ws SF_2026             # 抓完整描述和主办方问卷
python -m eventsauto rank    --ws SF_2026             # 打分、分梯队
```

`links` 故意跑得很慢：大约每 7 秒请求一次，这是网站的限流速度，而且只请求和你背景相关的活动。每一步的结果都有缓存，中断后重跑会接着上次的进度。

### 第 2 步：登录 Partiful（只需一次）

```bash
python -m eventsauto login --ws SF_2026
```

会弹出一个浏览器窗口，你自己用手机号和短信验证码登录，工具接触不到这些信息。登录状态保存在 `SF_2026/.browser-profile/`。Partiful 网页版的登录偶尔会过期，过期时登录窗口会自动再弹出来。

```bash
python -m eventsauto sync --ws SF_2026   # 导入你之前手动报过的活动
```

### 第 3 步：在网页上挑选

```bash
python -m eventsauto serve --ws SF_2026   # 打开 http://localhost:8765
```

- **日期卡片**：显示每天的“主选”场数和你设定的上限（`per_day`），超出会标红。
- **勾选**：勾上或取消勾选，**立即保存**。
- **命中关键词**：每一行都列出命中的关键词，一眼能看出这场为什么得这个分。
- **「试填（不提交）」**：把所有表单填好并截图，但不提交。结果在 `data/dryrun.json`，标着“需补充信息”的场次需要你补答案。
- **「正式报名已勾选」**：正式提交。已报过的会自动跳过，结束时逐场对照你的账号核实。
- **「同步 Partiful 状态」**：刷新审批结果。

界面语言默认跟随浏览器，中文浏览器显示中文，右上角按钮可以切换中英文。

### 第 4 步：写入日历

```bash
python -m eventsauto calendar --ws SF_2026            # 生成 SF_2026/registered.ics，可导入任意日历
python -m eventsauto calendar --ws SF_2026 --google   # 同时写入 Google Calendar
```

用 `--google` 需要先准备 OAuth 凭据：在 Google Cloud Console → APIs & Services → Credentials 创建一个 *Desktop app* 类型的 OAuth 客户端，把凭据文件放到 `SF_2026/secrets/credentials.json`。还在待审批的活动，标题前会加 `[pending]`。

---

## 打分规则

全部在 `profile.yaml` 里配置，参考 [`profile.example.yaml`](profile.example.yaml)：

| 配置项 | 作用 |
|---|---|
| `interests` | 每个兴趣方向是一个正则。标题、主办方或描述命中时，加上它的 `weight` 分。 |
| `interests.*.bridge` | 有条件的兴趣。比如机器人类活动，只有同时涉及你能切入的话题（仿真、数据集等）才加分，否则扣分。 |
| `avoid`、`far` | 不想要的话题扣分；离住处太远的地方扣分。 |
| `food` | 描述里提到餐食或酒水时加分。 |
| `answers` | 主办方问题的答案，是一个有序的 `[正则, 答案]` 列表。下拉题的答案必须和某个选项对得上。 |

分梯队是整个工具的核心：

- **A 主选**：你真正要去的。按分数从高到低挑，时间不冲突（留 30 分钟路上时间），每天不超过 `per_day` 场。你已报名的活动也参与竞争，所以页面会告诉你审批通过后该放弃哪些。超过 3 小时的活动按“中途去待 3 小时”计算。
- **B 候补**：给需审批的活动配的同时段备选，默认每场主选配 3 个（`--backups`），因为不是每个主办方都会通过。
- **C 其他**：其余匹配的活动，用「最低分」滑块控制显示多少。

第一次正式报名之后，新出现的推荐不会再自动勾选，要你自己勾。

## 命令一览

| 命令 | 作用 |
|---|---|
| `fetch` | 下载整个日历。 |
| `links` | 按背景初筛，逐个解析报名链接（慢，可断点续跑）。 |
| `details` | 抓 Partiful 页面：描述、地点、问卷。 |
| `rank` | 打分、分梯队，生成 `picks.yaml` 和 `shortlist.md`。可调参数：`--per-day`、`--backups`、`--a-min`、`--b-min`。 |
| `login` | 打开浏览器登录 Partiful。 |
| `sync` | 拉取你在 Partiful 上的报名和审批状态，存到 `data/mine.json`。 |
| `serve` | 启动本地挑选网页，默认端口 8765（`--port` 可改）。 |
| `register` | 报名已勾选的活动。`--dry-run` 只填不交，`--only id1,id2` 只报指定场次。 |
| `calendar` | 导出已报名活动：生成 `.ics`，加 `--google` 同时写入 Google Calendar。 |

## 隐私与礼仪

- 个人数据只存在本机，并已加入 `.gitignore`：`profile.yaml`、`personal_info.txt`、`data/`、`.browser-profile/`、`secrets/`、`*.ics`、`picks.yaml`。
- 请求是逐个发出的，间隔 3–8 秒，遇到 HTTP 429 限流会自动退避。
- 工具操作的是你自己的账号，替你填本来要手动填的表。请只报你打算去的活动，去不了的及时退掉，把名额留给别人；使用前也请看一下各平台的服务条款。

## 许可证

[MIT](LICENSE)
