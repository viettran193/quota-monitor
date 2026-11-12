# ⚡ Antigravity Live Quota & Recovery Monitor (`quota_monitor.py`)

Công cụ độc lập giám sát hạn mức sử dụng (Quota), trạng thái hoạt động và thời gian đếm ngược hồi phục của Google Antigravity theo thời gian thực với thư viện Python `rich`.

---

## 📌 Tính năng chính

- 🔍 **Tự động rà soát log & phiên làm việc**:
  - Tự động nhận diện thư mục cấu hình và log của Antigravity (`~/.gemini/antigravity-cli/`, `~/.antigravity/`...).
  - Đọc cấu hình model hiện hành từ `settings.json`.
  - Phân tích transcript phiên làm việc để ước tính lượng token tiêu thụ.
- ⏱️ **Lắng nghe trực tiếp (Live File Watcher) & Bắt lỗi 429**:
  - Bắt ngay lập tức mã lỗi `RESOURCE_EXHAUSTED (code 429): Individual quota reached. ... Resets in 4h6m48s.`
  - Tự động trích xuất thời gian reset và kích hoạt đồng hồ đếm ngược `HH:MM:SS`.
- 📊 **Thanh tiến trình động (`render_block_bar` / `ProgressBar`)**:
  - **Trạng thái bình thường**: Đo % quota khả dụng với màu sắc đổi theo ngưỡng:
    - 🟩 **Xanh lá**: > 50%
    - 🟨 **Vàng hổ phách**: 20% - 50%
    - 🟥 **Đỏ rực**: < 20% hoặc khi chạm lỗi 429
  - **Trạng thái bị khóa Quota**: Chuyển sang thanh nạp phục hồi (**Refill Bar**) nhích mượt từng giây từ mức % hiện tại tới 100%.
- 🖥️ **Mini Live Dashboard siêu nhẹ**:
  - Làm mới mỗi 1 giây/lần với `rich.live.Live`.
  - Cơ chế đệm file stat (`st_mtime`, `st_size`) và `select()` giúp **tải CPU < 0.1%**.
  - Bắt phím **`q`** hoặc **`Ctrl+C`** thoát an toàn, không làm treo hay vỡ giao diện terminal.

---

## 🚀 Hướng dẫn cài đặt & Khởi chạy

### 1. Yêu cầu hệ thống
- Python 3.8+
- Thư viện `rich` (`pip install rich`)

### 2. Cấp quyền thực thi (nếu cần)
```bash
chmod +x /home/admin1/quota_monitor.py
```

### 3. Các lệnh chạy thông dụng

#### 🔹 Chạy giám sát thời gian thực (Mặc định)
```bash
python3 /home/admin1/quota_monitor.py
```
*Giao diện mẫu:*
```text
╭─ AGY LIVE QUOTA & RECOVERY TRACKER ──────────────────────────────────────────╮
│ Model: Gemini 3.8 Flash (Active)                            Uptime: 00:05:09 │
│ Sonnet Quota: [████░░░░░░░░░░░░] 27% (Locked: 429)                           │
│ Hồi phục sau: 02h 58m 58s (Đang đếm lùi...)                                  │
╰──────────────────────────────────────────── Nhấn 'q' hoặc 'Ctrl+C' để thoát ─╯
```

#### 🔹 Giao diện viền vuông ASCII cổ điển (`--ascii`)
```bash
python3 /home/admin1/quota_monitor.py --ascii
```
*Giao diện mẫu:*
```text
┌─ AGY LIVE QUOTA & RECOVERY TRACKER ──────────────────────────────────────────┐
│ Model: Gemini 3.8 Flash (Active)                            Uptime: 01:24:10 │
│ Sonnet Quota: [████████████████░░░░] 72% (Locked: 429)                       │
│ Hồi phục sau: 03h 41m 20s (Đang đếm lùi...)                                  │
└──────────────────────────────────────────── Nhấn 'q' hoặc 'Ctrl+C' để thoát ─┘
```

#### 🔹 Hiển thị song song 2 thanh (`--dual`)
Hiển thị cả thanh hạn mức model đang hoạt động và thanh hồi phục model bị khóa:
```bash
python3 /home/admin1/quota_monitor.py --dual
```
*Giao diện mẫu:*
```text
╭─ AGY LIVE QUOTA & RECOVERY TRACKER ──────────────────────────────────────────╮
│ Model: Gemini 3.8 Flash (Active)                            Uptime: 00:05:03 │
│ Flash Quota:  [███████████████░] 95% (49.2K / 1.0M tokens)                   │
│ Sonnet Quota: [████░░░░░░░░░░░░] 27% (Locked: 429)                           │
│ Hồi phục sau: 02h 59m 05s (Đang đếm lùi...)                                  │
╰──────────────────────────────────────────── Nhấn 'q' hoặc 'Ctrl+C' để thoát ─╯
```

#### 🔹 Mô phỏng trạng thái bị khóa để thử nghiệm (`--simulate`)
Dùng để kiểm tra hiệu ứng đếm ngược thời gian và thanh nạp phục hồi bất kỳ lúc nào:
```bash
# Mô phỏng khóa 15 phút với Claude Opus 4.6
python3 /home/admin1/quota_monitor.py --simulate 15m --model "Claude Opus 4.6"

# Mô phỏng khóa 4h6m48s với Claude Sonnet 4.6
python3 /home/admin1/quota_monitor.py --simulate 4h6m48s --model "Claude Sonnet 4.6"
```

#### 🔹 In snapshot 1 lần rồi thoát (`--once`)
Dành cho việc tích hợp vào shell script, crontab hoặc kiểm tra nhanh:
```bash
python3 /home/admin1/quota_monitor.py --once
```

---

## ⚙️ Bảng tham số dòng lệnh (CLI Arguments)

| Tham số | Ý nghĩa | Ví dụ |
| :--- | :--- | :--- |
| `-h`, `--help` | Hiển thị bảng trợ giúp hướng dẫn | `python3 quota_monitor.py --help` |
| `--simulate <time>` | Kích hoạt chế độ mô phỏng khóa 429 | `--simulate 4h6m48s` hoặc `--simulate 30m` |
| `--model <name>` | Chỉ định tên model hiển thị | `--model "Claude Sonnet 4.6"` |
| `--ascii` | Dùng viền vuông ASCII `┌──┐` thay cho viền bo tròn `╭──╮` | `--ascii` |
| `--dual` | Hiển thị đồng thời cả thanh Quota và thanh Refill | `--dual` |
| `--once` | In một lần snapshot rồi thoát | `--once` |
| `--log-dir <path>` | Chỉ định đường dẫn thư mục log tùy chọn | `--log-dir ~/.gemini/antigravity-cli/log` |

---

## ⌨️ Phím tắt điều khiển

- **`q`** hoặc **`Q`**: Thoát ngay lập tức khỏi dashboard.
- **`Ctrl+C`**: Ngắt chương trình an toàn, phục hồi terminal sạch sẽ.
