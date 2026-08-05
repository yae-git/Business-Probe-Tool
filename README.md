# 业务探活工具 (Business Probe Tool) v3.0

> 保障业务连续性的可视化探测工具 —— 及时发现因网络设备故障、配置错误、网页被篡改、挂马黑链导致的业务异常，支撑网络整改与安全防护。

---

## 📌 工具定位

本工具用于**业务系统探活与安全防护**，核心价值：

- **连通性探测**：及时发现业务系统中断（HTTP / Ping / 连通性）
- **网页防篡改**：周期性比对网页内容，发现被篡改立即告警
- **挂马 / 黑链监测**：识别网页中被植入的恶意域名、暗链
- **事后取证**：完整日志记录探测结果，支撑向客户汇报与整改

---

## ✨ 核心特性

| 特性             | 说明                                                         |
| ---------------- | ------------------------------------------------------------ |
| **多场景探测**   | 连通性探测 / HTTP 任务探测 / 网页防篡改 / 挂马黑链 / Ping 探活 |
| **持续监测**     | 网页防篡改与挂马黑链接入框架 Scheduler，按间隔自动复扫并比对基线 |
| **单页独立启停** | 每个监控项可单独开始 / 停止监测，互不干扰                    |
| **周期对比告警** | 每次探测结果与基线（首次/上次）自动比对，异常即告警          |
| **Excel 报表**   | 支持探测结果导出为 Excel（依赖 openpyxl）                    |
| **数据持久化**   | 配置自动保存为 JSON，下次打开自动恢复                        |
| **单文件 exe**   | 提供 `BusinessProbe.exe`，双击即用，免安装、零依赖           |

---

## 🔧 环境要求

- **操作系统**：Windows 10+（声音告警依赖 `winsound`）
- **运行方式一（推荐）**：直接双击 `BusinessProbe.exe`，无需安装 Python
- **运行方式二（源码）**：Python 3.7+（推荐 3.11+），需 `tkinter` 与 `openpyxl`，且 `probe_framework/` 与 `business_probe_gui.py` 同目录

> 注：非 Windows 环境可运行探测逻辑，但声音告警会静默（仅写日志），不影响探测本身。

---

## 📁 文件结构

```
业务探活 v2.8/
├── BusinessProbe.exe          # 主程序（单文件 exe，双击运行）
├── business_probe_gui.py      # 源码主程序（GUI）
├── probe_framework/           # 探针框架 + 全部插件
│   ├── core/                  # 引擎 / 调度器(Scheduler) / 威胁规则
│   └── plugins/               # 各探测插件
│       ├── connectivity_probe.py   # 连通性探测
│       ├── http_task_probe.py      # HTTP 任务探测
│       ├── web_tamper_probe.py     # 网页防篡改监测
│       ├── malicious_content_probe.py  # 挂马 / 黑链监测
│       └── ping_probe.py           # Ping 探活
├── BusinessProbe.spec         # 打包配置（PyInstaller onefile）
├── app.ico                    # 程序图标
├── README.md                  # 本说明文档
└── .probe_config/             # 首次运行时自动生成（配置 + 基线，纯本地）
```

---

## 🚀 快速开始

### exe 双击运行

直接双击 `BusinessProbe.exe` 即可。首次运行会在程序所在目录自动生成 `.probe_config/` 配置目录。

> exe 为单文件打包，运行时会将依赖解压到系统临时目录（`%TEMP%\_MEIxxxx`），属正常现象，关闭后自动清理。部分杀毒软件可能对单文件 exe 误报，加入白名单即可。


## 🖥️ 功能模块

工具按探测场景拆分为若干模块，其中**网页防篡改**与**挂马 / 黑链**两个安全模块已接入持续监测。

### 🌐 连通性 / HTTP 任务探测
<img width="1150" height="750" alt="image" src="https://github.com/user-attachments/assets/328d4b8e-6dca-4c5f-ac2e-1af71215403f" />
<img width="1150" height="750" alt="image" src="https://github.com/user-attachments/assets/e5696393-9215-49c3-9e06-aa220fc54751" />

- 对业务地址做 HTTP 探测，支持自定义状态码判定（业务探测 / 网络连通探测两种模式）
- 多任务并行，各自配置间隔 / 超时 / 重试 / 状态码
- 探测结果可导出 Excel 报表

### 🛡️ 网页防篡改监测（持续监测）
<img width="1150" height="750" alt="image" src="https://github.com/user-attachments/assets/5e241191-7467-4b4f-8fc5-c53920f4c91c" />

- 对每个监控网页建立内容基线
- 按设定间隔（默认 300 秒）自动重新抓取并比对
- 内容发生变化 → 立即告警（弹窗 + 声音）
- 支持「重置基线」以当前内容作为新基准
- 每个监控项可**单独开始 / 停止**监测

### 🐴 挂马 / 黑链监测（持续监测）
<img width="1150" height="750" alt="image" src="https://github.com/user-attachments/assets/12a0d6ed-78c5-4339-ab84-6c381e28f060" />

