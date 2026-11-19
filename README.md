# 全市场量能监控（macOS 状态栏 / Windows 悬浮框）

对标同花顺"大盘量能"页的轻量小工具：常驻 **macOS 状态栏** 或 **Windows 无边框悬浮框**，
实时显示 沪/创/科 三大指数、全市场（沪+深+北）成交额与量能增减、涨跌停家数、预测全天成交额，
还能添加若干只**自选股**，与指数一起**轮播**（现价 / 涨跌幅 / 昨成交额 / 今成交额 / 当前增减 / 预计成交额）。

纯标准库取数（腾讯行情 + 东财涨停池），两端共用同一套口径；**macOS 端为原生 AppKit 状态栏 + 原生设置窗口，
Windows 端是 tkinter 悬浮框**，除 GUI 外逻辑完全一致。

```
指数视图：沪3563.3 -0.68% 创2283.7 -1.21% 科1239.6 -0.68% 5030.4亿 ↑ -132亿 预1.34WY 16/15
个股视图：浦发银行 10.69 -0.28% 昨7.1亿 今1.9亿 +320万 预6.9亿
```

> 配色遵循 A 股习惯：**涨/放量为红，跌/缩量为绿**；个股卡片里的「昨 / 预 / 量」用亮黄 `#FFD54A`。

设置窗口（macOS 原生 AppKit，Windows 为 tkinter）：`docs/settings-window.png`

---

## 功能

| 模块 | 说明 |
|---|---|
| **全市场量能** | 成交额 = 上证指数(沪) + 深证综指(深) + 北证50(北)，对比**昨日同一时刻真实累计额**（读腾讯5日分时，不用线性估算） |
| **三大指数** | 沪=上证指数、创=创业板指、科=科创50，各自着色的点位 + 涨跌幅 |
| **涨跌停家数** | 东财涨停池/跌停池 `tc` 总数，标题末尾 `28/9`（红=涨停、绿=跌停）；跌停 > 10 家时数字每分钟闪一下告警 |
| **预测全天额** | 今日累计 ÷ (昨日同时刻占昨日全天比例)，收盘后即实际值 |
| **成交量对比** | 沪深口径，今日累计 vs 昨日同时刻（昨日1分钟线对齐） |
| **个股轮播** | 设置页添加多只股票，与指数按固定间隔（默认 **10 秒**）循环播放；每只在菜单里另有「较昨日同时刻 / 昨量 / 预测量」明细 |
| **故障自愈** | 接口抽风时状态栏显示「更新失败 Ns后重试 旧HH:MM」，连续失败自动降频到 30 秒，恢复后自动回到 10 秒 |
| **简洁模式** | 标题宽度减半（点位取整、去掉成交额与预测），防止被宽菜单 App 挤出状态栏 |

## 快速开始

### macOS（状态栏）

```bash
git clone https://github.com/SeptemberL/MarketVolume.git
cd MarketVolume

# 建独立环境并装依赖（rumps 依赖 PyObjC）
python3 -m venv venv
./venv/bin/pip install -U pip rumps pyobjc-framework-Cocoa

./run.sh          # 前台启动，状态栏出现标题即成功；菜单里选「退出」结束
```

开机自启（推荐 launchd，**不要**用 nohup/setsid 常驻，否则进程脱离图形会话、图标会消失）：

见 [`AI_SETUP.md`](AI_SETUP.md) 第 4 步，里面有可直接替换路径的 plist 模板与 `launchctl` 命令。

### Windows（悬浮框）

1. 装 Python 3.8+（官方安装包自带 tkinter，**无需 pip 安装任何包**），勾选 "Add Python to PATH"
2. 双击 `run_win.bat`（自动探测 `pythonw`/`python`/`py`，无控制台窗口）
3. 开机自启：`Win+R` → `shell:startup` → 把 `run_win.bat` 的**快捷方式**放进去

悬浮框用法：左键拖拽移动（位置自动记忆）、点 `✕` 退出、**右键菜单**里有「展开详情 / 立即刷新 / 设置… / 简洁模式 / 退出」。

## 设置页：监视股票 / 切换间隔

| 平台 | 入口 |
|---|---|
| macOS | 点状态栏图标 → 菜单底部「设置…（监视股票 / 切换间隔）」 |
| Windows | 悬浮框右键 → 「设置…（监视股票 / 切换间隔）」 |

- **添加股票**：输入 `600000` / `sh600000` / `平安银行`，自动补市场后缀并解析名称；列表里可 `↑` `↓` 调顺序、`×` 删除
- **切换间隔**：指数 ↔ 个股的循环周期，**默认 10 秒**（1~120 秒）
- **显示成交量**：勾选后个股标题末尾追加 `量17.5万手`

配置写在工具目录 `.settings.json`（本地状态文件，不进 git），两端读同一份，保存即生效：

```json
{
 "cycle_sec": 10,
 "show_volume": false,
 "stocks": [
  {"code": "sz300308", "name": "中际旭创"}
 ]
}
```

- macOS 设置窗口在**同一进程内**，点保存立刻重抓个股并重绘标题
- Windows 设置窗口是独立进程，主程序每 1 秒对比文件 mtime 热加载
- 手动改 `.settings.json` 也生效（1 秒内），不用重启

## 指标口径

