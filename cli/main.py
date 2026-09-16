# cli/main.py
import argparse
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


# ---------- 各子命令 ----------

def cmd_list(args):
    """列出所有启用的任务"""
    cfg = get_config()
    print(f"{'名称':<12} {'显示名':<12} {'入口':<20} 说明")
    print("-" * 70)
    for name in cfg.enabled_tasks():
        t = cfg.get_task(name)
        print(f"{name:<12} {t['label']:<12} {t['entry']:<20} {t['description']}")
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
    """执行任务"""
    cfg = get_config()

    # 收集要执行的任务名
    if args.all:
        task_names = cfg.enabled_tasks()
    elif args.task:
        task_names = args.task
    else:
        print("错误: 请指定任务名，或用 --all 执行全部")
        return 1

    # 校验任务名
    for name in task_names:
        if name not in cfg.tasks:
            print(f"错误: 未知任务 '{name}'")
            print(f"可用任务: {', '.join(cfg.tasks.keys())}")
            return 1

    runner = get_runner()
    results = {}
    try:
        for name in task_names:
            ok = runner.run_task(name)
            results[name] = ok
    finally:
        runner.flush_notifications()

    # 汇总
    print()
    print("=" * 40)
    failed = [n for n, ok in results.items() if not ok]
    for name, ok in results.items():
        print(f"  {'✓' if ok else '✗'} {name}")
    print("=" * 40)

    return 1 if failed else 0

def cmd_schedule_list(args):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    for s in mgr.schedules:
        run = mgr.run_counts.get(s["name"], 0)
        max_runs = s.get("max_runs", "-")
        print(f"{s['name']:<15} enabled={s.get('enabled', True)} 运行={run}/{max_runs}")
    return 0

def cmd_schedule_run(args):
    """手动触发一次定时任务（用于调试）"""
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr._run_schedule(args.name)
    return 0

def cmd_schedule_daemon(args):
    """前台运行调度器"""
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    mgr.run_forever()
    return 0


# ---------- 入口 ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maa",
        description="MaaFramework 任务调度工具",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="列出所有启用的任务")
    p_list.set_defaults(func=cmd_list)

    # show
    p_show = sub.add_parser("show", help="显示单个任务详情")
    p_show.add_argument("task", help="任务名")
    p_show.set_defaults(func=cmd_show)

    # run
    p_run = sub.add_parser("run", help="执行任务")
    p_run.add_argument("task", nargs="*", help="任务名（可多个）")
    p_run.add_argument("--all", action="store_true", help="执行全部启用的任务")
    p_run.set_defaults(func=cmd_run)


    p_sched = sub.add_parser("schedule", help="定时任务管理")
    sched_sub = p_sched.add_subparsers(dest="sched_cmd", required=True)

    sched_sub.add_parser("list").set_defaults(func=cmd_schedule_list)

    p_sr = sched_sub.add_parser("run", help="立即触发一次")
    p_sr.add_argument("name")
    p_sr.set_defaults(func=cmd_schedule_run)

    sched_sub.add_parser("daemon").set_defaults(func=cmd_schedule_daemon)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()