#!/usr/bin/env python3
# -*- coding: utf-8 -*-


import csv
import io
import json
import logging
import os
import platform
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    print("错误: 需要 tkinter 支持。请使用标准 Python 发行版。")
    sys.exit(1)

try:
    import winsound
    HAVE_WINSOUND = True
except ImportError:
    HAVE_WINSOUND = False

# ---------------------------------------------------------------------------
# 常量 & 配置文件路径
# ---------------------------------------------------------------------------
APP_NAME = "业务探活工具"
CONFIG_DIR = Path(__file__).parent / ".probe_config"
CONFIG_FILE = CONFIG_DIR / "config.json"
URLS_FILE = CONFIG_DIR / "urls.json"
TASKS_FILE = CONFIG_DIR / "tasks.json"
PING_DEVICES_FILE = CONFIG_DIR / "ping_devices.json"
LOG_FILE = CONFIG_DIR / "probe.log"

DEFAULT_INTERVAL = 180       # 默认探测间隔(秒)
DEFAULT_TIMEOUT = 10         # 默认请求超时(秒)
DEFAULT_RETRIES = 1          # 默认重试次数
DEFAULT_RETRY_DELAY = 2      # 默认重试间隔(秒)
DEFAULT_PING_INTERVAL = 60   # 默认 ping 间隔(秒)

DEFAULT_ZONES = ["默认", "互联网", "政务网", "政务外网"]


# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
def setup_logging():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


setup_logging()


# ---------------------------------------------------------------------------
# 数据持久化辅助
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logging.warning("加载 %s 失败: %s", path.name, e)
    return default


def save_json(path: Path, data):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning("保存 %s 失败: %s", path.name, e)


# ---------------------------------------------------------------------------
# 声音告警
# ---------------------------------------------------------------------------
def play_alarm(sound_path=None):
    if not HAVE_WINSOUND:
        return
    try:
        if sound_path and os.path.isfile(sound_path):
            winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            for _ in range(3):
                winsound.Beep(880, 300)
                time.sleep(0.1)
    except Exception as e:
        logging.warning("声音告警失败: %s", e)



def _force_ascii(s: str) -> str:
    """强制将字符串转为纯 ASCII，非 ASCII 字符做 percent-encoding。"""
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return urllib.parse.quote(s, safe="/:@!$&'()*+,;=?")


def _encode_url(url: str) -> str:
    """对 URL 全部非 ASCII 字符做百分号编码。"""
    try:
        parsed = urllib.parse.urlparse(url)
        encoded_path = urllib.parse.quote(parsed.path.encode("utf-8"), safe="/:@!$&'()*+,;=")
        encoded_params = urllib.parse.quote(parsed.params.encode("utf-8"), safe="/:@!$&'()*+,;=")
        encoded_query = urllib.parse.quote(parsed.query.encode("utf-8"), safe="=&")
        encoded_fragment = urllib.parse.quote(parsed.fragment.encode("utf-8"), safe="/:@!$&'()*+,;=")
        if parsed.netloc:
            try:
                encoded_netloc = parsed.netloc.encode("idna").decode("ascii")
            except Exception:
                encoded_netloc = urllib.parse.quote(parsed.netloc, safe=":.[]")
        else:
            encoded_netloc = ""
        result = urllib.parse.urlunparse((
            parsed.scheme, encoded_netloc, encoded_path,
            encoded_params, encoded_query, encoded_fragment,
        ))
        result.encode("ascii")
        return result
    except Exception:
        return urllib.parse.quote(url, safe="/:@!$&'()*+,;=?#[]")


def _probe_with_curl(url: str, timeout: int):
    """使用系统 curl 命令探测（终极兜底，完全绕开 Python 编码问题）。"""
    import subprocess
    try:
        result = subprocess.run(
            ["curl", "-sS", "-o", "NUL", "-w", "%{http_code}",
             "--connect-timeout", str(timeout), "--max-time", str(timeout + 5),
             "-L", "--user-agent", f"{APP_NAME}/2.3", url],
            capture_output=True, timeout=timeout + 10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        code_str = result.stdout.decode("ascii", "ignore").strip()
        if code_str.isdigit():
            status = int(code_str)
            if 200 <= status < 300:
                return True, status, "OK"
            return False, status, f"HTTP {status}"
        return False, None, "无响应"
    except FileNotFoundError:
        return None, None, "curl不可用"
    except subprocess.TimeoutExpired:
        return False, None, "超时"
    except Exception as e:
        return False, None, f"curl异常: {e}"


def probe_url(url: str, timeout: int = DEFAULT_TIMEOUT):
    """
    单次 HTTP 探测。多层防御策略：
      1. http.client (强制 ASCII 清洗所有参数)
      2. 如果仍触发编码错误 → 自动降级到系统 curl
    返回 (成功, 状态码或None, 说明文字)。
    """
    import http.client
    import ssl

    encoded_url = _encode_url(url)

    # ---- 策略1: http.client (带强制ASCII清洗) ----
    try:
        parsed = urllib.parse.urlparse(encoded_url)
        scheme = parsed.scheme.lower()
        host = _force_ascii(parsed.hostname or parsed.netloc.split(":")[0])
        try:
            port = parsed.port or (443 if scheme == "https" else 80)
        except (ValueError, TypeError):
            port = 443 if scheme == "https" else 80
        path = _force_ascii(parsed.path or "/")
        if parsed.query:
            path += "?" + _force_ascii(parsed.query)

        default_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)

            headers = {
                "User-Agent": _force_ascii(f"{APP_NAME}/2.3"),
                "Host": _force_ascii(host),
                "Accept": "*/*",
            }
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            resp.read()
            conn.close()
            return True, status, "OK"
        finally:
            socket.setdefaulttimeout(default_timeout)

    except UnicodeEncodeError:
        pass  # 降级到 curl
    except http.client.HTTPException as e:
        return False, None, f"HTTP异常: {e}"
    except ssl.SSLError as e:
        return False, None, f"SSL错误: {e}"
    except socket.timeout:
        return False, None, "连接超时"
    except ConnectionRefusedError:
        return False, None, "连接被拒绝"
    except OSError as e:
        return False, None, f"网络错误: {e}"

    # ---- 策略2: curl 兜底 ----
    ok, status, msg = _probe_with_curl(url, timeout)
    if ok is not None:
        return ok, status, msg

    return False, None, "探测失败(所有方式均不可用)"


def probe_with_retry(url, timeout, retries, retry_delay):
    """带重试的探测。"""
    last_status, last_msg = None, ""
    for attempt in range(retries + 1):
        ok, status, msg = probe_url(url, timeout)
        if ok:
            return True, status, msg
        last_status, last_msg = status, msg
        if attempt < retries:
            time.sleep(retry_delay)
    return False, last_status, last_msg


def ping_host(host: str, count: int = 3, timeout: int = 5):
    system = platform.system().lower()
    cmd = ["ping"]
    if system == "windows":
        cmd += ["-n", str(count), "-w", str(timeout * 1000)]
    else:
        cmd += ["-c", str(count), "-W", str(timeout)]
    cmd.append(host)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * count + 5)
        output = result.stdout + result.stderr
        if result.returncode == 0:
            avg_ms = _parse_ping_avg(output)
            return True, avg_ms, f"Ping 成功 (平均 {avg_ms}ms)"
        else:
            return False, None, f"Ping 超时/不可达 ({output.strip()[:120]})"
    except subprocess.TimeoutExpired:
        return False, None, f"Ping 命令超时 (> {timeout * count}s)"
    except FileNotFoundError:
        return False, None, "ping 命令未找到"
    except Exception as e:
        return False, None, f"Ping 异常: {e}"


