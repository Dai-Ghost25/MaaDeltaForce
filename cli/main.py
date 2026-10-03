# cli/main.py
import argparse
import os
import sys
import logging

from core.config import get_config
from core.task_runner import get_runner

logger = logging.getLogger(__name__)


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


# ============================================================
#  任务与按次数循环
# ============================================================

def cmd_list(args):
    """列出所有启用的任务"""
    cfg = get_config()
    print(f"{'名称':<12} {'显示名':<12} {'入口':<20} 说明")
    print("-" * 70)
    for name in cfg.enabled_tasks():
        t = cfg.get_task(name)
        label = str(t.get("label", name))
        entry = str(t.get("entry", ""))
        desc = str(t.get("description", ""))
        print(f"{name:<12} {label:<12} {entry:<20} {desc}")
    return 0


def cmd_show(args):
    """显示单个任务的完整配置"""
    cfg = get_config()
    try:
        t = cfg.get_task(args.task)
    except KeyError as e:
        print(f"错误: {e}")
        return 1
    print(f"名称: {args.task}")
    for k, v in t.items():
        print(f"  {k}: {v}")
    return 0


def cmd_run(args):
    """执行任务（按次数循环）"""
    import time
    cfg = get_config()

    if args.all:
        task_names = cfg.enabled_tasks()
    elif args.task:
        task_names = args.task
    else:
        print("错误: 请指定任务名，或用 --all 执行全部")
        return 1

    for name in task_names:
        if name not in cfg.tasks:
            print(f"错误: 未知任务 '{name}'")
            print(f"可用任务: {', '.join(cfg.tasks.keys())}")
            return 1

    repeat = args.repeat if args.repeat is not None else 1
    interval = args.interval if args.interval is not None else 5.0
    runner = get_runner()

    total_runs = 0
    total_success = 0
    total_fail = 0

    try:
        round_num = 0
        while repeat == -1 or round_num < repeat:
            round_num += 1
            label = f"第 {round_num} 轮" + ("（无限）" if repeat == -1 else f"/{repeat}")
            print(f"\n{'='*40}")
            print(f"  {label}")
            print(f"{'='*40}")

            for name in task_names:
                try:
                    ok = runner.run_task(name)
                except Exception as e:
                    logger.error(f"[{name}] 异常: {e}")
                    ok = False

                total_runs += 1
                if ok:
                    total_success += 1
                else:
                    total_fail += 1

                print(f"  {'✓' if ok else '✗'} {name}")

            if (repeat == -1 or round_num < repeat) and interval and interval > 0:
                print(f"\n等待 {interval} 秒...")
                time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n⚠️ 收到 Ctrl+C，停止循环")
    finally:
        runner.flush_notifications()

    print(f"\n{'='*40}")
    print(f"  总计: {total_runs} 次执行，成功 {total_success}，失败 {total_fail}")
    print(f"{'='*40}")

    return 0 if total_fail == 0 else 1


# ============================================================
#  设备管理
# ============================================================

def cmd_device_list(args):
    from core.device_config import load_devices, get_active_device_name
    cfg = load_devices()
    devices = cfg.get("devices", {})
    if not devices:
        print("devices.yaml 里没有设备")
        return 0

    active = None
    try:
        active = get_active_device_name()
    except Exception:
        pass

    print(f"{'标记':<4} {'名称':<12} {'ADB 地址':<24} {'profile':<14} 说明")
    print("-" * 80)
    for name, d in devices.items():
        mark = "*" if name == active else " "
        print(
            f"{mark:<4} {name:<12} "
            f"{str(d.get('adb_address', '')):<24} "
            f"{str(d.get('profile', '')):<14} "
            f"{d.get('description', '')}"
        )
    print(f"\n* 表示当前设备。切换: python mdf.py -D <名称> ...")
    return 0


def cmd_device_show(args):
    from core.device_config import get_device, DeviceConfigError
    try:
        d = get_device(args.name)
    except DeviceConfigError as e:
        print(f"错误: {e}")
        return 1
    print(f"设备: {args.name}")
    for k, v in d.items():
        print(f"  {k}: {v}")
    return 0


def cmd_device_use(args):
    from core.device_config import set_default_device, DeviceConfigError
    try:
        set_default_device(args.name)
    except DeviceConfigError as e:
        print(f"错误: {e}")
        return 1
    print(f"已切换默认设备为: {args.name}")
    return 0


def cmd_device_add(args):
    from core.device_config import add_device, DeviceConfigError
    try:
        add_device(
            name=args.name,
            adb_address=args.adb,
            profile=args.profile or "",
            description=args.desc or "",
            resolution=args.resolution or "",
        )
    except DeviceConfigError as e:
        print(f"错误: {e}")
        return 1
    print(f"已添加设备: {args.name} ({args.adb})")
    return 0


