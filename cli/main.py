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
#  时间盒：一次启动 → 多次 body → 一次关闭
# ============================================================

def tc():
    from core import session_config
    return session_config


def tc_finalize(cfg):
    """把已合并的配置 dict 转成 Session（含 --wait-until 的顺延处理）。"""
    from core.session import Clock, Session, parse_hhmm
    cfg = dict(cfg)

    until = cfg.get("until")
    if cfg.get("wait_until") and until is not None and not isinstance(until, Clock):
        # 时刻已过则顺延到明天（例如现在 20:00，--until 07:30 --wait-until）
        until = Clock(parse_hhmm(until, "截止时刻"), wait_passed=True)
    elif isinstance(until, str):
        until = Clock(parse_hhmm(until, "截止时刻"))
    cfg["until"] = until

    max_wait = cfg.get("max_wait")
    if max_wait in ("", None):
        cfg["max_wait"] = None

    cfg.setdefault("tasks", [])
    return Session(
        name=cfg.get("name", "session"),
        startup=list(cfg.get("startup") or []),
        body=list(cfg.get("body") or cfg.get("tasks") or []),
        shutdown=list(cfg.get("shutdown") or []),
        tasks=list(cfg.get("tasks") or []),
        repeat=cfg.get("repeat"),
        body_repeat=cfg.get("body_repeat"),
        duration=cfg.get("duration"),
        until=cfg.get("until"),
        window=cfg.get("active"),
        quiet=cfg.get("quiet"),
        wait_quiet=bool(cfg.get("wait_quiet", False)),
        force_cut=bool(cfg.get("force_cut", False)),
        interval=float(cfg.get("interval") or 0),
        max_wait=cfg.get("max_wait"),
        notify=cfg.get("notify"),
        keep_open=bool(cfg.get("keep_open", False)),
        skip_if_missed=bool(cfg.get("skip_if_missed", False)),
        skip_if_until_passed=bool(cfg.get("skip_if_until_passed", False)),
    )


def _build_session(name, overrides, profile):
    """合并 defaults / profile / overrides → 配置 dict"""
    from core.session_config import merge, get_defaults

    try:
        defaults = get_defaults()
    except Exception as e:
        print(f"警告: 读取 session.yaml defaults 失败: {e}")
        defaults = {}

    cfg = merge(profile=profile, overrides=overrides, defaults=defaults)
    cfg["name"] = name or cfg.get("name") or "session"
    return cfg


def _session_overrides(args) -> dict:
    """
    把命令行参数转成时间盒覆盖项（None = 未指定，不覆盖档案）。

    -r 在所有命令里都表示「body 跑几次」，覆盖档案的 body_repeat。
    """
    return {
        "body_repeat": getattr(args, "repeat", None),
        "duration": getattr(args, "duration", None),
        "until": getattr(args, "until", None),
        "wait_until": True if getattr(args, "wait_until", False) else None,
        "active": getattr(args, "active", None),
        "quiet": getattr(args, "quiet", None),
        "wait_quiet": True if getattr(args, "wait_quiet", False) else None,
        "force_cut": True if getattr(args, "force_cut", False) else None,
        "interval": getattr(args, "interval", None),
        "max_wait": getattr(args, "max_wait", None),
        "notify": getattr(args, "notify", None),
        "keep_open": True if getattr(args, "keep_open", False) else None,
        "skip_if_missed": True if getattr(args, "skip_if_missed", False) else None,
    }


def _validate_tasks(box, cfg) -> int:
    unknown = [t for t in box.all_tasks if t not in cfg.tasks]
    if unknown:
        print(f"错误: 未知任务 {', '.join(unknown)}")
        print(f"可用任务: {', '.join(cfg.tasks.keys())}")
        return 1
    if not box.all_tasks:
        print("错误: 时间盒里没有任务（用 --all、指定任务名，或在档案里写 body）")
        return 1
    return 0


def _print_box(box, title="时间盒"):
    print(f"{title}: {box.name}")
    for line in box.describe_lines():
        print(f"  {line}")


def _confirm_if_unlimited(box) -> bool:
    """完全没有上限时提醒一下（避免以为在跑 8 小时，结果跑到天亮）。"""
    if not box.infinite:
        return True
    if not (sys.stdin and sys.stdin.isatty()):
        return True
    print("\n⚠️ 这个时间盒没有轮数上限，也没有时间上限，会一直跑到 Ctrl+C。")
    if box.all_tasks:
        print(f"   任务: {' → '.join(box.all_tasks)}")
    try:
        answer = input("   确认继续？[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes", "是"):
        print("已取消。提示: 加 -d 8h / -u 12:00 / -r 3 之类的上限。")
        return False
    return True


def _session_loop(box, notify=None):
    from core.session import run_session

    if not _confirm_if_unlimited(box):
        return 1

    runner = get_runner()
    result = run_session(runner, box, notify=notify)

    if result.stop_reason == "error":
        return 2
    if result.interrupted:
        return 130
    return 0 if result.ok else 1


# ---------- session 子命令 ----------

