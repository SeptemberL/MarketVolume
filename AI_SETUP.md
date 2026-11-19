# 用 AI 安装 / 配置这个工具（给 AI 助手看的执行手册）

> 这份文件是**写给 AI 助手**的：把它整个丢给 Claude / ChatGPT / Codex / 本机 Agent，
> 它就能自己完成安装、自启、配置、验收和排障。
> 人也可以照着做，只是不用管那些"给 AI 的提示"。

---

## 0. 直接复制给 AI 的一段话

```text
请帮我安装并配置 https://github.com/SeptemberL/MarketVolume 这个"全市场量能监控"工具。
它是 macOS 状态栏 / Windows 悬浮框的 Python 小程序，靠腾讯行情 + 东财涨跌停数据取数。

要求：
1. 先读仓库里的 AI_SETUP.md，严格按里面的步骤执行（不要自己发明安装方式）。
2. macOS 上必须建独立 venv 并装 rumps + pyobjc-framework-Cocoa；不要装图形库，设置窗口是原生 AppKit。
3. 装完用 selftest.py 和 volume_core.py 验证，把输出贴给我；取数失败要区分"没网/代理/接口抽风"。
4. 用 launchd 配开机自启（不要用 nohup/setsid），配好后 launchctl list 确认在跑。
5. 最后按我的要求写好 .settings.json（监视股票 + 轮播间隔），并告诉我怎么打开设置页继续加股票。
全程不要修改 .settings.json 之外的用户数据；不要动 volume_core.py 里的 MARKET_CODES 口径。
```

下面每一步都给了**可执行命令 + 期望输出 + 失败处理**，AI 只需按序执行。

---

## 1. 环境检查

```bash
uname -s                 # Darwin = macOS,  Linux/其他: 只支持 macOS / Windows 两端
python3 -V               # 需要 3.9+（3.12/3.13 均可）
python3 -c "import sys; print(sys.executable)"
```

要点：

- **macOS**：优先用官方/Homebrew 的 `python3` 建 venv。**不要**验证 tkinter 是否可用 —— macOS 端不用它
  （很多机器 `import tkinter` 会报 `Cannot find a usable init.tcl`，与本工具无关）。
- **Windows**：用官方安装包（自带 tkinter），`python -V` 能出结果即可。
- 需要能访问外网（腾讯行情 `qt.gtimg.cn`、东财 `push2ex.eastmoney.com`）。

## 2. 取得代码

```bash
git clone https://github.com/SeptemberL/MarketVolume.git
cd MarketVolume
ls        # 应看到 volume_core.py volume_menu.py volume_float_win.py settings_mac.py settings_win.py ...
```

没有 git 就让用户下载 ZIP 解压，效果一样（目录名随意）。

## 3. macOS：建环境 + 装依赖

```bash
python3 -m venv venv
./venv/bin/pip install -U pip rumps pyobjc-framework-Cocoa
```

- 只用这两个依赖（`rumps` 是状态栏框架，`pyobjc-framework-Cocoa` 是原生设置窗口要用的 AppKit/Foundation）。
- 网络慢可加镜像：`./venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -U pip rumps pyobjc-framework-Cocoa`
- 验证：`./venv/bin/python -c "import rumps, AppKit; print('deps ok')"` → 期望输出 `deps ok`

> **Windows 不需要任何 pip 依赖**，`volume_float_win.py` 只用 tkinter。

## 4. 首次运行验证（先别急着配自启）

```bash
./venv/bin/python selftest.py            # 离线断言，不需要网络
./venv/bin/python volume_core.py         # 真实取数，打印一行摘要 + JSON 快照
```

**期望输出**

`selftest.py` 末尾应出现：

```
全部通过  (N 项)
```

`volume_core.py` 第一行类似（时间/数值随行情变化）：

```
[交易中] 沪3563.3 -0.68% 创2283.7 -1.21% 科1239.6 -0.68% | 全市场 5030.4亿 | 昨 1.38万亿 | 缩量-132亿 | 预测全天 1.34万亿 | 涨停16 / 跌停15 | ...
```

**失败处理**

