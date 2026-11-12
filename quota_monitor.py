#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
quota_monitor.py - Antigravity Quota & Recovery Live Monitor
=============================================================================
Theo dõi hạn mức sử dụng (Quota), trạng thái hoạt động và thời gian đếm ngược
hồi phục của Google Antigravity theo thời gian thực bằng thư viện Rich.

Tính năng nổi bật:
  1. Quét tự động cấu hình & log của Antigravity (~/.gemini/antigravity-cli, ~/.antigravity...)
  2. Lắng nghe file log trực tiếp (Live File Watcher) phát hiện ngay lỗi 429 (Rate Limit / Quota Exceeded)
  3. Bắt model AI hiện hành (Claude Sonnet 4.6, Claude Opus 4.6, Gemini 3.8 Flash...)
  4. Thanh tiến trình động [████████████████░░░░] đổi màu:
     - Xanh lá (> 50%), Vàng hổ phách (20-50%), Đỏ rực (< 20% hoặc lỗi 429)
     - Refill Bar nạp dần theo thời gian đếm ngược reset HH:MM:SS
  5. Mini Live Dashboard (rich.live.Live), tần số 1Hz, CPU siêu nhẹ < 0.1%
  6. Thoát sạch sẽ với phím 'q' hoặc Ctrl+C mà không làm treo terminal.