def cmd_session_list(args):
    tcc = tc()
    try:
        profiles = tcc.load_profiles()
    except tcc.SessionConfigError as e:
        print(f"错误: {e}")
        return 1
    if not profiles:
        print(f"session.yaml 里还没有时间盒档案（文件: {tcc.TIMEBOX_FILE}）")
        return 0

    cfg = get_config()
    defaults = tcc.get_defaults()
    errors = tcc.validate_all(cfg.tasks.keys())
    if errors:
        print("⚠️ session.yaml 校验发现问题:")
        for e in errors:
            print(f"  - {e}")
        print()

    print(f"{'名称':<20} {'body':<7} {'关闭':<7} {'时间约束':<26} 任务")
    print("-" * 108)
    for p in profiles:
        merged = tcc.merge(profile=p, defaults=defaults)
        body_n = merged.get("body_repeat")
        body_txt = "不限" if body_n in (None, -1, 0) else str(body_n)
        close = "保持" if merged.get("keep_open") else ("关闭" if merged.get("shutdown") else "-")
        limits = []
        if merged.get("duration") is not None:
            limits.append(f"时长{merged['duration']}")
        if merged.get("until") is not None:
            limits.append(f"到{merged['until']}")
        if merged.get("active") is not None:
            limits.append(f"窗口{merged['active']}")
        if merged.get("quiet") is not None:
            limits.append(f"安静{merged['quiet']}")
        body_tasks = merged.get("body") or merged.get("tasks") or []
        print(
            f"{merged.get('name', ''):<20} {body_txt:<7} {close:<7} "
            f"{('、'.join(limits) or '无'):<26} {' → '.join(body_tasks)}"
        )
    print(f"\n共 {len(profiles)} 个档案。运行: python mdf.py session run <名称>")

    return 0


def cmd_session_show(args):
    tcc = tc()
    try:
        profile = tcc.get_profile(args.name)
    except KeyError:
        print(f"错误: 未找到时间盒档案: {args.name}")
        print(f"可用档案: {', '.join(tcc.list_names()) or '（无）'}")
        return 1
    except tcc.SessionConfigError as e:
        print(f"错误: {e}")
        return 1

    errors = tcc.validate(profile, get_config().tasks.keys())
    print(f"档案: {args.name}")
    for line in tcc.describe(profile):
        print(f"  {line}")

    print("\n实际生效的约束:")
    try:
        box = tc_finalize(dict(profile, name=args.name))
        for line in box.describe_lines():
            print(f"  {line}")
        for w in tcc.warnings(profile):
            print(f"  ⚠️ {w}")
    except Exception as e:
        print(f"  ⚠️ 构造失败: {e}")

    if errors:
        print("\n⚠️ 校验发现问题:")
        for e in errors:
            print(f"  - {e}")
    return 0 if not errors else 1


def _load_profile_or_tasks(args, cfg, tcc):
    """
    解析位置参数：
      session run <档案名> [更多任务] ...
      session run <任务名> [任务名...] -d 8h
    返回 (name, profile, task_names, 错误信息)
    """
    profile = {}
    positional = [t for t in [args.name] + list(args.task or []) if t]

    if args.name:
        try:
            profile = tcc.get_profile(args.name)
            return args.name, profile, list(args.task or []), None
        except KeyError:
            if args.task:
                return "临时时间盒", {}, positional, None
            return None, {}, None, (
                f"错误: 未找到时间盒档案: {args.name}\n"
                f"可用档案: {', '.join(tcc.list_names()) or '（无）'}\n"
                "（想临时跑任务可以写: python mdf.py session run <任务名> [更多任务] -d 8h）"
            )
        except tcc.SessionConfigError as e:
            return None, {}, None, f"错误: {e}"

    if not positional and not args.all:
        args.all = True
    return "临时时间盒", {}, positional, None


def cmd_session_run(args):
    tcc = tc()
    cfg = get_config()

    name, profile, task_names, err = _load_profile_or_tasks(args, cfg, tcc)
    if err:
        print(err)
        return 1

    override = _session_overrides(args)
    if not profile:
        # 临时时间盒：位置参数就是 body
        if args.all:
            override["body"] = cfg.enabled_tasks()
        elif task_names:
            override["body"] = task_names
    elif task_names:
        override["body"] = task_names

    cfg_dict = _build_session(name, override, profile)

    errors = tcc.validate(cfg_dict, cfg.tasks.keys())
    if errors:
        print("错误: 时间盒配置有问题:")
        for e in errors:
            print(f"  - {e}")
        return 1

    try:
        box = tc_finalize(cfg_dict)
    except Exception as e:
        print(f"错误: {e}")
        return 1

    rc = _validate_tasks(box, cfg)
    if rc:
        return rc

    if args.dry_run:
        _print_box(box)
        end = box.deadline
        if end:
            print(f"\n预计结束: {end:%Y-%m-%d %H:%M:%S}")
        elif box.body_times() not in (None, -1):
            print(f"\n没有时间上限，跑满 {box.body_times()} 次 body 后结束")
        else:
            print("\n没有时间上限（只能靠轮数或 Ctrl+C 结束）")
        check = box.check(box.started_at, done_body=0, done_rounds=0)
        if check.not_ready:
            wait = check.wait_seconds or 0
            print(f"当前状态: {check.reason_label}，还需等待 {wait:.0f} 秒")
        elif check.finished:
            tail = f"（{check.detail}）" if check.detail else ""
            print(f"当前状态: 会立即结束：{check.reason_label}{tail}")
        else:
            print("当前状态: 可以立即开始")
        for w in tcc.warnings(cfg_dict):
            print(f"⚠️ {w}")
        print("\n（--dry-run：没有连接设备，也没有执行任何任务）")
        return 0

    return _session_loop(box, notify=box.notify)