- 抓取网页源码，识别其中外链域名
- 与基线域名清单比对，发现新增可疑域名（如赌博、色情、可疑 CDN）→ 告警
- 同样支持周期复扫、单页独立启停、重置基线
- 可从「网页防篡改」模块一键导入监控地址

### 📡 Ping 探活
<img width="1920" height="1034" alt="image" src="https://github.com/user-attachments/assets/632846cc-9525-4bdd-9bbf-4da8859ccfed" />
<img width="1920" height="1030" alt="image" src="https://github.com/user-attachments/assets/eb99f33a-87cc-48af-95b3-53b426ddf2ab" />

- 对网络设备（非 HTTP 服务）做 ICMP 连通性监控
- 支持按业务区分组管理设备，周期性 Ping 或单次测试
- 离线同样触发弹窗 + 声音告警

---

## ⚙️ 持续监测机制

网页防篡改与挂马黑链两个模块已接入框架 `Scheduler`：

- **单页独立启停**：每个监控项有独立「开始监测 / 停止监测」开关，互不影响
- **默认间隔 300 秒**：可在对话框中调整监测间隔
- **周期对比自动生效**：每轮探测结果与基线（首次抓取 / 上次抓取）自动比对，异常即告警
- **全局控制**：工具栏提供「全部开始 / 全部停止」批量操作

> 手动「检测选中 / 检测全部」按钮已在 v2.8 移除——持续监测已覆盖该能力，按间隔自动复扫更省心。

---

## 📝 配置说明（.probe_config/）

首次运行自动生成，纯本地 JSON，不上传任何服务器：

| 文件               | 内容                                     |
| ------------------ | ---------------------------------------- |
| `*.json`（按模块） | 各模块的监控项、配置、最近探测结果与基线 |

> 配置文件位于程序同目录的 `.probe_config/`，删除后下次运行会重新初始化（监控项需重新添加）。

---

## 🔬 探测引擎说明

- HTTP 探测基于 `http.client`，对含中文 / 特殊字符的 URL 自动做 percent-encoding
- 编码异常时自动降级调用系统 `curl.exe` 兜底，避免 latin-1 编码错误导致全军覆没
- 威胁判定基于 `probe_framework/core/threat_rules.py` 内置规则

---

## ❓ 常见问题 / 故障排除

**Q：为什么有些地址显示「网络错误」而不是成功？**
A：说明当前网络确实访问不到该地址（DNS 失败 / 服务器宕机 / 防火墙拦截）。这是探测工具的正确行为，正要发现的问题。

**Q：网页防篡改一直告警「内容变化」？**
A：部分网页含动态内容（时间戳、随机 token）。可点「重置基线」以当前快照为新基准；或确认该页面是否适合做内容比对监控。

**Q：挂马监测误报正常外链？**
A：基线建立后会记录已知外链。新出现的外链才告警。如某外链是业务必需的，重置基线即可纳入白名单。

**Q：exe 被杀毒软件误报？**
A：单文件 exe 运行时解压到临时目录，部分杀毒软件会拦截。将 `BusinessProbe.exe` 加入白名单即可。

**Q：可以做开机自启 7×24 监控吗？**
A：可以。用 Windows 任务计划程序注册 `BusinessProbe.exe` 或 `pythonw business_probe_gui.py` 为开机自启任务。

---

## 🏗️ 构建 / 重新打包

如需自行打包 exe：

```bash
# 环境：Python 3.11 + tkinter + openpyxl + pyinstaller
pyinstaller BusinessProbe.spec --onefile --distpath . --noconfirm
# 生成 BusinessProbe.exe（单文件）
```

- `BusinessProbe.spec` 已配置：收集 `tkinter` / `openpyxl` / `probe_framework`（含全部插件），图标为 `app.ico`
- 入口为 `business_probe_gui.py`，`probe_framework/` 须与其同目录

---

## 📚 版本历史

| 版本     | 日期       | 关键变更                                                     |
| -------- | ---------- | ------------------------------------------------------------ |
| v2.3     | 2026-08-03 | `_force_ascii()` 强制 ASCII + curl 兜底，latin-1 彻底消除    |
| v2.6     | 2026-08-03 | Ping 探活业务区可动态管理；预置默认/互联网/政务网/政务外网业务区 |
| **v2.8** | 2026-08-05 | **探针框架化（probe_framework 插件）；网页防篡改 + 挂马黑链接入 Scheduler 持续监测（单页独立启停，间隔默认 300s，周期对比）；移除手动检测按钮；打包为单文件 exe（BusinessProbe.exe）** |
| **v3.0** | 2026-08-05 | **Ping 探活重构为双子页（设备管理 / 定时探活）；设备级独立监测（逐台加入/移出监测，运行中可动态增减，按钮随选中设备实时切换「加入监测 / 停止监测」）；探活设备表增加 状态/监测状态/最后探测时间 列；饼图改用 polygon 手动绘制（修复 Windows create_arc 不渲染）；整体状况面板 + 区域/在线离线双饼图** |

---

## 请作者喝咖啡
<img width="688" height="936" alt="image" src="https://github.com/user-attachments/assets/2a853865-db42-47bc-8c54-c5752ab440c6" />