=============================================================================
"""

import argparse
import glob
import json
import os
import re
import select
import sys
import termios
import time
import tty
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

# ==============================================================================
# CẤU HÌNH & HẰNG SỐ
# ==============================================================================

# Ước lượng dung lượng context / rate limit theo từng họ model
DEFAULT_MODEL_LIMITS = {
    "Claude Sonnet": 200_000,
    "Claude Opus": 200_000,
    "Gemini 3.8 Flash": 1_000_000,
    "Gemini 3.7 Flash": 1_000_000,
    "Gemini 3.6 Flash": 1_000_000,
    "Gemini 3.1 Pro": 2_000_000,
    "GPT-OSS 120B": 128_000,
    "Default": 200_000,
}

# Regex bắt lỗi RESOURCE_EXHAUSTED / 429 / Individual quota reached trong log Antigravity:
# Mẫu: E1001 20:06:57.072398 ... RESOURCE_EXHAUSTED (code 429): Individual quota reached... Resets in 4h6m48s.
ERROR_QUOTA_REGEX = re.compile(
    r"^[EWD](\d{4})\s+(\d{2}:\d{2}:\d{2}\.\d+).*?(?:RESOURCE_EXHAUSTED|Individual quota reached|code\s+429|Rate\s+limit).*?Resets\s+in\s+([0-9hmsd\s:]+)",
    re.IGNORECASE,
)

# Regex bắt sự kiện chuyển đổi hoặc nạp model
MODEL_RESOLVE_REGEX = re.compile(
    r"(?:Resolving model|Propagating selected model override to backend:\s*label=[\"']?)([^\"\n\r]+)[\"']?",
    re.IGNORECASE,
)

# Regex phân tích các chuỗi thời gian (e.g. 4h6m48s, 1d 2h, 30m, 15s)
DURATION_REGEX = re.compile(
    r"(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?",
    re.IGNORECASE,
)


def parse_duration(s: str) -> timedelta:
    """Chuyển đổi chuỗi thời gian như '4h6m48s', '03:41:20', '30m' thành timedelta."""
    if not s:
        return timedelta(0)
    s = s.strip()

    # Kiểm tra dạng HH:MM:SS
    col = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", s)
    if col:
        h, m, sec = map(int, col.groups())
        return timedelta(hours=h, minutes=m, seconds=sec)

    # Kiểm tra dạng Xd Yh Zm Ws
    m = DURATION_REGEX.search(s)
    if m:
        days = int(m.group(1)) if m.group(1) else 0
        hours = int(m.group(2)) if m.group(2) else 0
        mins = int(m.group(3)) if m.group(3) else 0
        secs = int(m.group(4)) if m.group(4) else 0
        if days or hours or mins or secs:
            return timedelta(days=days, hours=hours, minutes=mins, seconds=secs)

    return timedelta(0)


def format_countdown(td: timedelta) -> str:
    """Định dạng thời gian đếm ngược dạng '03h 41m 20s'."""
    total_seconds = max(0, int(td.total_seconds()))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}h {minutes:02d}m {seconds:02d}s"


def clean_model_display(name: str) -> str:
    """Rút gọn tên model hiển thị sạch sẽ trên tiêu đề (e.g. Gemini 3.8 Flash)."""
    if not name:
        return "Unknown Model"
    # Bỏ hậu tố như (Thinking), (High), (Medium)...
    cleaned = re.sub(r"\s*\([^)]*\)", "", name).strip()
    return cleaned if cleaned else name.strip()


def get_short_model_tag(name: str) -> str:
    """Tạo nhãn ngắn gọn trước thanh bar (e.g. Sonnet Quota, Flash Quota)."""
    name_lower = name.lower()
    if "sonnet" in name_lower:
        return "Sonnet"
    if "opus" in name_lower:
        return "Opus"
    if "flash" in name_lower:
        return "Flash"
    if "pro" in name_lower:
        return "Gemini Pro"
    parts = clean_model_display(name).split()
    return parts[0] if parts else "Model"


# ==============================================================================
# HỆ THỐNG PHÁT HIỆN & RÀ SOÁT TÀI NGUYÊN ANTIGRAVITY
# ==============================================================================

class AntigravityEnvironment:
    """Quản lý các đường dẫn cấu hình, log và phiên làm việc của Antigravity."""

    def __init__(self, custom_log_dir: Optional[str] = None):
        self.custom_log_dir = custom_log_dir
        self.base_dir = self._detect_base_dir()
        self.log_dir = self._detect_log_dir()
        self.settings_file = os.path.join(self.base_dir, "settings.json") if self.base_dir else None
        self.last_conv_file = os.path.join(self.base_dir, "cache", "last_conversations.json") if self.base_dir else None
        self.brain_dir = os.path.join(self.base_dir, "brain") if self.base_dir else None

        # Bộ đệm để tối ưu CPU khi đọc token
        self._cached_tokens = 0
        self._cached_steps = 0
        self._last_transcript_path = None
        self._last_transcript_mtime = 0.0
        self._last_transcript_size = 0

    def _detect_base_dir(self) -> str:
        candidates = [
            os.path.expanduser("~/.gemini/antigravity-cli"),
            os.path.expanduser("~/.antigravity"),
            os.path.expanduser("~/.config/antigravity"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return candidates[0]

    def _detect_log_dir(self) -> str:
        if self.custom_log_dir and os.path.exists(self.custom_log_dir):
            return self.custom_log_dir
        if self.base_dir:
            cand = os.path.join(self.base_dir, "log")
            if os.path.exists(cand):
                return cand
        return ""

    def get_active_log_file(self) -> Optional[str]:
        """Xác định file log hiện hành (ưu tiên symlink cli.log)."""
        if not self.log_dir or not os.path.exists(self.log_dir):
            return None

        # 1. Kiểm tra symlink cli.log
        if self.base_dir:
            cli_log = os.path.join(self.base_dir, "cli.log")
            if os.path.exists(cli_log):
                return os.path.realpath(cli_log)

        # 2. Tìm file cli-*.log có mtime mới nhất
        logs = glob.glob(os.path.join(self.log_dir, "cli-*.log"))
        if logs:
            return max(logs, key=os.path.getmtime)
        return None

    def read_configured_model(self) -> str:
        """Đọc model hiện hành từ settings.json."""
        if self.settings_file and os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    model = data.get("model")
                    if model:
                        return str(model)
            except Exception:
                pass
        return "Gemini 3.8 Flash (High)"

    def get_active_conversation_id(self) -> Optional[str]:
        """Lấy conversation ID đang hoạt động."""
        if self.last_conv_file and os.path.exists(self.last_conv_file):
            try:
                with open(self.last_conv_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    cwd = os.getcwd()
                    if cwd in data:
                        return data[cwd]
                    if "/home/admin1" in data:
                        return data["/home/admin1"]
                    if data:
                        return next(iter(data.values()))
            except Exception:
                pass

        if self.brain_dir and os.path.exists(self.brain_dir):
            dirs = [d for d in glob.glob(os.path.join(self.brain_dir, "*")) if os.path.isdir(d)]
            if dirs:
                return os.path.basename(max(dirs, key=os.path.getmtime))
        return None

    def get_session_token_usage(self) -> Tuple[int, int]:
        """
        Tính số lượng token đã dùng trong session hiện hành.
        Có cơ chế đệm file stat (mtime, size) để đảm bảo CPU < 0.1%.
        """
        conv_id = self.get_active_conversation_id()
        if not conv_id or not self.brain_dir:
            return self._cached_tokens, self._cached_steps

        transcript_path = os.path.join(self.brain_dir, conv_id, ".system_generated", "logs", "transcript.jsonl")
        if not os.path.exists(transcript_path):
            return self._cached_tokens, self._cached_steps

        try:
            stat = os.stat(transcript_path)
            # Nếu file không thay đổi về kích thước và mtime, trả về cache ngay lập tức
            if (
                transcript_path == self._last_transcript_path
                and stat.st_mtime == self._last_transcript_mtime
                and stat.st_size == self._last_transcript_size
            ):
                return self._cached_tokens, self._cached_steps

            total_chars = 0
            steps = 0
            with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        steps += 1
                        content = row.get("content") or ""
                        thinking = row.get("thinking") or ""
                        tool_calls = row.get("tool_calls")
                        tool_str = json.dumps(tool_calls) if tool_calls else ""
                        total_chars += len(content) + len(thinking) + len(tool_str)
                    except Exception:
                        continue

            est_tokens = max(0, total_chars // 4)
            self._cached_tokens = est_tokens
            self._cached_steps = steps
            self._last_transcript_path = transcript_path
            self._last_transcript_mtime = stat.st_mtime
            self._last_transcript_size = stat.st_size
            return est_tokens, steps
        except Exception:
            return self._cached_tokens, self._cached_steps

    def get_agy_uptime(self, start_monitor_time: float) -> str:
        """Đo lường thời gian hoạt động (Uptime) của tiến trình Antigravity."""
        now = time.time()
        # 1. Tìm tiến trình agy trong /proc
        try:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                cmd_path = f"/proc/{pid}/cmdline"
                if os.path.exists(cmd_path):
                    with open(cmd_path, "rb") as f:
                        cmd = f.read().decode("utf-8", errors="ignore")
                        if "agy" in cmd and "quota_monitor" not in cmd:
                            mtime = os.path.getmtime(f"/proc/{pid}")
                            elapsed = int(now - mtime)
                            h = elapsed // 3600
                            m = (elapsed % 3600) // 60
                            s = elapsed % 60
                            return f"{h:02d}:{m:02d}:{s:02d}"
        except Exception:
            pass

        # 2. Dùng thời gian mở file log hiện tại
        active_log = self.get_active_log_file()
        if active_log and os.path.exists(active_log):
            try:
                ctime = os.path.getctime(active_log)
                elapsed = int(now - ctime)
                h = elapsed // 3600
                m = (elapsed % 3600) // 60
                s = elapsed % 60
                return f"{h:02d}:{m:02d}:{s:02d}"
            except Exception:
                pass

        # 3. Fallback: Thời gian chạy của monitor
        elapsed = int(now - start_monitor_time)
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        return f"{h:02d}:{m:02d}:{s:02d}"


# ==============================================================================
# LOG WATCHER & QUOTA LOCK MANAGER
# ==============================================================================

class QuotaLockInfo:
    """Quản lý trạng thái khóa hạn mức và tiến độ hồi phục của một model."""

    def __init__(self, model: str, error_time: datetime, duration: timedelta, resets_in_str: str):
        self.model = model
        self.error_time = error_time
        self.duration = duration
        self.total_seconds = max(1.0, duration.total_seconds())
        self.unlock_time = error_time + duration
        self.resets_in_str = resets_in_str

    def is_locked(self, at_time: Optional[datetime] = None) -> bool:
        now = at_time or datetime.now()
        return now < self.unlock_time

    def remaining_seconds(self, at_time: Optional[datetime] = None) -> float:
        now = at_time or datetime.now()
        return max(0.0, (self.unlock_time - now).total_seconds())

    def refill_percentage(self, at_time: Optional[datetime] = None) -> float:
        """Tính phần trăm hồi phục theo thời gian đếm ngược (0% -> 100%)."""
        now = at_time or datetime.now()
        elapsed = (now - self.error_time).total_seconds()
        pct = (elapsed / self.total_seconds) * 100.0
        return min(100.0, max(0.0, pct))


class LogWatcher:
    """Theo dõi log theo thời gian thực (tail -f) để bắt sự kiện model và lỗi 429."""

    def __init__(self, env: AntigravityEnvironment):
        self.env = env
        self.active_log_path: Optional[str] = None
        self.file_handle = None
        self.last_pos = 0
        self.last_inode = None
        self.last_stat_check = 0.0
        self.active_model: str = env.read_configured_model()
        self.locks: Dict[str, QuotaLockInfo] = {}

    def scan_recent_logs(self, hours: int = 24):
        """Quét các file log gần đây để khôi phục các lệnh khóa quota còn hiệu lực."""
        if not self.env.log_dir or not os.path.exists(self.env.log_dir):
            return

        logs = sorted(glob.glob(os.path.join(self.env.log_dir, "cli-*.log")), key=os.path.getmtime)
        now = datetime.now()

        for log_path in logs:
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(log_path))
                if (now - mtime).total_seconds() > hours * 3600:
                    continue

                curr_model = "Claude Sonnet 4.6"
                year = mtime.year

                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        mm = MODEL_RESOLVE_REGEX.search(line)
                        if mm:
                            curr_model = mm.group(1).strip("\"' ")

                        em = ERROR_QUOTA_REGEX.search(line)
                        if em:
                            err_time_str = em.group(2)
                            dur_str = em.group(3)
                            date_match = re.match(r"^[EWD](\d{2})(\d{2})", line)
                            if date_match:
                                month = int(date_match.group(1))
                                day = int(date_match.group(2))
                                t_parts = err_time_str.split(":")
                                h = int(t_parts[0])
                                mi = int(t_parts[1])
                                s = int(t_parts[2].split(".")[0])
                                err_time = datetime(year, month, day, h, mi, s)
                                dur = parse_duration(dur_str)
                                lock = QuotaLockInfo(curr_model, err_time, dur, dur_str)
                                if lock.is_locked(now):
                                    self.locks[curr_model] = lock
            except Exception:
                continue

    def poll_new_events(self):
        """Đọc phần log mới ghi thêm và cập nhật trạng thái."""
        target_path = self.env.get_active_log_file()
        if not target_path or not os.path.exists(target_path):
            return

        now = datetime.now()
        year = now.year

        try:
            stat = os.stat(target_path)
            # Kiểm tra thay đổi inode hoặc đường dẫn log file
            if self.file_handle is None or target_path != self.active_log_path or stat.st_ino != self.last_inode:
                if self.file_handle:
                    self.file_handle.close()
                self.active_log_path = target_path
                self.last_inode = stat.st_ino
                self.file_handle = open(target_path, "r", encoding="utf-8", errors="ignore")
                self.file_handle.seek(0, os.SEEK_END)
                self.last_pos = self.file_handle.tell()
                return

            # Nếu kích thước file không tăng, bỏ qua
            if stat.st_size <= self.last_pos:
                return

            new_lines = self.file_handle.readlines()
            self.last_pos = self.file_handle.tell()

            for line in new_lines:
                mm = MODEL_RESOLVE_REGEX.search(line)
                if mm:
                    new_model = mm.group(1).strip("\"' ")
                    if new_model:
                        self.active_model = new_model

                em = ERROR_QUOTA_REGEX.search(line)
                if em:
                    err_time_str = em.group(2)
                    dur_str = em.group(3)
                    date_match = re.match(r"^[EWD](\d{2})(\d{2})", line)
                    if date_match:
                        month = int(date_match.group(1))
                        day = int(date_match.group(2))
                        t_parts = err_time_str.split(":")
                        h = int(t_parts[0])
                        mi = int(t_parts[1])
                        s = int(t_parts[2].split(".")[0])
                        err_time = datetime(year, month, day, h, mi, s)
                        dur = parse_duration(dur_str)
                        lock = QuotaLockInfo(self.active_model, err_time, dur, dur_str)
                        self.locks[self.active_model] = lock
        except Exception:
            pass

    def get_active_lock(self) -> Optional[QuotaLockInfo]:
        """Lấy khóa quota đang còn hiệu lực."""
        now = datetime.now()
        active_list = [lock for lock in self.locks.values() if lock.is_locked(now)]
        if active_list:
            for lock in active_list:
                if lock.model == self.active_model:
                    return lock
            return active_list[0]
        return None

    def close(self):
        if self.file_handle:
            try:
                self.file_handle.close()
            except Exception:
                pass


# ==============================================================================
# GIAO DIỆN MINI LIVE DASHBOARD
# ==============================================================================

class DashboardRenderer:
    """Vẽ giao diện bảng điều khiển thời gian thực bằng Rich."""

    def __init__(self, use_ascii_box: bool = False, bar_width: int = 16, dual_mode: bool = False):
        self.use_ascii_box = use_ascii_box
        self.bar_width = bar_width
        self.dual_mode = dual_mode

    def render_block_bar(self, percent: float, color: str) -> str:
        """Tạo thanh tiến trình khối dạng [████████████████░░░░] đẹp mắt."""
        clamped_pct = max(0.0, min(100.0, percent))
        filled = int(round(self.bar_width * clamped_pct / 100.0))
        filled = max(0, min(self.bar_width, filled))
        empty = self.bar_width - filled
        bar = f"[{color}]" + "█" * filled + f"[/{color}]" + "[dim]" + "░" * empty + "[/dim]"
        return f"[{bar}]"

    def render_dashboard(
        self,
        active_model: str,
        uptime_str: str,
        lock_info: Optional[QuotaLockInfo],
        tokens_used: int,
        steps: int,
    ) -> Panel:
        """Sinh ra renderable Panel cho Live view."""
        grid = Table.grid(expand=True)
        grid.add_column()

        # Dòng 1: Model đang chạy & Uptime
        top_table = Table.grid(expand=True)
        top_table.add_column(justify="left")
        top_table.add_column(justify="right")

        clean_active = clean_model_display(active_model)
        status_tag = (
            "[bold yellow](Locked: 429)[/bold yellow]"
            if (lock_info and lock_info.model == active_model)
            else "[bold green](Active)[/bold green]"
        )

        top_table.add_row(
            Text.from_markup(f"Model: [bold cyan]{clean_active}[/bold cyan] {status_tag}"),
            Text.from_markup(f"Uptime: [bold white]{uptime_str}[/bold white]"),
        )
        grid.add_row(top_table)

        # Tính toán dung lượng token cho model hiện hành
        model_limit = DEFAULT_MODEL_LIMITS.get("Default", 200_000)
        for key, val in DEFAULT_MODEL_LIMITS.items():
            if key.lower() in active_model.lower():
                model_limit = val
                break

        used_ratio = min(1.0, tokens_used / model_limit) if model_limit else 0.0
        quota_pct = max(0.0, (1.0 - used_ratio) * 100.0)

        # Ngưỡng màu: > 50% Green, 20-50% Yellow, < 20% Red
        if quota_pct > 50.0:
            quota_color = "green"
        elif quota_pct >= 20.0:
            quota_color = "yellow"
        else:
            quota_color = "bold red"

        # TRƯỜNG HỢP 1: Có model bị khóa Quota (429)
        if lock_info and lock_info.is_locked():
            refill_pct = lock_info.refill_percentage()
            rem_seconds = lock_info.remaining_seconds()
            countdown_str = format_countdown(timedelta(seconds=int(rem_seconds)))
            short_tag = get_short_model_tag(lock_info.model)

            # Refill bar nạp dần
            bar_color = "red" if refill_pct < 20 else ("yellow" if refill_pct < 80 else "green")
            bar_str = self.render_block_bar(refill_pct, bar_color)

            # Nếu chế độ dual mode, hiển thị cả 2 thanh
            if self.dual_mode and lock_info.model != active_model:
                act_tag = get_short_model_tag(active_model)
                act_bar = self.render_block_bar(quota_pct, quota_color)
                token_desc = f"{tokens_used / 1000:.1f}K / {model_limit / 1_000_000:.1f}M" if model_limit >= 1_000_000 else f"{tokens_used / 1000:.1f}K / {model_limit / 1000:.0f}K"
                grid.add_row(
                    Text.from_markup(f"{act_tag} Quota:  {act_bar} [bold {quota_color}]{int(quota_pct)}%[/bold {quota_color}] [dim]({token_desc} tokens)[/dim]")
                )

            # Dòng thanh Refill Bar
            quota_line = Text.from_markup(
                f"{short_tag} Quota: {bar_str} [bold red]{int(refill_pct)}%[/bold red] [bold yellow](Locked: 429)[/bold yellow]"
            )
            grid.add_row(quota_line)

            # Dòng đếm ngược hồi phục
            recovery_line = Text.from_markup(
                f"Hồi phục sau: [bold yellow]{countdown_str}[/bold yellow] [dim](Đang đếm lùi...)[/dim]"
            )
            grid.add_row(recovery_line)

        # TRƯỜNG HỢP 2: Hoạt động bình thường
        else:
            short_tag = get_short_model_tag(active_model)
            bar_str = self.render_block_bar(quota_pct, quota_color)
            token_desc = f"{tokens_used / 1000:.1f}K / {model_limit / 1_000_000:.1f}M" if model_limit >= 1_000_000 else f"{tokens_used / 1000:.1f}K / {model_limit / 1000:.0f}K"

            quota_line = Text.from_markup(
                f"{short_tag} Quota: {bar_str} [bold {quota_color}]{int(quota_pct)}%[/bold {quota_color}] [dim]({token_desc} tokens)[/dim]"
            )
            grid.add_row(quota_line)

            status_line = Text.from_markup(
                "Trạng thái:   [bold green]● Sẵn sàng hoạt động (Không bị khóa Quota)[/bold green]"
            )
            grid.add_row(status_line)

        # Khung viền Panel (SQUARE cho phong cách cổ điển ┌──┐ hoặc ROUNDED cho bo tròn ╭──╮)
        panel_box = box.SQUARE if self.use_ascii_box else box.ROUNDED
        return Panel(
            grid,
            title="[bold bright_white]AGY LIVE QUOTA & RECOVERY TRACKER[/bold bright_white]",
            title_align="left",
            border_style="bright_blue",
            box=panel_box,
            padding=(0, 1),
            subtitle="[dim]Nhấn 'q' hoặc 'Ctrl+C' để thoát[/dim]",
            subtitle_align="right",
        )


# ==============================================================================
# BẮT PHÍM BẤM TERMINAL KHÔNG CHẶN (ZERO CPU SELECT)
# ==============================================================================

class TerminalKeyHandler:
    """Bắt phím bấm terminal không chặn, đảm bảo CPU < 0.1% và thoát an toàn."""

    def __init__(self):
        self.is_tty = sys.stdin.isatty()
        self.old_settings = None

    def __enter__(self):
        if self.is_tty:
            try:
                self.old_settings = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self.old_settings = None
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.is_tty and self.old_settings:
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_settings)
            except Exception:
                pass

    def wait_for_key_or_timeout(self, timeout_sec: float = 1.0) -> Optional[str]:
        """Tạm dừng đến 1 giây mà không tốn CPU; phản hồi ngay khi có phím bấm."""
        try:
            rlist, _, _ = select.select([sys.stdin], [], [], timeout_sec)
            if rlist:
                ch = sys.stdin.read(1)
                return ch
        except Exception:
            time.sleep(timeout_sec)
        return None


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="AGY Quota Monitor - Giám sát Quota & Đếm ngược Reset của Antigravity theo thời gian thực"
    )
    parser.add_argument(
        "--simulate",
        type=str,
        default=None,
        help="Mô phỏng khóa 429 với thời gian reset (ví dụ: 4h6m48s, 30m, 15m)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Tên model chỉ định (mặc định tự động nhận diện từ Antigravity)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Đường dẫn thư mục log Antigravity tùy chọn",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Chỉ in ra một snapshot dashboard rồi thoát (dành cho cron/script)",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="Sử dụng viền vuông ASCII ┌───┐ thay cho viền bo tròn ╭───╮",
    )
    parser.add_argument(
        "--dual",
        action="store_true",
        help="Hiển thị cả thanh Quota của model Active và thanh hồi phục model bị Locked",
    )
    args = parser.parse_args()

    console = Console()
    env = AntigravityEnvironment(custom_log_dir=args.log_dir)
    watcher = LogWatcher(env)
    renderer = DashboardRenderer(use_ascii_box=args.ascii, dual_mode=args.dual)

    # 1. Quét lịch sử log tìm các khóa còn hiệu lực
    watcher.scan_recent_logs()

    # 2. Xử lý chế độ mô phỏng
    if args.simulate:
        sim_model = args.model or "Claude Sonnet 4.6"
        sim_dur = parse_duration(args.simulate)
        if sim_dur.total_seconds() <= 0:
            sim_dur = timedelta(hours=4, minutes=6, seconds=48)
        sim_lock = QuotaLockInfo(
            model=sim_model,
            error_time=datetime.now(),
            duration=sim_dur,
            resets_in_str=args.simulate,
        )
        watcher.locks[sim_model] = sim_lock

    if args.model:
        watcher.active_model = args.model

    # 3. Chế độ in một lần (Snapshot)
    start_time = time.time()
    if args.once:
        watcher.poll_new_events()
        lock = watcher.get_active_lock()
        tokens, steps = env.get_session_token_usage()
        uptime = env.get_agy_uptime(start_time)
        panel = renderer.render_dashboard(
            active_model=watcher.active_model,
            uptime_str=uptime,
            lock_info=lock,
            tokens_used=tokens,
            steps=steps,
        )
        console.print(panel)
        watcher.close()
        return

    # 4. Chế độ Mini Live Dashboard thời gian thực
    try:
        with TerminalKeyHandler() as key_handler:
            with Live(console=console, refresh_per_second=1, transient=False) as live:
                while True:
                    # Đọc log mới & cập nhật dữ liệu
                    watcher.poll_new_events()
                    lock = watcher.get_active_lock()
                    tokens, steps = env.get_session_token_usage()
                    uptime = env.get_agy_uptime(start_time)

                    # Sinh giao diện Panel
                    panel = renderer.render_dashboard(
                        active_model=watcher.active_model,
                        uptime_str=uptime,
                        lock_info=lock,
                        tokens_used=tokens,
                        steps=steps,
                    )
                    live.update(panel)

                    # Bắt phím 'q' hoặc Ctrl+C phản hồi ngay lập tức
                    ch = key_handler.wait_for_key_or_timeout(1.0)
                    if ch in ("q", "Q", "\x03"):
                        break
    except KeyboardInterrupt:
        pass
    finally:
        watcher.close()
        console.print("[dim]Đã dừng Quota Monitor. Chúc bạn một ngày làm việc hiệu quả![/dim]")


if __name__ == "__main__":
    main()
