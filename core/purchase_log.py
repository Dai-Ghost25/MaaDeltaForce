# core/purchase_log.py
import json
import logging
from datetime import datetime
from pathlib import Path

from core.config import PROJECT_ROOT

logger = logging.getLogger(__name__)

LOG_FILE = PROJECT_ROOT / "logs" / "purchases.jsonl"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log_purchase(
    item: str,
    expect_price: int,
    expect_qty: int,
    balance_before: int,
    balance_after: int,
    note: str = "",
) -> dict:
    """
    记录一次购买尝试，追加写入 JSONL。
    返回写入的记录 dict，方便调用方后续处理。

    参数:
        item: 商品名
        expect_price: 预期单价
        expect_qty: 预期数量
        balance_before: 购买前余额
        balance_after: 购买后余额
        note: 备注（如"余额识别失败"）
    """
    spent = balance_before - balance_after

    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "item": item,
        "expect_price": expect_price,
        "expect_qty": expect_qty,
        "expect_total": expect_price * expect_qty,
        "before": balance_before,
        "after": balance_after,
        "spent": spent,
        "note": note,
    }

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(
            f"[购买记录] {item} 花费 {spent} "
            f"(余额 {balance_before} → {balance_after})"
        )
    except Exception as e:
        logger.error(f"写入购买日志失败: {e}")

    return record


def load_all() -> list[dict]:
    """读取全部购买记录"""
    if not LOG_FILE.is_file():
        return []
    records = []
    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(f"跳过无效行: {line[:50]}")
    return records


def summarize_today() -> dict:
    """统计今日购买情况"""
    today = datetime.now().strftime("%Y-%m-%d")
    records = [r for r in load_all() if r["time"].startswith(today)]

    by_item: dict[str, dict] = {}
    total_spent = 0

    for r in records:
        item = r["item"]
        if item not in by_item:
            by_item[item] = {"count": 0, "spent": 0, "success": 0}
        by_item[item]["count"] += 1
        by_item[item]["spent"] += r["spent"]
        if r["spent"] > 0 and r["spent"] <= r["expect_total"]:
            by_item[item]["success"] += 1
        total_spent += r["spent"]

    return {
        "date": today,
        "total_records": len(records),
        "total_spent": total_spent,
        "by_item": by_item,
    }


def format_summary() -> str:
    """格式化为可读的报告文本"""
    s = summarize_today()
    lines = [f"=== 今日购买 ({s['date']}) ==="]

    if not s["by_item"]:
        lines.append("无购买记录")
        return "\n".join(lines)

    for item, stat in s["by_item"].items():
        lines.append(
            f"  {item}: {stat['count']} 次尝试, "
            f"{stat['success']} 次成功, 花费 {stat['spent']}"
        )
    lines.append(f"  ---")
    lines.append(f"  总计: {s['total_records']} 次, 花费 {s['total_spent']}")
    return "\n".join(lines)