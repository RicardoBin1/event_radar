# A 股事件雷达

全市场公告 + 快讯监控，命中"事件驱动型异动"关键词后推送到微信。
零成本、云端定时运行（GitHub Actions 每 15 分钟），不做代码白名单。

**项目位置：`E:\Code\event_radar`**（不占 C 盘；本地 venv 也在 E 盘）

---

## 1. 它抓什么

两个源，任一挂掉不影响另一个：

| 源 | 接口 | 用途 |
|---|---|---|
| 巨潮资讯 | `hisAnnouncement/query`（POST form） | 全市场公告原文，权威 |
| 财联社 | `/api/cache?name=telegraph` | 快讯，速度快 |

事件分三级：

| 级别 | 类型 | 推送方式 |
|---|---|---|
| P0 | 重组/资产注入、停牌核查、立案、异动问询 | 命中即单条推 |
| P1 | 连板、停复牌、合同中标、业绩预告 | 每 4 次运行（约 1 小时）合并一条 |
| 噪音 | 股东大会、董事会决议、质押、减持计划等 | 丢弃 |

P0 命中时不再走噪音黑名单——避免"关于召开股东大会审议重大资产重组方案的公告"被误杀。

---

## 2. 部署

### 2.1 上传到 GitHub

```bash
cd E:\Code\event_radar
git init
git add .
git commit -m "init event radar"
git branch -M main
git remote add origin https://github.com/<你的用户名>/event_radar.git
git push -u origin main
```

### 2.2 配置 Secret

仓库 → Settings → Secrets and variables → Actions → New repository secret

- Name: `PUSHPLUS_TOKEN`
- Value: PushPlus 的 token（pushplus.plus 微信扫码登录后获取）

### 2.3 启用 Actions

仓库 → Actions → 左侧 `event-radar` → Enable workflow。

GitHub 对新建仓库的定时任务有概率要求"先手动跑一次"才正式生效，所以：

### 2.4 手动触发验证

Actions → event-radar → Run workflow → Run。

看日志应该出现：

```
巨潮公告 N 条（窗口 ...）
财联社电报 M 条
命中 P0 x 条，P1 y 条
完成：P0 x / P1 y / 今日已推 z 条
```

最后一步 `Persist state` 会把 `data/state.json` 提交回仓库——这是去重状态，**不要删除这个文件**。

---

## 3. 如何调整关键词库

只改 `config.py`，不用碰 main.py。

```python
P0_RULES = [
    {
        "tag": "重组",                      # 推送标题里显示 [P0·重组]
        "keywords": ["重大资产重组", ...],   # 任一命中即可
        "pairs": [("收购", "股权")],         # 或者两个词同时出现
        "require_any": [],                  # 还必须额外命中其中之一
        "exclude": [],                      # 命中任一则否决
    },
]
```

四种匹配条件同时生效（AND 关系）：

| 字段 | 含义 |
|---|---|
| `keywords` / `pairs` | 二者满足其一即算命中主体 |
| `require_any` | 在主体命中的基础上，还要出现其中之一 |
| `exclude` | 出现任一则直接否决，优先级最高 |

举例：只想加"破产重整"，在重组规则的 `keywords` 里加一个字符串即可。

改完提交：

```bash
git add config.py && git commit -m "tune keywords" && git push
```

下一次定时运行（≤15 分钟）自动生效。

**想立刻验证匹配效果，不用等推送**：

```bash
E:\Code\event_radar\.venv\Scripts\python.exe main.py --offline --dry-run
```

---

## 4. 如何查看当日推送计数

三个位置：

**（1）Actions 日志最后一行**

```
完成：P0 3 / P1 5 / 今日已推 6 条
```

**（2）仓库里的 data/state.json**

```json
{
 "daily_date": "2026-10-19",
 "daily_count": 6,
 "p1_buffer": [],
 "p1_runs": 12
}
```

`daily_count` 就是当日已推条数。跨日会自动归零。

**（3）本地看**

```bash
type data\state.json
```

当日推送达到 `DAILY_LIMIT`（默认 180）后：
- P0 照常推
- P1 不再逐小时推，转入 `carry` 字段，次日第一次运行补发一条汇总

---

## 5. 如何手动清空状态重跑

**场景**：调试关键词后想重新跑一遍，或状态文件写坏了。

```bash
cd E:\Code\event_radar
.venv\Scripts\python.exe main.py --reset
git add data/state.json && git commit -m "reset state" && git push
```

清空后下一次运行会回溯最近 30 分钟，重新抓一遍。

**云端清空**：直接在 GitHub 网页上编辑 `data/state.json`，内容改成 `{}` 即可。

**只想把积压的 P1 立刻推出来**（不重置、不丢历史）：

```bash
.venv\Scripts\python.exe main.py --flush
```

---

## 6. 本地调试

```bash
cd E:\Code\event_radar

# 不联网，用内置样本验证分类逻辑（改了关键词后先跑这个）
.venv\Scripts\python.exe main.py --offline --dry-run

# 联网抓取，但不推送
.venv\Scripts\python.exe main.py --dry-run

# 正常跑一次（需要 PUSHPLUS_TOKEN 环境变量）
set PUSHPLUS_TOKEN=你的token
.venv\Scripts\python.exe main.py
```

`--dry-run` 下会打印每条 P0 的标题，但不消耗 PushPlus 额度。

---

## 7. 已知限制

1. **每 15 分钟一批，不是实时。** GitHub cron 高峰期可能延迟 5–15 分钟。要更快只能换云服务器常驻。
2. **单次翻页有上限**（`MAX_PAGES=10`）。15 分钟窗口足够；如果把 `FIRST_RUN_LOOKBACK_MIN` 调大，首次运行可能截断。年报季 8:00–9:00 公告密集，必要时把 `MAX_PAGES` 调到 20。
3. **财联社接口无签名校验时可能被限流**，失败会打日志并跳过，不阻塞流程。
4. **电报里股票名提取是启发式的**（6 位代码或"XX："前缀），提取不到就留空，不影响推送。
5. **同一股票同一事件类型 24 小时只推一次**。好处是不刷屏，代价是同一重组的后续进展 24 小时内不会重复提醒。

---

## 8. 排查

| 现象 | 先看这里 |
|---|---|
| 没收到推送 | Actions 日志里 `命中 P0 0 条`？那就是真没事件；有命中但没推 → 检查 Secret |
| `未配置 PUSHPLUS_TOKEN` | Secret 名字拼错，或没在 workflow 里传 env |
| 巨潮 0 条 | 正常，夜间/非交易时段公告少；用 `--dry-run` 看窗口时间对不对 |
| 重复推送 | `Persist state` 步骤失败了，去 Actions 看 git push 是否被拒 |
| 推送太吵 | 把 P0 规则里噪音大的词挪到 P1，或加进 `NOISE_KEYWORDS` |

---

## 9. 文件说明

```
event_radar/
├── main.py                     抓取 + 匹配 + 去重 + 推送
├── config.py                   事件规则、限额、接口地址
├── requirements.txt            requests / beautifulsoup4
├── data/state.json             去重状态与计数（会被自动提交）
└── .github/workflows/monitor.yml   每 15 分钟定时任务
```
