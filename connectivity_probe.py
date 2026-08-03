#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
业务连通性探测脚本 (Business Connectivity Probe)
=================================================

用途:
    定期(默认每 3 分钟)从一个 URL 清单里随机抽查一个地址进行访问,
    一旦发现「连不上」或「返回非 2xx 状态码」, 立即用系统声音告警,
    并记录日志, 用于及时发现业务中断、定位网络设备配置问题。

特点:
    - 纯标准库实现, 无需 pip install, Windows 上直接运行
    - 探测失败判定: 网络错误(超时/DNS/拒绝) 或 HTTP 状态码非 2xx
    - 内置声音告警(winsound 蜂鸣, 或播放指定 wav)
    - 支持重试(默认重试 1 次, 避免瞬时抖动误报)
    - 完整日志记录(控制台 + 可选日志文件)
    - Ctrl+C 优雅退出

运行示例:
    python connectivity_probe.py -f urls.txt
    python connectivity_probe.py -f urls.txt -i 180 --timeout 10
    python connectivity_probe.py -f urls.txt --sound alarm.wav
    python connectivity_probe.py -f urls.txt --once        # 只探测一轮, 用于测试
    python connectivity_probe.py -f urls.txt --log probe.log
"""

import argparse
import logging
import random
import signal
import sys
import time
import urllib.error
import urllib.request

try:
    import winsound
    HAVE_WINSOUND = True
except ImportError:
    HAVE_WINSOUND = False


# ----------------------------------------------------------------------------
# 全局退出标志 (用于 Ctrl+C 优雅退出)
# ----------------------------------------------------------------------------
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logging.info("收到退出信号, 将在本轮结束后停止...")


signal.signal(signal.SIGINT, _handle_signal)
try:
    signal.signal(signal.SIGTERM, _handle_signal)
except (AttributeError, ValueError):
    pass


# ----------------------------------------------------------------------------
# 声音告警
# ----------------------------------------------------------------------------
def alarm(sound_path: str | None):
    """触发声音告警。优先播放指定 wav, 否则用系统蜂鸣三连。"""
    if not HAVE_WINSOUND:
        # 非 Windows 环境(如 Linux/Mac)无 winsound, 静默跳过, 仅依赖日志
        return
    try:
        if sound_path:
            winsound.PlaySound(sound_path, winsound.SND_FILENAME)
            return
        # 880Hz 蜂鸣三连, 间隔 0.1s, 足够刺耳提醒
        for _ in range(3):
            winsound.Beep(880, 300)
            time.sleep(0.1)
    except Exception as e:  # 声音播放失败绝不影响主流程
        logging.warning("声音告警触发失败: %s", e)


# ----------------------------------------------------------------------------
# 读取 URL 清单
# ----------------------------------------------------------------------------
def load_urls(path: str) -> list[str]:
    urls = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


# ----------------------------------------------------------------------------
# 单次探测
# ----------------------------------------------------------------------------
def probe_once(url: str, timeout: int) -> tuple[bool, int | None, str]:
    """
    访问单个 URL。
    返回: (是否成功, 状态码或None, 说明)
    - 成功: 2xx 状态码
    - 失败: 网络错误 / 非 2xx / 异常
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ConnectivityProbe/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
        if 200 <= status < 300:
            return True, status, "OK"
        return False, status, f"HTTP {status} (非 2xx)"
    except urllib.error.HTTPError as e:
        return False, e.code, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, None, f"网络错误: {e.reason}"
    except Exception as e:  # noqa: BLE001
        return False, None, f"异常: {e}"


def probe_with_retry(url: str, timeout: int, retries: int, retry_delay: int):
    """带重试的探测, 全部失败才算失败。"""
    last_status = None
    last_msg = ""
    for attempt in range(retries + 1):
        ok, status, msg = probe_once(url, timeout)
        if ok:
            return True, status, msg
        last_status, last_msg = status, msg
        if attempt < retries:
            logging.warning("  第 %d 次失败(%s), %ds 后重试...", attempt + 1, msg, retry_delay)
            time.sleep(retry_delay)
    return False, last_status, last_msg


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="业务连通性探测脚本: 定期随机抽查 URL 并声音告警",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-f", "--url-file", required=True, help="URL 清单文件(txt, 每行一个, # 开头为注释)")
    p.add_argument("-i", "--interval", type=int, default=180, help="探测间隔(秒), 默认 180 (3 分钟)")
    p.add_argument("--timeout", type=int, default=10, help="单次请求超时(秒), 默认 10")
    p.add_argument("--retries", type=int, default=1, help="失败前重试次数, 默认 1")
    p.add_argument("--retry-delay", type=int, default=2, help="重试间隔(秒), 默认 2")
    p.add_argument("--sound", default=None, help="告警音 wav 文件路径(不填则用系统蜂鸣)")
    p.add_argument("--log", default=None, help="日志文件路径(不填则仅输出到控制台)")
    p.add_argument("--once", action="store_true", help="只运行一轮探测后退出(用于测试)")
    return p.parse_args()


def setup_logging(log_path: str | None):
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_path:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


def main():
    args = parse_args()
    setup_logging(args.log)

    try:
        urls = load_urls(args.url_file)
    except FileNotFoundError:
        logging.error("URL 清单文件不存在: %s", args.url_file)
        sys.exit(1)

    if not urls:
        logging.error("URL 清单为空, 请检查 %s", args.url_file)
        sys.exit(1)

    logging.info("=" * 60)
    logging.info("业务连通性探测启动")
    logging.info("URL 清单: %s (%d 个地址)", args.url_file, len(urls))
    logging.info("探测间隔: %d 秒 | 超时: %d 秒 | 重试: %d 次",
                 args.interval, args.timeout, args.retries)
    logging.info("告警声音: %s", args.sound if args.sound else "系统蜂鸣")
    logging.info("失败判定: 网络错误 或 非 2xx 状态码")
    logging.info("=" * 60)

    round_no = 0
    while not _shutdown:
        round_no += 1
        url = random.choice(urls)
        logging.info("第 %d 轮: 抽查 %s", round_no, url)

        ok, status, msg = probe_with_retry(
            url, args.timeout, args.retries, args.retry_delay
        )

        if ok:
            logging.info("  ✓ 正常 (HTTP %s) %s", status, msg)
        else:
            logging.error("  ✗ 业务中断! (HTTP %s) %s -> %s", status, msg, url)
            alarm(args.sound)

        if args.once:
            break

        # 等待下一轮, 期间可被 Ctrl+C 中断
        for _ in range(args.interval):
            if _shutdown:
                break
            time.sleep(1)

    logging.info("探测已停止。")


if __name__ == "__main__":
    main()
