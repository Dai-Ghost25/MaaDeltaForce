# test.py
import sys
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from core.task_runner import get_runner
from core.parsing import parse_price, parse_price_safe


# ---------- 测试注册表 ----------

TESTS = {}


def register(name, description):
    def decorator(func):
        TESTS[name] = {"func": func, "desc": description}
        return func
    return decorator


# ---------- 测试：余额识别 ----------

@register("balance", "识别余额并解析")
def test_balance():
    runner = get_runner()
    captured = runner.run_raw("识别余额", watch_nodes={"识别余额": "text"})

    if not captured:
        print("❌ 未识别到余额")
        return False

    for r in captured:
        print(f"原文: '{r['text']}'")
        balance = parse_price(r["text"])
        if balance is None:
            print(f"❌ 解析失败")
            return False
        print(f"✓ 解析: {balance}")
    return True


# ---------- 测试：价格识别 ----------

@register("price", "识别交易行价格并解析")
def test_price(item_name="原木木板"):
    runner = get_runner()
    captured = runner.run_raw(
        "交易行",
        pipeline_override={"识别商品坐标": {"expected": item_name}},
        watch_nodes={"识别价格": "text"},
    )

    if not captured:
        print("❌ 未识别到价格")
        return False

    for r in captured:
        print(f"原文: '{r['text']}'")
        price = parse_price(r["text"])
        if price is None:
            print(f"❌ 解析失败")
            return False
        print(f"✓ 解析: {price}")
    return True


# ---------- 测试：纯逻辑（不连设备） ----------

@register("logic", "纯逻辑：parse_price / decide_buy（不连设备）")
def test_logic():
    from core.trade_logic import decide_buy

    cases = [
        ("1,791", 1791),
        ("平均单价：1,791", 1791),
        ("5，200", 5200),
        ("1,79l", 1791),
    ]
    ok = True
    for text, expected in cases:
        got = parse_price(text)
        status = "✓" if got == expected else "✗"
        print(f"{status} '{text}' → {got} (期望 {expected})")
        if got != expected:
            ok = False

    print()
    buy_cases = [(4500, True), (5000, True), (5001, False)]
    for price, expected in buy_cases:
        got = decide_buy(price, 5000)
        status = "✓" if got == expected else "✗"
        print(f"{status} {price} ≤ 5000 → {got} (期望 {expected})")
        if got != expected:
            ok = False
    return ok

#

@register("purchase_log", "购买记录写入与读取（不连设备）")
def test_purchase_log():
    from core.purchase_log import log_purchase, load_all, format_summary

    # 写入一条测试记录
    record = log_purchase(
        item="测试商品",
        expect_price=1000,
        expect_qty=10,
        balance_before=50000,
        balance_after=40000,
        note="test",
    )
    print(f"写入: {record}")

    # 读回来
    records = load_all()
    print(f"当前共 {len(records)} 条记录")
    assert len(records) > 0
    assert records[-1]["item"] == "测试商品"
    assert records[-1]["spent"] == 10000

    # 汇总
    print()
    print(format_summary())
    return True

@register("trade_config", "交易配置读取与校验")
def test_trade_config():
    from core.trade_config import (
        get_scan_items, get_buy_threshold, get_bargain_config, validate,
    )

    errors = validate()
    if errors:
        print("❌ 配置有错:")
        for e in errors:
            print(f"  - {e}")
        return False
    print("✓ 配置校验通过\n")

    for item in get_scan_items():
        threshold = get_buy_threshold(item)
        bargain = get_bargain_config(item)
        print(f"[{item}]")
        print(f"  买入阈值: {threshold}")
        if bargain:
            print(f"  杀价: target={bargain['target_price']} "
                  f"accept={bargain.get('accept_price')} "
                  f"max_refresh={bargain['max_refresh']} "
                  f"refresh_action={bargain['refresh_action']}")
        else:
            print(f"  杀价: 未启用")
    return True

@register("buy_flow", "端到端买入（会真花钱，注意）")
def test_buy_flow():
    from core.trade_flow import run_buy_flow

    item = "原木木板"     # ← 改这里
    print(f"测试商品: {item}")
    print("⚠️ 这会真的点击买入按钮，请确认游戏内设置正确")
    input("按回车继续，Ctrl+C 取消...")

    ok = run_buy_flow(item)
    print(f"\n{'✓ 买入流程完成' if ok else '✗ 未买入'}")
    return ok


# ---------- 调度器 ----------

def list_tests():
    print("可用测试:")
    for name, info in TESTS.items():
        print(f"  {name:<12} {info['desc']}")
    print("\n用法: python test.py <name> | all")


def run_one(name):
    if name not in TESTS:
        print(f"未知测试: {name}")
        return False
    print(f"\n===== 测试: {name} =====")
    try:
        ok = TESTS[name]["func"]()
        print(f"\n{'✅ 通过' if ok else '❌ 失败'}")
        return ok
    except Exception as e:
        print(f"\n💥 异常: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    args = sys.argv[1:]
    if not args:
        list_tests()
        return

    if args[0] == "all":
        results = {name: run_one(name) for name in TESTS}
        print("\n" + "=" * 40)
        for name, ok in results.items():
            print(f"  {'✅' if ok else '❌'} {name}")
        sys.exit(0 if all(results.values()) else 1)

    ok = run_one(args[0])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()