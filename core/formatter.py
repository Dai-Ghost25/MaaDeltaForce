from datetime import datetime


def simple(title: str, content: str) -> str:
    """简单推送：一段标题 + 一段正文"""
    return (
        f"### {title}\n\n"
        f"{content}\n\n"
        f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )


def report(title: str, items: list[tuple[str, str]]) -> str:
    """
    综合推送：标题 + 若干 (名称, 值) 字段
    items = [("任务", "收菜"), ("耗时", "3.2 秒"), ("结果", "成功")]
    """
    lines = [f"### {title}", ""]
    for name, value in items:
        lines.append(f"**{name}**: {value}  ")  # 两个空格换行
    lines.append("")
    lines.append(f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)