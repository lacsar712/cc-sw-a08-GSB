TOLERANCE_NM = 0.08


def judge(nominal: float, measured: float) -> tuple[str, str]:
    delta = abs(measured - nominal)
    if delta <= TOLERANCE_NM:
        return "合格", f"偏差 {delta:.4f} nm 在允差内"
    return "超差", f"偏差 {delta:.4f} nm 超过允差 {TOLERANCE_NM}"


def parse_hhmm(text: str) -> int:
    """'HH:MM' -> 当日分钟数 (0-1439)，非法输入抛 ValueError。"""
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到 {text!r}")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"时间格式应为 HH:MM，收到 {text!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时间超出 00:00-23:59 范围: {text!r}")
    return h * 60 + m


def fmt_hhmm(minutes: int) -> str:
    """当日分钟数 -> 'HH:MM'。"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def in_ban_window(now_minutes: int, start_minutes: int, end_minutes: int) -> bool:
    """每日禁写闭区间 [start, end]；start > end 视为跨零点窗口。"""
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes <= end_minutes
    return now_minutes >= start_minutes or now_minutes <= end_minutes