def cmd_device_remove(args):
    from core.device_config import remove_device, DeviceConfigError
    try:
        remove_device(args.name)
    except DeviceConfigError as e:
        print(f"错误: {e}")
        return 1
    print(f"已删除设备: {args.name}")
    return 0


# ============================================================
#  调度器 / 交易（保持现有实现）
# ============================================================

def cmd_schedule_list(args):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    for s in mgr.schedules:
        run = mgr.run_counts.get(s["name"], 0)
        max_runs = s.get("max_runs", "-")
        print(f"{s['name']:<15} enabled={s.get('enabled', True)} 运行={run}/{max_runs}")
    return 0


def cmd_schedule_run(args):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr._run_schedule(args.name)
    return 0


def cmd_schedule_daemon(args):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr.run_forever()
    return 0


def cmd_trade_scan(args):
    from core.trade_flow import run_scan_all
    results = run_scan_all(
        items=args.items if args.items else None,
        list_name=args.list_name,
    )
    print("\n===== 扫描结果 =====")
    for item, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {item}")
    return 0 if results and all(results.values()) else 1


def cmd_trade_log(args):
    from core.purchase_log import format_summary
    print(format_summary())
    return 0


# ============================================================
#  参数定义
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mdf",
        description="MaaDeltaForce 任务调度工具",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "-D", "--device", default=None, metavar="名称",
        help="指定设备（覆盖环境变量 MDF_DEVICE 和 devices.yaml 的 default）"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # ---- list / show ----
    sub.add_parser("list", help="列出所有启用的任务").set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="显示单个任务详情")
    p_show.add_argument("task", help="任务名")
    p_show.set_defaults(func=cmd_show)

    # ---- run ----
    p_run = sub.add_parser("run", help="执行任务")
    p_run.add_argument("task", nargs="*", help="任务名（可多个）")
    p_run.add_argument("--all", action="store_true", help="执行全部启用的任务")
    p_run.add_argument(
        "-r", "--repeat", type=int, default=1,
        help="重复次数，-1=无限循环，默认 1"
    )
    p_run.add_argument(
        "--interval", type=float, default=5.0,
        help="每次循环之间的等待秒数，默认 5"
    )
    p_run.set_defaults(func=cmd_run)

    # ---- device ----
    p_dev = sub.add_parser("device", help="设备管理")
    dev_sub = p_dev.add_subparsers(dest="device_cmd", required=True)

    dev_sub.add_parser("list", help="列出所有设备").set_defaults(func=cmd_device_list)

    p_dshow = dev_sub.add_parser("show", help="显示设备详情")
    p_dshow.add_argument("name", help="设备名")
    p_dshow.set_defaults(func=cmd_device_show)

    p_duse = dev_sub.add_parser("use", help="设置默认设备")
    p_duse.add_argument("name", help="设备名")
    p_duse.set_defaults(func=cmd_device_use)

    p_dadd = dev_sub.add_parser("add", help="添加新设备")
    p_dadd.add_argument("name", help="设备名")
    p_dadd.add_argument("--adb", required=True, help="ADB 地址，例如 192.168.1.170:5555")
    p_dadd.add_argument("--profile", default=None, help="布局 profile 名（预留）")
    p_dadd.add_argument("--desc", default=None, help="备注")
    p_dadd.add_argument("--resolution", default=None, help="分辨率备注，例如 1080x2340")
    p_dadd.set_defaults(func=cmd_device_add)

    p_drm = dev_sub.add_parser("remove", help="删除设备")
    p_drm.add_argument("name", help="设备名")
    p_drm.set_defaults(func=cmd_device_remove)

    # ---- schedule ----
    p_sched = sub.add_parser("schedule", help="定时任务管理")
    sched_sub = p_sched.add_subparsers(dest="sched_cmd", required=True)
    sched_sub.add_parser("list").set_defaults(func=cmd_schedule_list)

    p_sr = sched_sub.add_parser("run", help="立即触发一次")
    p_sr.add_argument("name")
    p_sr.set_defaults(func=cmd_schedule_run)

    sched_sub.add_parser("daemon").set_defaults(func=cmd_schedule_daemon)

    # ---- trade ----
    p_trade = sub.add_parser("trade", help="交易相关")
    trade_sub = p_trade.add_subparsers(dest="trade_cmd", required=True)

    p_scan = trade_sub.add_parser("scan", help="扫描并购买商品")
    p_scan.add_argument("items", nargs="*", help="指定商品（留空则用配置）")
    p_scan.add_argument("--list", dest="list_name", help="使用预定义清单")
    p_scan.set_defaults(func=cmd_trade_scan)

    trade_sub.add_parser("log", help="查看购买记录").set_defaults(func=cmd_trade_log)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    # ★ 全局 -D 通过环境变量传递给 get_runner（最简方案）
    if args.device:
        os.environ["MDF_DEVICE"] = args.device

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()