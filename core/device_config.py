# core/device_config.py
import os
import yaml
import logging

from core.config import DEVICES_FILE

logger = logging.getLogger(__name__)


class DeviceConfigError(Exception):
    pass


# ---------- 底层读写 ----------

def load_devices() -> dict:
    if not DEVICES_FILE.is_file():
        raise DeviceConfigError(f"未找到设备配置: {DEVICES_FILE}")
    with open(DEVICES_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_devices(cfg: dict):
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


# ---------- 查询 ----------

def list_devices() -> list[str]:
    return list(load_devices().get("devices", {}).keys())


def get_device(name: str) -> dict:
    cfg = load_devices()
    devices = cfg.get("devices", {})
    if name not in devices:
        raise DeviceConfigError(
            f"未知设备: {name}，可用: {list(devices.keys()) or '（无）'}"
        )
    return devices[name]


def get_default_device_name() -> str:
    cfg = load_devices()
    default = cfg.get("default")
    if not default:
        raise DeviceConfigError("devices.yaml 里没有配置 default")
    if default not in cfg.get("devices", {}):
        raise DeviceConfigError(f"default 指向的设备不存在: {default}")
    return default


def get_active_device_name(cli_arg: str | None = None) -> str:
    """
    优先级: CLI -D > 环境变量 MDF_DEVICE > devices.yaml 的 default
    """
    if cli_arg:
        return cli_arg
    env = os.environ.get("MDF_DEVICE")
    if env:
        return env
    return get_default_device_name()


# ---------- 修改 ----------

def set_default_device(name: str):
    cfg = load_devices()
    if name not in cfg.get("devices", {}):
        raise DeviceConfigError(f"未知设备: {name}")
    cfg["default"] = name
    save_devices(cfg)


def add_device(
    name: str,
    adb_address: str,
    profile: str = "",
    description: str = "",
    resolution: str = "",
):
    cfg = load_devices()
    devices = cfg.setdefault("devices", {})
    if name in devices:
        raise DeviceConfigError(f"设备已存在: {name}")
    devices[name] = {
        "adb_address": adb_address,
        "profile": profile,
        "description": description,
        "resolution": resolution,
    }
    save_devices(cfg)


def remove_device(name: str):
    cfg = load_devices()
    devices = cfg.get("devices", {})
    if name not in devices:
        raise DeviceConfigError(f"未知设备: {name}")
    if cfg.get("default") == name:
        raise DeviceConfigError(f"不能删除默认设备 {name}，先切换 default")
    del devices[name]
    save_devices(cfg)