- **全市场成交额** = 沪（上证指数）+ 深（深证综指）+ 北（北证50）。⚠️ 创业板指/科创50 与沪深成交额高度重叠，
  **只用于点位展示，绝不并入聚合**，否则总量重复计算
- **量比（主判定）** = 今日累计成交额 / **昨日同一时刻真实累计额**；≥1.03 放量（红）、≤0.97 缩量（绿）
- **较昨日差额** = 今日 − 昨日同时刻；收盘后即实际全天差
- **预测全天额** = 今日累计 ÷ (昨日同时刻占昨日全天的比例)；收盘后即实际值
- **分钟节奏**：每分钟采样累计成交额，本分钟增量 vs 上分钟增量（纯对比，无平量死区）；
  节奏翻转记录在菜单「节奏转换历史」里
- **涨跌停家数**：东财涨停池/跌停池 `tc`，数据日期取**最新交易日**（非交易日东财返回空池 → 该段自动省略）
- **个股**：昨成交额取日K前一根（`r[8]`，万元）；昨日同时刻取5日分时；
  「当前增减」= 较上一次刷新的增量（与「较昨日同时刻」是两个口径，后者在菜单里）

## 数据源

| 用途 | 接口 |
|---|---|
| 实时行情（指数/个股/多码一次） | `https://qt.gtimg.cn/q=sh000001,sz399106,bj899050,sz399006,sh000688` |
| 日K | `https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=<code>,day,,,5,qfq` |
| 1分钟K（成交量对比） | `https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=<code>,m1,,800` |
| 5日分时（昨日同时刻累计额） | `https://web.ifzq.gtimg.cn/appstock/app/day/query?code=<code>` |
| 涨停池/跌停池（东财） | `https://push2ex.eastmoney.com/getTopic{ZT,DT}Pool?...&pagesize=1&date=YYYYMMDD` |
| 代码/名称联想（设置页） | `https://smartbox.gtimg.cn/s3/?q=<关键词>&t=all` |

腾讯字段：`p[1]`名 `p[3]`现价 `p[4]`昨收 `p[6]`量(手) `p[30]`时间 `p[32]`涨跌% `p[37]`成交额(万元)。

## 常见问题

- **`Connection refused` / SSL 证书报错**：程序启动时会**强制清空** `HTTP_PROXY` 等代理变量（直连腾讯）。
  若在带代理的环境启动，请先 `unset HTTP_PROXY HTTPS_PROXY ALL_PROXY` 再运行
- **macOS 状态栏图标消失**：不是崩溃。切到菜单项多的 App（Finder/浏览器）时图标会被挤出屏幕，
  开**简洁模式**、或按住 `Cmd` 把图标拖到最右边靠近时钟的位置
- **点「设置…」窗口没出来**：状态栏 App 的激活策略是 Accessory，窗口容易被压在别的窗口后面。
  本版本已临时切 Regular + 浮动窗口层级处理；仍在排查的话看日志
  `~/Library/Logs/market-volume.log` 里 `[设置] 窗口 visible=… level=… policy=…` 一行
- **macOS 上 `import tkinter` 报 `Cannot find a usable init.tcl`**：本机 Python 的 Tcl 运行时不完整。
  本工具 macOS 端的设置窗口走**原生 AppKit**，不依赖 tkinter；tkinter 只用于 Windows
- **命令行快速自检**：`./venv/bin/python volume_core.py` 打印一行摘要 + 完整 JSON 快照；
  `./venv/bin/python selftest.py` 跑离线断言（不需要网络）

## 用 AI 安装 / 配置

如果你想让 AI 助手（Claude / ChatGPT / Codex / 本机 Agent）帮你装好、配好、排障，
把 [`AI_SETUP.md`](AI_SETUP.md) 整个文件丢给它，或直接让它读仓库里的这个文件。里面有：
前置检查、逐步命令、每步的期望输出、launchd/开机自启、`.settings.json` 配置格式、故障排查表、
以及一份可以直接粘贴的 prompt。

## 文件结构

| 文件 | 职责 |
|---|---|
| `volume_core.py` | 核心：取数 + 计算 + 配置读写（纯标准库，两端共用，可 `python3 volume_core.py` 直接跑） |
| `volume_menu.py` | macOS 状态栏 App（rumps + PyObjC） |
| `volume_float_win.py` | Windows 悬浮框 App（tkinter，无第三方依赖） |
| `settings_mac.py` | macOS 设置窗口（原生 AppKit，同进程，保存即生效） |
| `settings_win.py` | Windows 设置窗口（tkinter，独立进程 + mtime 热加载） |
| `selftest.py` | 离线自检（断言核心文案/口径/配置读写，不需要网络） |
| `run.sh` / `run_win.bat` | 一键启动脚本 |
| `docs/settings-window.png` | 设置窗口截图 |

## 已知限制

- 北证无 1 分钟线，"同时刻成交量对比"为**沪深口径**（北证成交额占比很小，误差可忽略）
- 涨跌停家数来自东财（唯一非腾讯数据源），接口抽风时该段消失，不影响量能主逻辑
- 个股每只需 2 条附加请求（日K + 昨日分时），按「代码 + 自然日」缓存，盘中不重复拉取
- 节假日判断基于"最新K线日期是否为今天"，非交易日不会误判

---

*仅供个人学习使用，不构成投资建议。数据来自腾讯行情/东方财富，接口随时可能变化。*
# MarketVolume