def _parse_ping_avg(output: str) -> int | None:
    import re
    patterns = [
        r"平均\s*=\s*(\d+)ms",
        r"Average\s*=\s*(\d+)ms",
        r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/",
        r"time=(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, output)
        if m:
            try:
                return int(float(m.group(1)))
            except ValueError:
                pass
    return None


# ===========================================================================
# 告警弹窗 (仅 Tab2/Tab3 使用, Tab1 不触发)
# ===========================================================================
class AlarmPopup(tk.Toplevel):
    """强制置顶告警弹窗，带声音和自定义提示。"""

    def __init__(self, parent, title="⚠️ 业务中断告警", message="", sound_path=None,
                 on_acknowledge=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.title(title)
        self.sound_path = sound_path
        self.on_acknowledge = on_acknowledge

        self.geometry("520x340")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._acknowledge)

        self.update_idletasks()
        x = (self.winfo_screenwidth() - 520) // 2
        y = (self.winfo_screenheight() - 340) // 2
        self.geometry(f"+{x}+{y}")

        style_frame = tk.Frame(self, bg="#fff0f0")
        style_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        tk.Label(style_frame, text="🚨", font=("Segoe UI Emoji", 48),
                 bg="#fff0f0").pack(pady=(20, 5))
        tk.Label(style_frame, text=title, font=("Microsoft YaHei UI", 16, "bold"),
                fg="#c0392b", bg="#fff0f0").pack(pady=(0, 10))

        msg_text = tk.Text(style_frame, height=6, wrap=tk.WORD,
                           font=("Microsoft YaHei UI", 11),
                           bg="#ffffff", fg="#333333",
                           relief=tk.FLAT, padx=15, pady=10)
        msg_text.insert("1.0", message)
        msg_text.config(state=tk.DISABLED)
        msg_text.pack(fill=tk.X, padx=25, pady=(0, 15))

        btn_frame = tk.Frame(style_frame, bg="#fff0f0")
        btn_frame.pack(pady=(0, 20))
        tk.Button(btn_frame, text="✓ 我已知晓，立即处理",
                  font=("Microsoft YaHei UI", 11, "bold"),
                  fg="white", bg="#e74c3c", activebackground="#c0392b",
                  relief=tk.FLAT, cursor="hand2",
                  command=self._acknowledge, width=22, height=1).pack()

        play_alarm(sound_path)
        self._repeat_timer = None
        self._schedule_repeat()

    def _schedule_repeat(self):
        if self.winfo_exists():
            play_alarm(self.sound_path)
            self._repeat_timer = self.after(5000, self._schedule_repeat)

    def _acknowledge(self):
        if self._repeat_timer:
            self.after_cancel(self._repeat_timer)
        self.destroy()
        if self.on_acknowledge:
            self.on_acknowledge()


# ===========================================================================
# Tab 1: URL 业务探测 (v2.1 重构版)
# ===========================================================================
class URLProbeTab(ttk.Frame):
    """
    URL 业务探测 — 单次探测工具，用于识别在用/停用业务。

    功能:
    - 按「业务区」分组管理 URL (如: 互联网、政务网、政务外网)
    - 导入 TXT / 手动添加 / 分类在用&停用
    - 单次快速探测 / 全部探测 (无告警弹窗，只记录结果)
    - 一键将探测成功的 URL 导入到「探活任务」Tab 创建 24h 监控任务
    - 导出探测结果 (TXT / CSV) 用于汇报和存档
    """

    def __init__(self, parent, app_ref=None, task_tab_ref=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.app_ref = app_ref
        self.task_tab_ref = task_tab_ref  # 引用 Tab2，用于导入任务
        # 数据结构: {"zones": {"zone_name": {"active": [...], "stopped": [...]}}}
        raw = load_json(URLS_FILE, {})
        self.urls_data = raw.get("zones", {})
        if not self.urls_data:
            self.urls_data["默认"] = {"active": [], "stopped": []}
        self.probe_threads: dict[str, threading.Thread] = {}
        self._build_ui()
        self._refresh_zone_combo()
        self._refresh_tree()

    # ---- UI 构建 ----
    def _build_ui(self):
        # === 第一行: 业务区选择 + 管理 ===
        row_zone = ttk.Frame(self)
        row_zone.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(row_zone, text="当前业务区:", font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        self.zone_var = tk.StringVar(value="")
        self.zone_combo = ttk.Combobox(row_zone, textvariable=self.zone_var,
                                        values=[], state="readonly", width=14)
        self.zone_combo.pack(side=tk.LEFT, padx=(4, 10))
        self.zone_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_tree())

        ttk.Button(row_zone, text="➕ 新建业务区", command=self._create_zone).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row_zone, text="✏ 重命名业务区", command=self._rename_zone).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(row_zone, text="🗑 删除业务区", command=self._delete_zone).pack(side=tk.LEFT, padx=(0, 4))

        # === 第二行: 工具栏 ===
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=(4, 4))

        ttk.Button(toolbar, text="📥 导入 TXT", command=self._import_txt).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="➕ 手动添加", command=self._add_url).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(toolbar, text="▶ 快速探测选中", command=self._quick_probe).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="▶ 全部探测", command=self._probe_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="🔄 重新探测", command=self._reprobe_all).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Label(toolbar, text="超时(s):").pack(side=tk.LEFT)
        self.timeout_var = tk.IntVar(value=DEFAULT_TIMEOUT)
        ttk.Spinbox(toolbar, from_=3, to=60, width=5,
                    textvariable=self.timeout_var).pack(side=tk.LEFT, padx=(2, 12))

        # === 第三行: 导入/导出操作栏 ===
        action_bar = ttk.Frame(self)
        action_bar.pack(fill=tk.X, padx=8, pady=(2, 4))

        ttk.Button(action_bar, text="📤 导入成功URL到探活任务",
                   command=self._import_to_task).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Separator(action_bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
        ttk.Button(action_bar, text="💾 导出全部结果(TXT)", command=lambda: self._export("txt")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(action_bar, text="💾 导出成功URL(CSV)", command=lambda: self._export("csv")).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(action_bar, text="💾 导出失败URL(CSV)", command=lambda: self._export_failed_csv()).pack(side=tk.LEFT, padx=(0, 4))

        # === Treeview ===
        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        columns = ("status", "url", "last_result", "last_time")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=13)

        self.tree.heading("status", text="状态")
        self.tree.heading("url", text="URL 地址")
        self.tree.heading("last_result", text="最近探测结果")
        self.tree.heading("last_time", text="探测时间")

        self.tree.column("status", width=70, anchor=tk.CENTER)
        self.tree.column("url", width=400)
        self.tree.column("last_result", width=200)
        self.tree.column("last_time", width=155)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        # 右键菜单
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label="标记为【在用】", command=lambda: self._set_status("active"))
        self.context_menu.add_command(label="标记为【停用】", command=lambda: self._set_status("stopped"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="移动到其他业务区...", command=self._move_to_zone)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="删除选中", command=self._delete_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="复制 URL", command=self._copy_url)
        self.tree.bind("<Button-3>", self._show_context)

        # === 底部统计 ===
        stat_bar = ttk.Frame(self)
        stat_bar.pack(fill=tk.X, padx=8, pady=(4, 8))
        self.stat_label = ttk.Label(stat_bar, text="", font=("Microsoft YaHei UI", 9))
        self.stat_label.pack(side=tk.LEFT)

    # ---- 业务区管理 ----
    def _get_current_zone(self) -> str:
        z = self.zone_var.get()
        return z if z else (list(self.urls_data.keys())[0] if self.urls_data else "默认")

    def _get_zone_data(self, zone=None) -> dict:
        z = zone or self._get_current_zone()
        return self.urls_data.get(z, {"active": [], "stopped": []})

    def _ensure_zone(self, name):
        if name not in self.urls_data:
            self.urls_data[name] = {"active": [], "stopped": []}

    def _refresh_zone_combo(self):
        zones = list(self.urls_data.keys())
        self.zone_combo["values"] = zones
        current = self.zone_var.get()
        if not current or current not in self.urls_data:
            self.zone_var.set(zones[0] if zones else "")

    def _create_zone(self):
        dlg = tk.Toplevel(self)
        dlg.title("新建业务区")
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("360x140")
        dlg.resizable(False, False)

        ttk.Label(dlg, text="业务区名称:", font=("Microsoft YaHei UI", 10)).pack(pady=(18, 4))
        entry = ttk.Entry(dlg, font=("Microsoft YaHei UI", 10), width=32)
        entry.pack(padx=20, pady=4)
        entry.focus_set()

        bf = ttk.Frame(dlg)
        bf.pack(pady=(14, 12))

        def do_create():
            name = entry.get().strip()
            if not name:
                messagebox.showwarning("提示", "名称不能为空", parent=dlg)
                return
            if name in self.urls_data:
                messagebox.showwarning("提示", f"业务区「{name}」已存在", parent=dlg)
                return
            self._ensure_zone(name)
            self._save()
            self._refresh_zone_combo()
            self.zone_var.set(name)
            self._refresh_tree()
            dlg.destroy()

        ttk.Button(bf, text="创建", command=do_create).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=5)
        entry.bind("<Return>", lambda e: do_create())

    def _rename_zone(self):
        zone = self._get_current_zone()
        if zone == "默认":
            messagebox.showinfo("提示", "不能重命名「默认」业务区")
            return
        dlg = tk.Toplevel(self)
        dlg.title("重命名业务区")
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("360x130")

        ttk.Label(dlg, text=f"将「{zone}」重命名为:").pack(pady=(16, 4))
        entry = ttk.Entry(dlg, font=("Microsoft YaHei UI", 10), width=32)
        entry.insert(0, zone)
        entry.pack(padx=20, pady=4)
        entry.select_range(0, tk.END)
        entry.focus_set()

        bf = ttk.Frame(dlg)
        bf.pack(pady=(12, 12))

        def do_rename():
            new_name = entry.get().strip()
            if not new_name or new_name == zone:
                dlg.destroy()
                return
            if new_name in self.urls_data:
                messagebox.showwarning("提示", f"业务区「{new_name}」已存在", parent=dlg)
                return
            self.urls_data[new_name] = self.urls_data.pop(zone)
            self._save()
            self._refresh_zone_combo()
            self.zone_var.set(new_name)
            self._refresh_tree()
            dlg.destroy()

        ttk.Button(bf, text="确定", command=do_rename).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=5)

    def _delete_zone(self):
        zone = self._get_current_zone()
        if zone == "默认":
            messagebox.showinfo("提示", "不能删除「默认」业务区")
            return
        zd = self._get_zone_data(zone)
        total = len(zd.get("active", [])) + len(zd.get("stopped", []))
        if total > 0:
            if not messagebox.askyesno("确认删除",
                                       f"业务区「{zone}」下有 {total} 个 URL\n确定删除？(URL 将一并删除)"):
                return
        del self.urls_data[zone]
        self._save()
        self._refresh_zone_combo()
        self._refresh_tree()

    def _move_to_zone(self):
        urls = self._get_selected_urls()
        if not urls:
            return
        zones = [z for z in self.urls_data.keys() if z != self._get_current_zone()]
        if not zones:
            messagebox.showinfo("提示", "没有其他业务区可选")
            return

        dlg = tk.Toplevel(self)
        dlg.title="移动到业务区"
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("320x150")

        ttk.Label(dlg, text=f"将选中的 {len(urls)} 个 URL 移动到:").pack(pady=(14, 6))
        target_var = tk.StringVar(value=zones[0])
        combo = ttk.Combobox(dlg, textvariable=target_var, values=zones, state="readonly", width=26)
        combo.pack(padx=16, pady=4)

        bf = ttk.Frame(dlg)
        bf.pack(pady=(12, 12))

        def do_move():
            target = target_var.get()
            src_zone = self._get_current_zone()
            self._ensure_zone(target)
            for u in urls:
                # 从源区移除
                for cat in ("active", "stopped"):
                    self.urls_data[src_zone][cat] = [
                        item for item in self.urls_data[src_zone].get(cat, [])
                        if item.get("url") != u
                    ]
                # 查找原数据并移到目标区
                found = None
                for cat in ("active", "stopped"):
                    for item in self.urls_data[src_zone].get(cat, []):
                        if item.get("url") == u:
                            found = item
                            break
                    if found:
                        break
                if not found:
                    found = {"url": u, "last_result": "-", "last_time": "-"}
                self.urls_data[target]["active"].append(found)
            self._save()
            self._refresh_tree()
            dlg.destroy()

        ttk.Button(bf, text="移动", command=do_move).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=5)

    # ---- 刷新显示 ----
    def _refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        zone = self._get_current_zone()
        zd = self._get_zone_data(zone)
        for url_info in zd.get("active", []):
            self._insert_row(url_info, "✅ 在用")
        for url_info in zd.get("stopped", []):
            self._insert_row(url_info, "⏸ 停用")
        self._update_stat()

    def _insert_row(self, url_info, status_tag):
        url = url_info.get("url", "")
        last = url_info.get("last_result", "-")
        t = url_info.get("last_time", "-")
        self.tree.insert("", tk.END, values=(status_tag, url, last, t), tags=(url,))

    def _update_stat(self):
        zone = self._get_current_zone()
        zd = self._get_zone_data(zone)
        a = len(zd.get("active", []))
        s = len(zd.get("stopped", []))
        ok_count = sum(1 for item in zd.get("active", []) if item.get("last_result", "").startswith("✓"))
        fail_count = sum(1 for item in zd.get("active", []) if item.get("last_result", "").startswith("✗"))
        self.stat_label.config(
            text=f"📁 业务区: {zone}  |  ✅在用: {a}  ⏸停用: {s}  "
                 f"|  最近探测: ✓成功 {ok_count}  ✗失败 {fail_count}"
        )

    def _save(self):
        save_json(URLS_FILE, {"zones": self.urls_data})

    # ---- 导入 / 添加 ----
    def _import_txt(self):
        path = filedialog.askopenfilename(
            title="选择 URL 清单文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            urls = []
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        urls.append(line)
            zone = self._get_current_zone()
            self._ensure_zone(zone)
            added = 0
            for u in urls:
                existing = any(item["url"] == u for item in
                               self._get_zone_data(zone).get("active", [])
                               + self._get_zone_data(zone).get("stopped", []))
                if not existing:
                    self.urls_data[zone].setdefault("active", []).append({
                        "url": u, "last_result": "-", "last_time": "-"
                    })
                    added += 1
            self._save()
            self._refresh_tree()
            messagebox.showinfo("导入完成",
                                f"共读取 {len(urls)} 个地址\n新增 {added} 个 → 业务区「{zone}」\n重复跳过 {len(urls) - added} 个")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _add_url(self):
        dlg = tk.Toplevel(self)
        dlg.title="添加 URL"
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("500x170")
        dlg.resizable(False, False)

        f1 = ttk.Frame(dlg)
        f1.pack(fill=tk.X, padx=18, pady=(16, 4))
        ttk.Label(f1, text="URL 地址:", font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)
        entry = ttk.Entry(fg1, font=("Consolas", 9), width=50)
        entry.pack(side=tk.LEFT, padx=8, fill=tk.X, expand=True)
        entry.focus_set()

        f2 = ttk.Frame(dlg)
        f2.pack(fill=tk.X, padx=18, pady=4)
        ttk.Label(f2, text="状态:").pack(side=tk.LEFT)
        st_var = tk.StringVar(value="在用")
        ttk.Radiobutton(f2, text="在用", variable=st_var, value="在用").pack(side=tk.LEFT, padx=(4, 10))
        ttk.Radiobutton(f2, text="停用", variable=st_var, value="停用").pack(side=tk.LEFT)

        bf = ttk.Frame(dlg)
        bf.pack(pady=(12, 12))

        def do_add():
            url = entry.get().strip()
            if not url:
                messagebox.showwarning("提示", "URL 不能为空", parent=dlg)
                return
            zone = self._get_current_zone()
            self._ensure_zone(zone)
            existing = any(item["url"] == url for item in
                           self._get_zone_data(zone).get("active", [])
                           + self._get_zone_data(zone).get("stopped", []))
            if existing:
                messagebox.showwarning("提示", "该 URL 已存在于当前业务区", parent=dlg)
                return
            cat = "active" if st_var.get() == "在用" else "stopped"
            self.urls_data[zone].setdefault(cat, []).append({
                "url": url, "last_result": "-", "last_time": "-"
            })
            self._save()
            self._refresh_tree()
            dlg.destroy()

        ttk.Button(bf, text="添加", command=do_add).pack(side=tk.LEFT, padx=5)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=5)
        entry.bind("<Return>", lambda e: do_add())

    # ---- 右键操作 ----
    def _show_context(self, event):
        sel = self.tree.selection()
        if sel:
            self.context_menu.post(event.x_root, event.y_root)

    def _get_selected_urls(self):
        return [self.tree.item(i)["values"][1] for i in self.tree.selection()]

    def _set_status(self, new_status):
        urls = self._get_selected_urls()
        zone = self._get_current_zone()
        for u in urls:
            for cat in ("active", "stopped"):
                self.urls_data[zone][cat] = [
                    item for item in self.urls_data[zone].get(cat, [])
                    if item.get("url") != u
                ]
            info = next((item for item in sum([self.urls_data[zone].get(c, []) for c in ("active", "stopped")], [])
                         if item.get("url") == u), None)
            if not info:
                info = {"url": u, "last_result": "-", "last_time": "-"}
            self.urls_data[zone].setdefault(new_status, []).append(info)
        self._save()
        self._refresh_tree()

    def _delete_selected(self):
        urls = self._get_selected_urls()
        if not urls:
            return
        if messagebox.askyesno("确认删除", f"确定删除选中的 {len(urls)} 个 URL？"):
            zone = self._get_current_zone()
            for u in urls:
                for cat in ("active", "stopped"):
                    self.urls_data[zone][cat] = [
                        item for item in self.urls_data[zone].get(cat, [])
                        if item.get("url") != u
                    ]
            self._save()
            self._refresh_tree()

    def _copy_url(self):
        urls = self._get_selected_urls()
        if urls:
            self.clipboard_clear()
            self.clipboard_append(urls[0])

    # ---- 探测 (无告警弹窗) ----
    def _quick_probe(self):
        urls = self._get_selected_urls()
        if not urls:
            messagebox.showinfo("提示", "请先选中要探测的 URL")
            return
        timeout = self.timeout_var.get()
        for u in urls:
            t = threading.Thread(target=self._do_probe_one, args=(u, timeout), daemon=True)
            t.start()

    def _probe_all(self):
        zone = self._get_current_zone()
        all_urls = [item["url"] for item in self.urls_data.get(zone, {}).get("active", [])]
        if not all_urls:
            messagebox.showinfo("提示", "当前业务区没有「在用」的 URL 可探测")
            return
        timeout = self.timeout_var.get()
        for u in all_urls:
            t = threading.Thread(target=self._do_probe_one, args=(u, timeout), daemon=True)
            t.start()

    def _reprobe_all(self):
        """清空当前探测结果，然后重新执行全部探测。"""
        zone = self._get_current_zone()
        # 1. 清空数据中的探测结果
        for cat in ("active", "stopped"):
            for item in self.urls_data.get(zone, {}).get(cat, []):
                item["last_result"] = ""
                item["last_time"] = ""
        self._save()
        # 2. 清空表格中的结果显示
        for item_id in self.tree.get_children():
            vals = self.tree.item(item_id)["values"]
            if len(vals) >= 4:
                self.tree.item(item_id, values=(vals[0], vals[1], "", ""))
        self._update_stat()
        # 3. 执行重新探测
        self._probe_all()

    def _do_probe_one(self, url, timeout):
        ok, status, msg = probe_with_retry(url, timeout, DEFAULT_RETRIES, DEFAULT_RETRY_DELAY)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result_str = f"{'✓' if ok else '✗'} HTTP {status} {msg}" if status is not None else f"{'✓' if ok else '✗'} {msg}"

        # 更新数据
        zone = self._get_current_zone()
        for cat in ("active", "stopped"):
            for item in self.urls_data.get(zone, {}).get(cat, []):
                if item["url"] == url:
                    item["last_result"] = result_str
                    item["last_time"] = now
        self._save()

        # 更新界面 (线程安全)
        self.after(0, lambda: self._update_row(url, result_str, now))
        # 注意: Tab1 不触发告警弹窗！这是单次探测工具。

    def _update_row(self, url, result, ts):
        for item_id in self.tree.get_children():
            vals = self.tree.item(item_id)["values"]
            if len(vals) >= 2 and vals[1] == url:
                self.tree.item(item_id, values=(vals[0], url, result, ts))
                break
        self._update_stat()

    # ---- 导入到探活任务 ----
    def _import_to_task(self):
        """收集当前业务区所有探测成功的 URL，发送到 Tab2 创建探活任务。"""
        success_urls = []
        zone = self._get_current_zone()
        for item in self.urls_data.get(zone, {}).get("active", []):
            if item.get("last_result", "").startswith("✓"):
                success_urls.append(item["url"])

        if not success_urls:
            messagebox.showinfo("提示", "当前业务区没有探测成功的 URL。\n请先执行「全部探测」，然后再导入。")
            return

        # 弹窗让用户配置任务
        dlg = tk.Toplevel(self)
        dlg.title="导入到探活任务 (24h 实时监控)"
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("600x480")

        ttk.Label(dlg, text=f"从业务区「{zone}」导入 {len(success_urls)} 个成功 URL 到探活任务",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(14, 8))

        form = ttk.Frame(dlg)
        form.pack(fill=tk.X, padx=18)

        r1 = ttk.Frame(form)
        r1.pack(fill=tk.X, pady=4)
        ttk.Label(r1, text="任务名称:").pack(side=tk.LEFT)
        name_var = tk.StringVar(value=f"{zone}_业务探活_{datetime.now().strftime('%m%d%H%M')}")
        ttk.Entry(r1, textvariable=name_var, width=40).pack(side=tk.LEFT, padx=8)

        r2 = ttk.Frame(form)
        r2.pack(fill=tk.X, pady=4)
        ttk.Label(r2, text="探测模式:").pack(side=tk.LEFT)
        mode_var = tk.StringVar(value="业务探测")
        ttk.Combobox(r2, textvariable=mode_var, values=["业务探测", "网络连通探测"],
                     state="readonly", width=14).pack(side=tk.LEFT, padx=8)

        r3 = ttk.Frame(form)
        r3.pack(fill=tk.X, pady=4)
        ttk.Label(r3, text="间隔(秒):").pack(side=tk.LEFT)
        iv = tk.IntVar(value=DEFAULT_INTERVAL)
        ttk.Spinbox(r3, from_=30, to=3600, width=7, textvariable=iv).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(r3, text="超时(s):").pack(side=tk.LEFT)
        tv = tk.IntVar(value=DEFAULT_TIMEOUT)
        ttk.Spinbox(r3, from_=3, to=60, width=5, textvariable=tv).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(r3, text="重试:").pack(side=tk.LEFT)
        rt = tk.IntVar(value=DEFAULT_RETRIES)
        ttk.Spinbox(r3, from_=0, to=5, width=4, textvariable=rt).pack(side=tk.LEFT, padx=4)

        r4 = ttk.Frame(form)
        r4.pack(fill=tk.X, pady=4)
        ttk.Label(r4, text="正常状态码:").pack(side=tk.LEFT)
        sc_var = tk.StringVar(value="200")
        ttk.Entry(r4, textvariable=sc_var, width=26).pack(side=tk.LEFT, padx=8)

        # 预览 URL 列表
        ttk.Label(dlg, text=f"待导入 URL ({len(success_urls)} 个):",
                  font=("Microsoft YaHei UI", 9)).pack(anchor=tk.W, padx=18, pady=(8, 2))
        preview = scrolledtext.ScrolledText(dlg, height=7, font=("Consolas", 8), wrap=tk.NONE)
        preview.pack(fill=tk.BOTH, expand=True, padx=18, pady=2)
        preview.insert("1.0", "\n".join(success_urls))
        preview.config(state=tk.DISABLED)

        bf = ttk.Frame(dlg)
        bf.pack(pady=(10, 14))

        def do_import():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("提示", "任务名称不能为空", parent=dlg)
                return
            try:
                codes = [int(c.strip()) for c in sc_var.get().split(",") if c.strip()]
            except ValueError:
                messagebox.showwarning("提示", "状态码格式错误", parent=dlg)
                return

            task_data = {
                "name": name,
                "probe_mode": mode_var.get(),
                "interval": iv.get(),
                "timeout": tv.get(),
                "retries": rt.get(),
                "status_codes": codes,
                "urls": success_urls[:],
                "_log": [],
            }

            # 写入 tasks.json
            tasks = load_json(TASKS_FILE, [])
            tasks.append(task_data)
            save_json(TASKS_FILE, tasks)

            # 如果 Tab2 存在引用，刷新其列表
            if self.task_tab_ref:
                self.task_tab_ref.tasks = tasks
                self.task_tab_ref._refresh_task_list()

            messagebox.showinfo("导入成功",
                                f"已创建探活任务「{name}」\n"
                                f"共 {len(success_urls)} 个 URL\n"
                                f"请切换到「探活任务」Tab 启动监控！", parent=dlg)
            dlg.destroy()

        ttk.Button(bf, text="✅ 创建探活任务", command=do_import).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    # ---- 导出功能 ----
    def _collect_all_results(self):
        """收集所有业务区的探测结果。"""
        results = []
        for zone_name, zd in self.urls_data.items():
            for item in zd.get("active", []) + zd.get("stopped", []):
                results.append({
                    "zone": zone_name,
                    "status": "在用" if item in zd.get("active", []) else "停用",
                    "url": item.get("url", ""),
                    "result": item.get("last_result", "-"),
                    "time": item.get("last_time", "-"),
                })
        return results

    def _export(self, fmt="txt"):
        results = self._collect_all_results()
        if not results:
            messagebox.showinfo("提示", "没有数据可导出")
            return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt == "txt":
            default_name = f"业务探测报告_{timestamp}.txt"
            path = filedialog.asksaveasfilename(title="导出 TXT 报告",
                                                defaultextension=".txt",
                                                initialfile=default_name,
                                                filetypes=[("文本文件", "*.txt")])
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"{'='*70}\n")
                    f.write(f"  业务探测报告\n")
                    f.write(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                    f.write(f"{'='*70}\n\n")
                    # 按业务区分组
                    by_zone = {}
                    for r in results:
                        by_zone.setdefault(r["zone"], []).append(r)
                    for zn, items in by_zone.items():
                        f.write(f"\n【{zn}】({len(items)} 个)\n")
                        f.write("-" * 60 + "\n")
                        for it in items:
                            f.write(f"  [{it['status']}] {it['url']}\n")
                            f.write(f"       结果: {it['result']}  时间: {it['time']}\n")
                    ok_total = sum(1 for r in results if r["result"].startswith("✓"))
                    fail_total = sum(1 for r in results if r["result"].startswith("✗"))
                    f.write(f"\n{'='*70}\n")
                    f.write(f"汇总: 总计 {len(results)} 个  |  成功 {ok_total}  |  失败 {fail_total}\n")
                messagebox.showinfo("导出成功", f"已导出到:\n{path}")
            except Exception as e:
                messagebox.showerror("导出失败", str(e))

        elif fmt == "csv":
            default_name = f"业务探测报告_{timestamp}.csv"
            path = filedialog.asksaveasfilename(title="导出 CSV 报告",
                                                defaultextension=".csv",
                                                initialfile=default_name,
                                                filetypes=[("CSV 文件", "*.csv")])
            if not path:
                return
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    writer = csv.DictWriter(f, fieldnames=["业务区", "状态", "URL地址", "探测结果", "探测时间"])
                    writer.writeheader()
                    for r in results:
                        writer.writerow({
                            "业务区": r["zone"], "状态": r["status"],
                            "URL地址": r["url"], "探测结果": r["result"], "探测时间": r["time"],
                        })
                messagebox.showinfo("导出成功", f"已导出到:\n{path}")
            except Exception as e:
                messagebox.showerror("导出失败", str(e))

    def _export_failed_csv(self):
        """单独导出失败的 URL。"""
        failed = [r for r in self._collect_all_results() if r["result"].startswith("✗")]
        if not failed:
            messagebox.showinfo("提示", "没有探测失败的记录可导出")
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"业务探测_失败列表_{timestamp}.csv"
        path = filedialog.asksaveasfilename(title="导出失败 URL",
                                            defaultextension=".csv",
                                            initialfile=default_name,
                                            filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["业务区", "URL地址", "探测结果", "探测时间"])
                writer.writeheader()
                for r in failed:
                    writer.writerow({"业务区": r["zone"], "URL地址": r["url"],
                                     "探测结果": r["result"], "探测时间": r["time"]})
            messagebox.showinfo("导出成功", f"已导出 {len(failed)} 条失败记录到:\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))


# ===========================================================================
# Tab 2: 探活任务管理
# ===========================================================================
class TaskProbeTab(ttk.Frame):
    """多任务探测管理：创建多个命名任务，每个任务有独立 URL 列表、间隔、状态码判定、重试策略。"""

    def __init__(self, parent, app_ref=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.app_ref = app_ref
        self.tasks = load_json(TASKS_FILE, [])
        self.task_runners: dict[str, dict] = {}
        self._build_ui()
        self._refresh_task_list()

    def _build_ui(self):
        left_pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        left_pane.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(8, 4), pady=8)

        task_toolbar = ttk.Frame(left_pane)
        task_toolbar.pack(fill=tk.X, pady=(0, 4))

        ttk.Button(task_toolbar, text="➕ 新建任务", command=self._create_task).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(task_toolbar, text="✏ 编辑任务", command=self._edit_task).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(task_toolbar, text="🗑 删除任务", command=self._delete_task).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Separator(task_toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        self.btn_start = ttk.Button(task_toolbar, text="▶ 启动选中", command=self._start_selected)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_stop = ttk.Button(task_toolbar, text="⏹ 停止选中", command=self._stop_selected)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(task_toolbar, text="⏹ 全部停止", command=self._stop_all).pack(side=tk.LEFT)

        cols = ("name", "mode", "interval", "urls_count", "status_codes", "retries", "running")
        self.task_tree = ttk.Treeview(left_pane, columns=cols, show="headings", height=10)

        headings = {
            "name": "任务名称", "mode": "模式", "interval": "间隔(s)",
            "urls_count": "URL数", "status_codes": "状态码", "retries": "重试", "running": "运行状态"
        }
        widths = {"name": 150, "mode": 90, "interval": 65, "urls_count": 50,
                  "status_codes": 130, "retries": 45, "running": 85}
        for c in cols:
            self.task_tree.heading(c, text=headings[c])
            self.task_tree.column(c, width=widths[c], anchor=tk.CENTER if c != "name" else tk.W)

        self.task_tree.pack(fill=tk.BOTH, expand=True)
        self.task_tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        self.task_tree.bind("<Double-1>", lambda e: self._edit_task())

        right_pane = ttk.PanedWindow(self, orient=tk.VERTICAL)
        right_pane.pack(fill=tk.BOTH, expand=True, side=tk.RIGHT, padx=(4, 8), pady=8)

        detail_lab = ttk.LabelFrame(right_pane, text="任务详情 / 实时日志")
        detail_lab.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(detail_lab, height=18, font=("Consolas", 9),
                                                  state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        bottom = ttk.Frame(right_pane)
        bottom.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(bottom, text="提示: 双击任务可编辑 | 绿色=运行中 | 红色=已停止 | 失败会弹窗+声音告警").pack(side=tk.LEFT)

    def _refresh_task_list(self):
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        for task in self.tasks:
            running = "🟢 运行中" if task.get("name") in self.task_runners else "🔴 已停止"
            sc = task.get("status_codes", [200])
            sc_str = ",".join(str(c) for c in sc[:4])
            if len(sc) > 4:
                sc_str += f"...({len(sc)}个)"
            self.task_tree.insert("", tk.END, values=(
                task.get("name", ""), task.get("probe_mode", "业务探测"),
                task.get("interval", DEFAULT_INTERVAL), len(task.get("urls", [])),
                sc_str, task.get("retries", DEFAULT_RETRIES), running
            ), tags=(task.get("name", ""),))
        self._on_select()

    def _on_select(self):
        sel = self.task_tree.selection()
        if sel:
            name = self.task_tree.item(sel[0])["values"][0]
            self._show_log(name)

    def _show_log(self, task_name):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        task = next((t for t in self.tasks if t.get("name") == task_name), None)
        if task:
            log_lines = task.get("_log", [])
            self.log_text.insert("1.0", "\n".join(log_lines[-200:]) if log_lines else "(暂无日志)")
        self.log_text.config(state=tk.DISABLED)

    def _append_log(self, task_name, line):
        task = next((t for t in self.tasks if t.get("name") == task_name), None)
        if task:
            task.setdefault("_log", []).append(line)
            if len(task["_log"]) > 500:
                task["_log"] = task["_log"][-400:]
        sel = self.task_tree.selection()
        if sel and self.task_tree.item(sel[0])["values"][0] == task_name:
            self.after(0, lambda: self._show_log(task_name))

    # ---- CRUD ----
    def _create_task(self):
        self._task_editor("新建探活任务")

    def _edit_task(self):
        sel = self.task_tree.selection()
        if not sel:
            return
        name = self.task_tree.item(sel[0])["values"][0]
        task = next((t for t in self.tasks if t.get("name") == name), None)
        if task:
            self._task_editor("编辑探活任务", task)

    def _delete_task(self):
        sel = self.task_tree.selection()
        if not sel:
            return
        name = self.task_tree.item(sel[0])["values"][0]
        if messagebox.askyesno("确认删除", f"确定删除任务「{name}」？"):
            self._stop_task(name)
            self.tasks = [t for t in self.tasks if t.get("name") != name]
            save_json(TASKS_FILE, self.tasks)
            self._refresh_task_list()

    def _task_editor(self, title, existing=None):
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("640x560")
        dlg.resizable(True, True)

        f1 = ttk.Frame(dlg)
        f1.pack(fill=tk.X, padx=15, pady=(12, 4))
        ttk.Label(f1, text="任务名称:").pack(side=tk.LEFT)
        name_var = tk.StringVar(value=existing.get("name", "") if existing else "")
        ttk.Entry(f1, textvariable=name_var, width=35).pack(side=tk.LEFT, padx=8)

        f2 = ttk.Frame(dlg)
        f2.pack(fill=tk.X, padx=15, pady=4)
        ttk.Label(f2, text="探测模式:").pack(side=tk.LEFT)
        mode_var = tk.StringVar(value=existing.get("probe_mode", "业务探测") if existing else "业务探测")
        ttk.Combobox(f2, textvariable=mode_var, values=["业务探测", "网络连通探测"],
                     state="readonly", width=16).pack(side=tk.LEFT, padx=8)
        ttk.Label(f2, text="(业务探测要求2xx; 网络连通探测只要能连上即可)",
                  font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT, padx=4)

        f3 = ttk.Frame(dlg)
        f3.pack(fill=tk.X, padx=15, pady=4)
        ttk.Label(f3, text="间隔(秒):").pack(side=tk.LEFT)
        intv_var = tk.IntVar(value=existing.get("interval", DEFAULT_INTERVAL) if existing else DEFAULT_INTERVAL)
        ttk.Spinbox(f3, from_=10, to=3600, width=7, textvariable=intv_var).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(f3, text="超时(秒):").pack(side=tk.LEFT)
        to_var = tk.IntVar(value=existing.get("timeout", DEFAULT_TIMEOUT) if existing else DEFAULT_TIMEOUT)
        ttk.Spinbox(f3, from_=3, to=60, width=5, textvariable=to_var).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(f3, text="重试次数:").pack(side=tk.LEFT)
        rt_var = tk.IntVar(value=existing.get("retries", DEFAULT_RETRIES) if existing else DEFAULT_RETRIES)
        ttk.Spinbox(f3, from_=0, to=5, width=5, textvariable=rt_var).pack(side=tk.LEFT, padx=4)

        f4 = ttk.Frame(dlg)
        f4.pack(fill=tk.X, padx=15, pady=4)
        ttk.Label(f4, text="正常状态码:").pack(side=tk.LEFT)
        sc_var = tk.StringVar(value=",".join(str(c) for c in (existing.get("status_codes", [200]) if existing else [200])))
        ttk.Entry(f4, textvariable=sc_var, width=30).pack(side=tk.LEFT, padx=8)
        ttk.Label(f4, text="(逗号分隔, 如 200,201,202)", font=("Microsoft YaHei UI", 8)).pack(side=tk.LEFT)

        ttk.Label(dlg, text="探测 URL 列表 (每行一个):", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor=tk.W, padx=15, pady=(10, 2))
        url_text = scrolledtext.ScrolledText(dlg, height=10, font=("Consolas", 9), wrap=tk.NONE)
        url_text.pack(fill=tk.BOTH, expand=True, padx=15, pady=4)
        if existing:
            url_text.insert("1.0", "\n".join(existing.get("urls", [])))

        bf = ttk.Frame(dlg)
        bf.pack(pady=(8, 12))

        def do_save():
            name = name_var.get().strip()
            if not name:
                messagebox.showwarning("提示", "任务名称不能为空", parent=dlg)
                return
            try:
                codes = [int(c.strip()) for c in sc_var.get().split(",") if c.strip()]
            except ValueError:
                messagebox.showwarning("提示", "状态码格式错误，请用逗号分隔数字", parent=dlg)
                return
            urls_raw = url_text.get("1.0", tk.END).strip()
            urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
            if not urls:
                messagebox.showwarning("提示", "至少需要一个 URL", parent=dlg)
                return

            task_data = {
                "name": name, "probe_mode": mode_var.get(),
                "interval": intv_var.get(), "timeout": to_var.get(),
                "retries": rt_var.get(), "status_codes": codes,
                "urls": urls, "_log": existing.get("_log", []) if existing else [],
            }
            if existing:
                idx = next((i for i, t in enumerate(self.tasks) if t.get("name") == existing["name"]), None)
                if idx is not None:
                    self.tasks[idx] = task_data
            else:
                self.tasks.append(task_data)
            save_json(TASKS_FILE, self.tasks)
            self._refresh_task_list()
            dlg.destroy()

        ttk.Button(bf, text="💾 保存", command=do_save).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    # ---- 启动 / 停止 ----
    def _start_selected(self):
        sel = self.task_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个任务")
            return
        name = self.task_tree.item(sel[0])["values"][0]
        self._start_task(name)

    def _start_task(self, task_name):
        task = next((t for t in self.tasks if t.get("name") == task_name), None)
        if not task:
            return
        if task_name in self.task_runners:
            messagebox.showinfo("提示", f"任务「{task_name}」已在运行中")
            return
        stop_flag = threading.Event()
        runner = {
            "thread": threading.Thread(target=self._run_task_loop, args=(task, stop_flag), daemon=True),
            "stop_flag": stop_flag,
        }
        self.task_runners[task_name] = runner
        runner["thread"].start()
        self._refresh_task_list()
        self._append_log(task_name, f"[{datetime.now().strftime('%H:%M:%S')}] ▶ 任务启动")

    def _stop_selected(self):
        sel = self.task_tree.selection()
        if not sel:
            return
        name = self.task_tree.item(sel[0])["values"][0]
        self._stop_task(name)

    def _stop_task(self, task_name):
        if task_name in self.task_runners:
            self.task_runners[task_name]["stop_flag"].set()
            del self.task_runners[task_name]
            self._refresh_task_list()
            self._append_log(task_name, f"[{datetime.now().strftime('%H:%M:%S')}] ⏹ 任务停止")

    def _stop_all(self):
        for name in list(self.task_runners.keys()):
            self._stop_task(name)

    # ---- 任务运行循环 (含告警) ----
    def _run_task_loop(self, task, stop_flag):
        interval = task.get("interval", DEFAULT_INTERVAL)
        timeout = task.get("timeout", DEFAULT_TIMEOUT)
        retries = task.get("retries", DEFAULT_RETRIES)
        valid_codes = set(task.get("status_codes", [200]))
        urls = task.get("urls", [])
        name = task.get("name", "?")
        round_no = 0

        while not stop_flag.is_set():
            round_no += 1
            if not urls:
                self._append_log(name, f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ URL 列表为空，跳过")
            else:
                url = random.choice(urls)
                self._append_log(name, f"[{datetime.now().strftime('%H:%M:%S')}] 第{round_no}轮 → {url}")

                ok, status, msg = probe_with_retry(url, timeout, retries, DEFAULT_RETRY_DELAY)

                if ok and status is not None:
                    if status in valid_codes:
                        result = "✓ 正常"
                    else:
                        ok = False
                        result = f"✗ 状态码 {status} 不在允许范围 {valid_codes}"
                elif ok:
                    result = "✓ 正常"
                else:
                    result = f"✗ {msg}"

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                log_line = f"[{datetime.now().strftime('%H:%M:%S')}] {result}"
                self._append_log(name, log_line)

                # 失败触发告警 (只有 Tab2 才有告警!)
                if not ok and self.app_ref:
                    cfg = self.app_ref.get_alarm_config()
                    alarm_msg = (
                        f"任务: {name}\n"
                        f"时间: {now}\n"
                        f"URL: {url}\n"
                        f"结果: {result}\n"
                        f"\n请检查网络及业务状态！"
                    )
                    custom = cfg.get("popup_message", "").strip()
                    if custom:
                        alarm_msg = custom.replace("{task}", name).replace("{url}", url).replace(
                            "{result}", result).replace("{time}", now)
                    self.after(0, lambda m=alarm_msg: AlarmPopup(
                        self.winfo_toplevel(),
                        title=cfg.get("popup_title", "⚠️ 业务中断告警"),
                        message=m,
                        sound_path=cfg.get("sound_file"),
                    ))

            for _ in range(interval):
                if stop_flag.is_set():
                    break
                time.sleep(1)

        self._append_log(name, f"[{datetime.now().strftime('%H:%M:%S')}] 任务循环退出")


# ===========================================================================
# Tab 3: Ping 探活
# ===========================================================================
class PingProbeTab(ttk.Frame):

    def __init__(self, parent, app_ref=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.app_ref = app_ref
        self.devices = load_json(PING_DEVICES_FILE, [])
        self.ping_runner = None
        self.ping_stop = threading.Event()
        self._build_ui()
        self._refresh_device_list()

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Button(toolbar, text="➕ 添加设备", command=self._add_device).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="✏ 编辑", command=self._edit_device).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="🗑 删除", command=self._del_device).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        self.btn_ping_start = ttk.Button(toolbar, text="▶ 开始周期 Ping", command=self._start_ping)
        self.btn_ping_start.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_ping_stop = ttk.Button(toolbar, text="⏹ 停止", command=self._stop_ping, state=tk.DISABLED)
        self.btn_ping_stop.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="🔍 单次 Ping 选中", command=self._single_ping).pack(side=tk.LEFT, padx=(0, 6))

        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Label(toolbar, text="间隔(s):").pack(side=tk.LEFT)
        self.ping_interval_var = tk.IntVar(value=DEFAULT_PING_INTERVAL)
        ttk.Spinbox(toolbar, from_=10, to=3600, width=6,
                    textvariable=self.ping_interval_var).pack(side=tk.LEFT, padx=(4, 12))

        cols = ("name", "host", "alias", "last_latency", "last_status", "last_time")
        self.dev_tree = ttk.Treeview(self, columns=cols, show="headings", height=13)

        headings = {"name": "设备名称", "host": "主机/IP", "alias": "备注",
                    "last_latency": "延迟(ms)", "last_status": "状态", "last_time": "最后Ping"}
        widths = {"name": 140, "host": 180, "alias": 120, "last_latency": 80,
                  "last_status": 90, "last_time": 155}
        for c in cols:
            self.dev_tree.heading(c, text=headings[c])
            self.dev_tree.column(c, width=widths[c])

        self.dev_tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.dev_tree.bind("<Double-1>", lambda e: self._edit_device())

        self.ping_stat = ttk.Label(self, text="")
        self.ping_stat.pack(fill=tk.X, padx=8, pady=(0, 8))

    def _refresh_device_list(self):
        for item in self.dev_tree.get_children():
            self.dev_tree.delete(item)
        for d in self.devices:
            self.dev_tree.insert("", tk.END, values=(
                d.get("name", ""), d.get("host", ""), d.get("alias", ""),
                d.get("last_latency", "-"), d.get("last_status", "-"), d.get("last_time", "-")
            ))
        self.ping_stat.config(text=f"📡 设备总数: {len(self.devices)}")

    def _device_editor(self, title, existing=None):
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.grab_set()
        dlg.geometry("420x260")
        dlg.resizable(False, False)

        fields = ttk.Frame(dlg)
        fields.pack(padx=20, pady=(20, 10), fill=tk.X)

        ttk.Label(fields, text="设备名称:").grid(row=0, column=0, sticky=tk.W, pady=6)
        n_var = tk.StringVar(value=existing.get("name", "") if existing else "")
        ttk.Entry(fields, textvariable=n_var, width=36).grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(fields, text="主机/IP:").grid(row=1, column=0, sticky=tk.W, pady=6)
        h_var = tk.StringVar(value=existing.get("host", "") if existing else "")
        ttk.Entry(fields, textvariable=h_var, width=36).grid(row=1, column=1, padx=8, pady=6)

        ttk.Label(fields, text="备注:").grid(row=2, column=0, sticky=tk.W, pady=6)
        a_var = tk.StringVar(value=existing.get("alias", "") if existing else "")
        ttk.Entry(fields, textvariable=a_var, width=36).grid(row=2, column=1, padx=8, pady=6)

        bf = ttk.Frame(dlg)
        bf.pack(pady=(10, 16))

        def do_save():
            name, host = n_var.get().strip(), h_var.get().strip()
            if not name or not host:
                messagebox.showwarning("提示", "名称和主机不能为空", parent=dlg)
                return
            data = {"name": name, "host": host, "alias": a_var.get().strip(),
                    "last_latency": "-", "last_status": "-", "last_time": "-"}
            if existing:
                idx = next((i for i, d in enumerate(self.devices) if d.get("name") == existing["name"]), None)
                if idx is not None:
                    data["last_latency"] = existing.get("last_latency", "-")
                    data["last_status"] = existing.get("last_status", "-")
                    data["last_time"] = existing.get("last_time", "-")
                    self.devices[idx] = data
            else:
                self.devices.append(data)
            save_json(PING_DEVICES_FILE, self.devices)
            self._refresh_device_list()
            dlg.destroy()

        ttk.Button(bf, text="保存", command=do_save).pack(side=tk.LEFT, padx=6)
        ttk.Button(bf, text="取消", command=dlg.destroy).pack(side=tk.LEFT, padx=6)

    def _add_device(self):
        self._device_editor("添加 Ping 设备")

    def _edit_device(self):
        sel = self.dev_tree.selection()
        if not sel:
            return
        name = self.dev_tree.item(sel[0])["values"][0]
        dev = next((d for d in self.devices if d.get("name") == name), None)
        if dev:
            self._device_editor("编辑 Ping 设备", dev)

    def _del_device(self):
        sel = self.dev_tree.selection()
        if not sel:
            return
        name = self.dev_tree.item(sel[0])["values"][0]
        if messagebox.askyesno("确认", f"删除设备「{name}」？"):
            self.devices = [d for d in self.devices if d.get("name") != name]
            save_json(PING_DEVICES_FILE, self.devices)
            self._refresh_device_list()

    def _single_ping(self):
        sel = self.dev_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选中一个设备")
            return
        name = self.dev_tree.item(sel[0])["values"][0]
        dev = next((d for d in self.devices if d.get("name") == name), None)
        if dev:
            threading.Thread(target=self._do_ping, args=(dev,), daemon=True).start()

    def _do_ping(self, dev):
        host = dev["host"]
        ok, latency, msg = ping_host(host)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dev["last_latency"] = f"{latency}" if latency is not None else "-"
        dev["last_status"] = "✅ 通" if ok else "❌ 断"
        dev["last_time"] = now
        save_json(PING_DEVICES_FILE, self.devices)
        self.after(0, self._refresh_device_list)

        if not ok and self.app_ref:
            cfg = self.app_ref.get_alarm_config()
            alarm_msg = (
                f"Ping 探活告警\n"
                f"设备: {dev['name']} ({host})\n"
                f"时间: {now}\n"
                f"结果: {msg}\n"
                f"\n请检查该设备网络连接！"
            )
            self.after(0, lambda m=alarm_msg: AlarmPopup(
                self.winfo_toplevel(),
                title=f"⚠️ 设备离线: {dev['name']}",
                message=m,
                sound_path=cfg.get("sound_file"),
            ))

    def _start_ping(self):
        if not self.devices:
            messagebox.showinfo("提示", "请先添加要 Ping 的设备")
            return
        if self.ping_runner and self.ping_runner.is_alive():
            messagebox.showinfo("提示", "周期 Ping 已在运行中")
            return
        self.ping_stop.clear()
        self.ping_runner = threading.Thread(target=self._ping_loop, daemon=True)
        self.ping_runner.start()
        self.btn_ping_start.config(state=tk.DISABLED)
        self.btn_ping_stop.config(state=tk.NORMAL)
        self.ping_stat.config(text=f"📡 周期 Ping 运行中... 间隔: {self.ping_interval_var.get()}s")

    def _stop_ping(self):
        self.ping_stop.set()
        self.btn_ping_start.config(state=tk.NORMAL)
        self.btn_ping_stop.config(state=tk.DISABLED)
        self.ping_stat.config(text=f"📡 设备总数: {len(self.devices)}  |  已停止")

    def _ping_loop(self):
        interval = self.ping_interval_var.get()
        while not self.ping_stop.is_set():
            for dev in self.devices:
                if self.ping_stop.is_set():
                    break
                self._do_ping(dev)
            for _ in range(interval):
                if self.ping_stop.is_set():
                    break
                time.sleep(1)


# ===========================================================================
# Tab 4: 告警设置
# ===========================================================================
class AlarmSettingsTab(ttk.Frame):

    def __init__(self, parent, app_ref=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.app_ref = app_ref
        self.config = load_json(CONFIG_FILE, {})
        self._build_ui()
        self._load_config()

    def _build_ui(self):
        main = ttk.Frame(self)
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        row0 = ttk.Frame(main)
        row0.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(row0, text="弹窗标题:", font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)
        self.title_var = tk.StringVar()
        ttk.Entry(row0, textvariable=self.title_var, width=55).pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)

        ttk.Label(main, text="告警提示语模板 (可用变量: {url}, {task}, {result}, {time}):",
                  font=("Microsoft YaHei UI", 10)).pack(anchor=tk.W, pady=(4, 2))
        self.msg_text = scrolledtext.ScrolledText(main, height=7, font=("Microsoft YaHei UI", 10), wrap=tk.WORD)
        self.msg_text.pack(fill=tk.X, pady=(0, 12))

        row_sound = ttk.Frame(main)
        row_sound.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(row_sound, text="告警声音文件 (.wav):", font=("Microsoft YaHei UI", 10)).pack(side=tk.LEFT)
        self.sound_var = tk.StringVar()
        self.sound_entry = ttk.Entry(row_sound, textvariable=self.sound_var, width=42)
        self.sound_entry.pack(side=tk.LEFT, padx=10)
        ttk.Button(row_sound, text="浏览...", command=self._browse_sound).pack(side=tk.LEFT, padx=4)
        ttk.Button(row_sound, text="清除", command=lambda: self.sound_var.set("")).pack(side=tk.LEFT, padx=2)

        note = ttk.Label(main,
                         text="💡 不指定声音文件则使用系统蜂鸣三连(880Hz)\n"
                              "💡 提示语留空则使用默认模板\n"
                              "💡 支持变量替换: {url}=目标地址, {task}=任务名, {result}=探测结果, {time}=时间\n"
                              "💡 此设置对「探活任务」和「Ping探活」生效，「URL业务探测」不触发告警",
                         font=("Microsoft YaHei UI", 9), foreground="gray")
        note.pack(anchor=tk.W, pady=(4, 16))

        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="💾 保存设置", command=self._save_config).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(btn_frame, text="🔊 测试告警(声音)", command=self._test_sound).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(btn_frame, text="🚨 测试完整告警(弹窗+声音)", command=self._test_full_alarm).pack(side=tk.LEFT)

    def _load_config(self):
        self.title_var.set(self.config.get("popup_title", "⚠️ 业务中断告警"))
        default_msg = (
            "检测到业务中断！\n\n"
            "目标: {url}\n"
            "结果: {result}\n"
            "时间: {time}\n\n"
            "请立即检查网络设备及配置！"
        )
        self.msg_text.insert("1.0", self.config.get("popup_message", default_msg))
        self.sound_var.set(self.config.get("sound_file", ""))

    def _browse_sound(self):
        path = filedialog.askopenfilename(
            title="选择告警声音文件",
            filetypes=[("WAV 音频", "*.wav"), ("所有文件", "*.*")],
        )
        if path:
            self.sound_var.set(path)

    def _save_config(self):
        self.config["popup_title"] = self.title_var.get().strip()
        self.config["popup_message"] = self.msg_text.get("1.0", tk.END).strip()
        self.config["sound_file"] = self.sound_var.get().strip()
        save_json(CONFIG_FILE, self.config)
        messagebox.showinfo("保存成功", "告警设置已保存")

    def get_config(self):
        return {
            "popup_title": self.title_var.get().strip() or "⚠️ 业务中断告警",
            "popup_message": self.msg_text.get("1.0", tk.END).strip(),
            "sound_file": self.sound_var.get().strip() or None,
        }

    def _test_sound(self):
        sound = self.sound_var.get().strip()
        play_alarm(sound if sound else None)

    def _test_full_alarm(self):
        cfg = self.get_config()
        msg = cfg["popup_message"].format(
            url="https://example.com/test", task="测试任务",
            result="✗ 连接超时", time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        AlarmPopup(
            self.winfo_toplevel(),
            title=cfg["popup_title"],
            message=msg,
            sound_path=cfg["sound_file"],
        )


# ===========================================================================
# 主窗口
# ===========================================================================
class BusinessProbeApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v2.3")
        self.geometry("1150x720")
        self.minsize(950, 580)

        self.update_idletasks()
        w, h = 1150, 720
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.url_tab = URLProbeTab(nb, app_ref=self)
        nb.add(self.url_tab, text="  🌐 URL业务探测  ")

        self.task_tab = TaskProbeTab(nb, app_ref=self)
        nb.add(self.task_tab, text="  📋 探活任务  ")

        # 让 Tab1 能引用 Tab2 (用于导入任务)
        self.url_tab.task_tab_ref = self.task_tab

        self.ping_tab = PingProbeTab(nb, app_ref=self)
        nb.add(self.ping_tab, text="  📡 Ping探活  ")

        self.alarm_tab = AlarmSettingsTab(nb, app_ref=self)
        nb.add(self.alarm_tab, text="  🔔 告警设置  ")

        self.status_bar = ttk.Label(self, text="就绪 | v2.3",
                                    font=("Microsoft YaHei UI", 8), foreground="gray")
        self.status_bar.pack(fill=tk.X, padx=8, pady=(0, 4))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def get_alarm_config(self):
        return self.alarm_tab.get_config()

    def _on_close(self):
        self.task_tab._stop_all()
        self.ping_tab._stop_ping()
        self.destroy()


if __name__ == "__main__":
    app = BusinessProbeApp()
    app.mainloop()
