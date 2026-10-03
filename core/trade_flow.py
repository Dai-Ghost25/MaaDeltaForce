# core/trade_flow.py
import logging
import time

from core.task_runner import get_runner
from core.parsing import parse_price_safe
from core.trade_logic import decide_buy
from core.trade_config import (
    get_buy_threshold,
    get_buy_qty,
    get_bargain_config,
    get_layout,
    get_item_scan_repeat,
)
from core.purchase_log import log_purchase

logger = logging.getLogger(__name__)


# Pipeline 入口常量
ENTRY_FIRST = "识别商品坐标"       # 首次：从收藏页找商品并进入
ENTRY_REFRESH = "返回商品收藏页面"  # 刷新：返回收藏页重新进入
ENTRY_BALANCE = "点击哈夫币位置"    # 显示并识别余额

# 余额合理范围
BALANCE_MIN = 0
BALANCE_MAX = 100_000_000_000


# ---------- 底层：识别价格 / 余额 ----------

def _enter_and_read_price(runner, item_name: str, entry: str, layout: str) -> int | None:
    """
    进入商品 → 按布局滑动 → 识别价格。
    entry: 首次用 ENTRY_FIRST，刷新用 ENTRY_REFRESH。
    """
    # 1. 进入商品
    runner.run_raw(
        entry,
        pipeline_override={"识别商品坐标": {"expected": item_name}},
        watch_nodes={},
    )

    # 2. 按布局滑动 + 识别价格
    max_entry = f"MAX!-{layout}"
    captured = runner.run_raw(
        max_entry,
        watch_nodes={"识别价格": "text"},
    )
    prices = [r for r in captured if r["node"] == "识别价格"]
    if not prices:
        return None
    return parse_price_safe(prices[0]["text"], min_price=1, max_price=10_000_000)


def _read_balance(runner) -> int | None:
    """点击哈夫币位置，识别余额。失败返回 None。"""
    captured = runner.run_raw(
        ENTRY_BALANCE,
        watch_nodes={"识别余额": "text"},
    )
    balances = [r for r in captured if r["node"] == "识别余额"]
    if not balances:
        return None
    return parse_price_safe(
        balances[0]["text"],
        min_price=BALANCE_MIN,
        max_price=BALANCE_MAX,
    )


# ---------- 买入 ----------

def _do_buy(runner, item_name: str, price: int, qty: int, layout: str) -> bool:
    """
    执行买入 + 验证 + 记录。
    返回 True 表示"确实花掉的钱在预期范围内"。
    """
    # 1. 买入前余额
    before = _read_balance(runner)
    if before is None:
        logger.warning(f"[{item_name}] 未识别到买入前余额")
        return False
    logger.info(f"[{item_name}] 买入前余额: {before}")

    # 2. 执行买入
    # buy_entry = f"买入-{layout}"
    # logger.info(f"[{item_name}] 执行买入: {buy_entry}")
    # runner.run_raw(buy_entry, watch_nodes={buy_entry: "text"})

    # 3. 买入后余额
    time.sleep(0.5)
    after = _read_balance(runner)
    if after is None:
        logger.warning(f"[{item_name}] 未识别到买入后余额")
        log_purchase(item_name, price, qty, before, before, note="余额识别失败")
        return False
    logger.info(f"[{item_name}] 买入后余额: {after}")

    # 4. 记录
    log_purchase(item_name, price, qty, before, after)

    # 5. 判断
    spent = before - after
    expect_total = price * qty

    if spent <= 0:
        logger.info(f"[{item_name}] 未花费，可能没买到")
        return False
    if spent > expect_total:
        logger.warning(f"[{item_name}] 花费 {spent} 超过预期 {expect_total}，异常")
        return False

    est_qty = round(spent / price) if price > 0 else 0
    if qty > 0 and est_qty / qty < 0.5:
        logger.warning(f"[{item_name}] 花费 {spent} 只够买 {est_qty}/{qty} 个")
    else:
        logger.info(f"[{item_name}] ✓ 花费 {spent}，估算购买 {est_qty}/{qty} 个")
    return True


# ---------- 主流程：单商品买入 ----------