| 现象 | 判断 | 处理 |
|---|---|---|
| `URLError ... Connection refused` | 环境里有代理，程序已清空 `HTTP_PROXY`，但 shell 里还有别的拦截 | `unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy` 后重试 |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 代理 MITM / 系统根证书不对 | 关掉代理软件或装其根证书；这是环境问题，不是代码问题 |
| `nodename nor servname provided` | 断网或 DNS 挂了 | 联网后重试 |
| `IndexError: list index out of range`（日K 为空） | 腾讯日K 接口偶发抽风 | 直接重试 1~2 次；连续失败再查网络 |
| 非交易时段摘要较短 | 正常 | 盘前只显示昨日成交额，涨跌停段会缺席 |

GUI 冒烟（可选，会在屏幕上出现状态栏图标 / 悬浮窗）：

```bash
./run.sh                       # macOS：状态栏出现标题即成功，菜单里「退出」结束
python volume_float_win.py     # Windows
```

## 5. 开机自启

### macOS：launchd（唯一推荐方式）

> ⚠️ **不要**用 `nohup` / `setsid` / `&` 常驻：shell 会话结束后进程会被踢出图形会话，
> 状态栏图标消失（进程还活着但 UI 失联）。

把下面模板存成 `~/Library/LaunchAgents/com.<用户名>.volume-monitor.plist`，
**把两处绝对路径换成实际路径**（`which python3` 不用，用 venv 里的解释器）：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.septemberl.volume-monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/绝对路径/venv/bin/python3</string>
    <string>/绝对路径/volume_menu.py</string>
  </array>
  <key>WorkingDirectory</key><string>/绝对路径</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>/Users/你/Library/Logs/market-volume.log</string>
  <key>StandardErrorPath</key><string>/Users/你/Library/Logs/market-volume.log</string>
</dict>
</plist>
```

加载（**必须在该用户的图形会话里执行**，SSH 里执行会失败）：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.septemberl.volume-monitor.plist
launchctl list | grep volume-monitor        # 第二列是 PID，非 0 即在跑
```

改代码后重启：

```bash
launchctl kickstart -k gui/$(id -u)/com.septemberl.volume-monitor
```

卸载：

```bash
launchctl bootout gui/$(id -u)/com.septemberl.volume-monitor
```

> AI 注意：`bootstrap` 报 `EIO` / `Input/output error` 通常是**不在图形会话**（SSH、某些自动化沙箱）。
> 这时把上面三条命令交给用户在自己的终端 App 里执行，不要反复重试。

### Windows：启动目录

```powershell
# 把 run_win.bat 的快捷方式放进启动目录
$startup = [Environment]::GetFolderPath('Startup')
$WshShell = New-Object -ComObject WScript.Shell
$sc = $WshShell.CreateShortcut("$startup\MarketVolume.lnk")
$sc.TargetPath = "C:\路径\run_win.bat"
$sc.WorkingDirectory = "C:\路径"
$sc.Save()
```

## 6. 配置监视股票与切换间隔

### 方式 A：图形设置页（推荐，给用户自己用）

| 平台 | 入口 |
|---|---|
| macOS | 点状态栏图标 → 菜单底部「设置…（监视股票 / 切换间隔）」 |
| Windows | 悬浮框右键 → 「设置…（监视股票 / 切换间隔）」 |

### 方式 B：AI 直接写 `.settings.json`（在工具目录，不进 git）

```json
{
 "cycle_sec": 10,
 "show_volume": false,
 "stocks": [
  {"code": "sh600000", "name": "浦发银行"},
  {"code": "sz300308", "name": "中际旭创"}
 ]
}
```

字段说明

| 字段 | 含义 | 取值 |
|---|---|---|
| `cycle_sec` | 指数 ↔ 个股 轮播间隔（秒） | 1~120，默认 10 |
| `show_volume` | 个股标题是否追加成交量 | `true` / `false` |
| `stocks[].code` | 代码，**必须带市场前缀** | `sh600000` / `sz000001` / `bj430047`；纯数字也能被程序补全，但写入时请带前缀 |
| `stocks[].name` | 显示名 | 随便写；写错也不影响取数 |

保存后**1 秒内自动生效**（两端都比对文件 mtime），无需重启。
写完可以用这个验证程序读到的内容：

```bash
./venv/bin/python -c "import volume_core as vc, json; print(json.dumps(vc.load_settings(), ensure_ascii=False, indent=1))"
```

代码补全规则（`normalize_code`）：`920/43/83/87` → `bj`；`5/6/9` → `sh`；其余（`0/1/2/3`）→ `sz`。

### 方式 C：让 AI 校验某个股票代码