# ---------- 定时任务 ----------

def cmd_schedule_list(args):
    from core.scheduler import ScheduleManager
    mgr = ScheduleManager()
    for s in mgr.schedules:
        run = mgr.run_counts.get(s["name"], 0)
        max_runs = s.get("max_runs", "-")
        mode = "时间盒" if (s.get("startup") or s.get("body") or s.get("shutdown")) else "任务列表"
        print(
            f"{s['name']:<16} enabled={str(s.get('enabled', True)):<5} "
            f"运行={run}/{max_runs}  {mode}"
        )
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


# ---------- 交易 ----------

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

# def _add_time_args(p, include_interval=True, default_interval=None):
#     """时间控制参数（run / session run / chain run 共用）。"""
#     p.add_argument(
#         "-d", "--duration", metavar="时长", default=None,
#         help="总运行时长，例如 8h / 30m / 1h30m（到点跑完当前一次再退出）"
#     )
#     p.add_argument(
#         "-u", "--until", metavar="时刻", default=None,
#         help="绝对截止时刻，例如 12:00（今天已过就不再继续）"
#     )
#     p.add_argument(
#         "--wait-until", action="store_true",
#         help="配合 --until：该时刻今天已过时顺延到明天（从头天晚上跑到次日 7:30 用这个）"
#     )
#     p.add_argument(
#         "-a", "--active", metavar="起~止", default=None,
#         help="允许运行的时间窗，例如 02:00~08:00；跨午夜写 23:00~07:30（未到则等待）"
#     )
#     p.add_argument(
#         "--quiet", metavar="起~止", default=None,
#         help="安静时段，到点不再开始新的一次，例如 22:00~24:00"
#     )
#     p.add_argument(
#         "--wait-quiet", action="store_true",
#         help="命中安静时段时等到时段结束再继续（默认直接收尾）"
#     )
#     p.add_argument(
#         "--force-cut", action="store_true",
#         help="到达时间上限时立即中断当前任务（默认跑完当前一次再退出）"
#     )
#     p.add_argument(
#         "--skip-if-missed", action="store_true",
#         help="时间窗没到就跳过（默认等到窗口打开）"
#     )
#     p.add_argument(
#         "--keep-open", action="store_true",
#         help="结束时不停游戏，留给下一个会话（默认按 shutdown 关闭）"
#     )
#     p.add_argument(
#         "--notify", choices=["none", "simple", "report"], default=None,
#         help="覆盖任务自身的钉钉通知设置，默认用任务里的配置"
#     )
#     p.add_argument(
#         "--max-wait", type=float, default=None, metavar="秒",
#         help="等待下一次时间窗打开的上限秒数，超过就结束（默认不限）"
#     )
#     if include_interval:
#         p.add_argument(
#             "--interval", type=float, default=default_interval, metavar="秒",
#             help="两次 body 之间的等待秒数，默认用档案里的配置"
#         )


def _add_body_repeat_arg(p):
    p.add_argument(
        "-r", "--repeat", type=int, default=None, metavar="次数",
        help="body 跑的次数，-1 或 0 = 不限；默认用档案里的配置"
    )



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

    # ---- session ----
    p_sess = sub.add_parser(
        "session",
        help="会话：一次启动 → 多次 body → 一次关闭",
    )
    sess_sub = p_sess.add_subparsers(dest="session_cmd", required=True)

    sess_sub.add_parser("list", help="列出 session.yaml 里的档案").set_defaults(
        func=cmd_session_list
    )

    p_sess_show = sess_sub.add_parser("show", help="显示某个档案的完整约束")
    p_sess_show.add_argument("name", help="档案名")
    p_sess_show.set_defaults(func=cmd_session_show)

    p_sess_run = sess_sub.add_parser("run", help="运行会话（可省略档案名）")
    p_sess_run.add_argument("name", nargs="?", help="session.yaml 里的档案名（可省略）")
    p_sess_run.add_argument("task", nargs="*", help="任务名（覆盖档案里的 body）")
    p_sess_run.add_argument("--all", action="store_true", help="用全部启用的任务作为 body")
    p_sess_run.add_argument("--dry-run", action="store_true", help="只试算，不连设备")
    _add_body_repeat_arg(p_sess_run)
    p_sess_run.set_defaults(func=cmd_session_run)

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