def run_buy_flow(item_name: str) -> bool:
    """
    单商品买入流程：
    1. 读配置
    2. 首次进入商品，识别价格
    3. 启用杀价：循环刷新，直到到价或超限
    4. 决策：到价 / 可接受 → 买入
    5. 买入：前后余额对比 + 记录
    """
    # 1. 读配置
    threshold = get_buy_threshold(item_name)
    if threshold is None:
        logger.info(f"[{item_name}] 买入未启用")
        return False

    qty = get_buy_qty(item_name)
    bargain = get_bargain_config(item_name)
    layout = get_layout(item_name)

    logger.info(
        f"[{item_name}] 阈值={threshold} 数量={qty} 布局={layout} "
        f"杀价={'启用' if bargain else '未启用'}"
    )

    runner = get_runner()

    # ★ 内层入口：确保当前在收藏页（幂等）
    logger.info(f"[{item_name}] 确保在收藏页")
    runner.run_raw("回到收藏页", watch_nodes={})

    # 2. 首次进入
    price = _enter_and_read_price(runner, item_name, ENTRY_FIRST, layout)
    if price is None:
        logger.warning(f"[{item_name}] 首次未识别到价格")
        return False
    logger.info(f"[{item_name}] 首次价格: {price}")

    # 3. 未启用杀价
    if not bargain:
        if decide_buy(price, threshold):
            logger.info(f"[{item_name}] 到价 → 买入")
            return _do_buy(runner, item_name, price, qty, layout)
        logger.info(f"[{item_name}] 观望（{price} > {threshold}）")
        return False

    # 4. 杀价循环
    target = bargain["target_price"]
    accept = bargain.get("accept_price")
    max_refresh = bargain["max_refresh"]

    best_price = price
    if price <= target:
        logger.info(f"[{item_name}] 首刷已到价 {price} ≤ {target}")
        return _do_buy(runner, item_name, price, qty, layout)

    for i in range(max_refresh):
        logger.info(f"[{item_name}] 刷新 {i+1}/{max_refresh}")
        price = _enter_and_read_price(runner, item_name, ENTRY_REFRESH, layout)

        if price is None:
            logger.warning(f"[{item_name}] 第 {i+1} 次刷新未识别到价格")
            continue

        logger.info(f"[{item_name}] 第 {i+1} 次刷新价格: {price}")
        best_price = min(best_price, price)

        if best_price <= target:
            logger.info(f"[{item_name}] 到价 {best_price} ≤ {target}")
            return _do_buy(runner, item_name, best_price, qty, layout)

    # 5. 刷满
    logger.info(f"[{item_name}] 刷满 {max_refresh} 次，最低价 {best_price}")
    if accept and best_price <= accept:
        logger.info(f"[{item_name}] 兜底接受 {best_price} ≤ {accept}")
        return _do_buy(runner, item_name, best_price, qty, layout)

    logger.info(f"[{item_name}] 放弃（最低 {best_price}）")
    return False

# ---------- 多商品扫描 ----------

def run_scan_all(
    items: list[str] | None = None,
    list_name: str | None = None,
) -> dict:
    """
    按顺序扫描多个商品。

    参数优先级:
        1. list_name 非空 → 用预定义清单
        2. items 非空     → 用传入列表
        3. 都为空         → 用 scan.enabled_items

    返回 {商品名: 是否成功买入}。
    """
    from core.trade_config import (
        get_scan_items,
        get_scan_interval,
        get_item_scan_repeat,
        resolve_item_list,
    )

    # 1. 决定商品清单
    if list_name:
        items = resolve_item_list(list_name)
        logger.info(f"使用清单 '{list_name}': {items}")
    elif items is None:
        items = get_scan_items()

    interval = get_scan_interval()

    if not items:
        logger.warning("扫描清单为空")
        return {}

    runner = get_runner()
    results = {}

    try:
        # 外层入口：确保在主页面
        logger.info("确保在主页面")
        runner.run_raw("确保在主页面", watch_nodes={})

        # 中层入口：确保进入交易行收藏页
        logger.info("确保进入交易行收藏页")
        runner.run_raw("确保进入交易行收藏页", watch_nodes={})

        # 执行扫描
        logger.info(f"===== 扫描 {len(items)} 个商品 =====")
        for i, item in enumerate(items):
            logger.info(f"\n===== [{i+1}/{len(items)}] {item} =====")
            repeat = get_item_scan_repeat(item)
            item_ok = False

            for r in range(repeat):
                if repeat > 1:
                    logger.info(f"  --- 第 {r+1}/{repeat} 次 ---")
                try:
                    if run_buy_flow(item):
                        item_ok = True
                        break   # 买到即停
                except Exception as e:
                    logger.error(f"[{item}] 异常: {e}")

            results[item] = item_ok

            if i < len(items) - 1 and interval > 0:
                time.sleep(interval)

    finally:
        # 结尾归位：无论成功失败
        logger.info("任务结束，回主页")
        try:
            runner.run_raw("确保在主页面", watch_nodes={})
        except Exception as e:
            logger.warning(f"结尾归位失败: {e}")

    logger.info("\n===== 扫描结果 =====")
    for item, ok in results.items():
        logger.info(f"  {'✓' if ok else '✗'} {item}")

    return results