```bash
./venv/bin/python -c "import volume_core as vc; print(vc.resolve_stock('中际旭创'))"
# ('sz300308', '中际旭创')
```

## 7. 验收清单（AI 逐条确认后再回复用户）

- [ ] `selftest.py` 输出「全部通过」
- [ ] `volume_core.py` 能打印出一行含"全市场 xxx 亿"的摘要（非交易时段允许较短）
- [ ] macOS：`launchctl list | grep volume-monitor` 有 PID；状态栏出现标题
- [ ] Windows：悬浮框出现在屏幕右上角，右键菜单能展开
- [ ] `load_settings()` 读到的 `stocks` 与用户要求一致
- [ ] 告诉用户：改完股票/间隔后**不用重启**；改 `.py` 代码后才需要 `launchctl kickstart -k ...`
- [ ] 告诉用户日志位置：macOS `~/Library/Logs/market-volume.log`；Windows 无日志文件（控制台启动可见）

## 8. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 状态栏图标不见了 | 被菜单项多的 App 挤出屏幕（系统行为，不是崩溃） | 开「简洁模式」，或按住 `Cmd` 把图标拖到最右 |
| 点「设置…」没反应 / 窗口在别的窗口后面 | 状态栏 App 激活策略是 Accessory | 本版本已临时切 Regular + 浮动层级；仍异常看日志 `[设置] 窗口 visible=… level=… policy=…` |
| 标题显示「更新失败 Ns后重试」 | 接口抽风/断网 | 自动降频重试，恢复后自动回到 10 秒；持续失败按第 4 步查网络 |
| 标题只剩「沪创科 休市」 | 周末/节假日 | 正常 |
| 涨跌停数字消失 | 东财接口失败或非交易日 | 正常降级，不影响量能主逻辑 |
| 个股卡片只有名字 + `--` | 个股行情取数失败 | 等下一轮刷新；持续失败查网络/代码拼写 |
| `Cannot find a usable init.tcl` | macOS 某 Python 缺 Tcl 运行时 | 与本工具无关（macOS 不用 tkinter）；**不要为此改代码** |
| `bootstrap` 报 `EIO` | 不在图形会话 | 交给用户在 Terminal.app 里手动执行 |

## 9. 改代码时的红线（AI 必读）

1. **口径不能动**：`volume_core.py` 顶部 `MARKET_CODES = ['sh000001','sz399106','bj899050']` 是"沪+深+北"全市场口径；
   `INDEX_CODES`（创业板指、科创50）**只能用于点位展示**，并入 `MARKET_CODES` 会让总量重复计算。
2. **两端都要改**：标题文案由 `volume_core.py` 的统一函数生成
   （`index_brief` / `limit_segments` / `stock_segments` / `fmt_amount` / `fmt_hand`），
   改完 macOS（`volume_menu.py`）与 Windows（`volume_float_win.py`）都要验证一致。
3. **macOS 不要用 tkinter**：设置窗口是原生 AppKit（`settings_mac.py`）；`settings_win.py` 只服务 Windows。
4. **不要用 nohup/setsid 常驻 macOS**，见第 5 步。
5. **不要把 `.settings.json` 提交进仓库**（`.gitignore` 已忽略），那是用户本地状态。
6. 改完先跑 `./venv/bin/python selftest.py`，再重启进程（`launchctl kickstart -k ...`）验证。

## 10. 代码地图（想加功能时看这里）

| 需求 | 改哪里 |
|---|---|
| 加/减指数、改缩写 | `volume_core.py` 顶部 `INDEX_CODES` / `INDEX_ABBR` / `INDEX_NAMES` |
| 改量能判定阈值（1.03/0.97） | `volume_core.py` `RATIO_UP` / `RATIO_DN` |
| 改刷新频率 | `volume_menu.py` / `volume_float_win.py` 的 `REFRESH_SEC` |
| 改跌停告警阈值/闪烁时长 | `volume_core.py` `FLASH_DT_THRESHOLD` / `FLASH_SEC` |
| 改个股卡片显示字段 | `volume_core.py` `stock_segments()`（**两端同时生效**） |
| 改设置页字段 | `settings_mac.py`（macOS）+ `settings_win.py`（Windows），配置格式同步改 `load_settings/save_settings` |
| 加一种新的数据源 | `volume_core.py` 的 `fetch_*` 函数，务必保持"失败不影响主逻辑"的降级习惯 |

---

*仅供个人学习使用，不构成投资建议。*
