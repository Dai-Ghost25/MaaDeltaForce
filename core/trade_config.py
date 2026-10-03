# core/trade_config.py
import logging
import yaml
from pathlib import Path

from core.config import TRADE_FILE

logger = logging.getLogger(__name__)

TRADE_FILE = PROJECT_ROOT / "trade_config.yaml"


# ---------- 底层 ----------

def load_trade_config() -> dict:
    if not TRADE_FILE.is_file():
        raise FileNotFoundError(f"未找到 {TRADE_FILE}")
    with open(TRADE_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------- 商品 ----------

def get_item_config(item_name: str) -> dict:
    cfg = load_trade_config()
    items = cfg.get("items", {})
    if item_name not in items:
        raise KeyError(f"未配置商品: {item_name}")
    return items[item_name]


def get_buy_threshold(item_name: str) -> int | None:
    item = get_item_config(item_name)
    buy = item.get("buy", {})
    if not buy.get("enabled", False):
        return None
    return buy.get("below")

def get_buy_qty(item_name: str) -> int:
    item = get_item_config(item_name)
    buy = item.get("buy", {})
    return buy.get("qty", 1)


def get_sell_threshold(item_name: str) -> int | None:
    item = get_item_config(item_name)
    sell = item.get("sell", {})
    if not sell.get("enabled", False):
        return None
    return sell.get("above")


def get_bargain_config(item_name: str) -> dict:
    """
    返回杀价配置。未启用返回 {}。
    返回值包含:
        target_price: 到价即买
        accept_price: 刷满后仍低于此值也买（可选）
        max_refresh: 最多刷几次
        refresh_action: 刷新的 Pipeline 入口
        enter_action: 首次进入的 Pipeline 入口
    """
    item = get_item_config(item_name)
    bargain = item.get("bargain", {})
    if not bargain.get("enabled", False):
        return {}
    return {
        "target_price": bargain.get("target_price"),
        "accept_price": bargain.get("accept_price"),
        "max_refresh": bargain.get("max_refresh", 0),
        "refresh_action": bargain.get("refresh_action", "返回商品收藏页面"),
        "enter_action": bargain.get("enter_action", "进入商品"),
    }

def get_item_scan_repeat(item_name: str) -> int:
    """商品级扫描次数，默认 1"""
    item = get_item_config(item_name)
    return item.get("scan_repeat", 1)

def get_item_id(item_name: str) -> int | None:
    return get_item_config(item_name).get("item_id")


# ---------- 扫描 ----------

def get_scan_items() -> list[str]:
    return list(load_trade_config().get("scan", {}).get("enabled_items", []))

def resolve_item_list(list_name: str) -> list[str]:
    """根据清单名从配置里取出商品列表"""
    cfg = load_trade_config()
    lists = cfg.get("item_lists", {})
    if list_name not in lists:
        raise KeyError(f"未定义的清单: {list_name}，可用: {list(lists.keys())}")
    return list(lists[list_name])

def list_item_lists() -> list[str]:
    """返回所有已定义的清单名"""
    cfg = load_trade_config()
    return list(cfg.get("item_lists", {}).keys())

def get_scan_interval() -> float:
    return load_trade_config().get("scan", {}).get("interval_seconds", 2.0)


# ---------- 校验 ----------

def validate() -> list[str]:
    errors = []
    try:
        cfg = load_trade_config()
    except Exception as e:
        return [f"配置文件读取失败: {e}"]

    items = cfg.get("items", {})
    if not items:
        errors.append("items 为空")

    for name, item in items.items():
        buy = item.get("buy", {})
        if buy.get("enabled") and "below" not in buy:
            errors.append(f"[{name}] buy 已启用但缺少 below")

        bargain = item.get("bargain", {})
        if bargain.get("enabled"):
            if "target_price" not in bargain:
                errors.append(f"[{name}] bargain 已启用但缺少 target_price")
            if "max_refresh" not in bargain:
                errors.append(f"[{name}] bargain 已启用但缺少 max_refresh")
            if "refresh_action" not in bargain:
                errors.append(f"[{name}] bargain 已启用但缺少 refresh_action")

    for name in cfg.get("scan", {}).get("enabled_items", []):
        if name not in items:
            errors.append(f"scan 中的 '{name}' 未在 items 定义")

    return errors

def get_layout(item_name: str) -> str:
    item = get_item_config(item_name)
    layout = item.get("layout")
    if layout not in ("有兑换", "无兑换"):
        raise ValueError(f"[{item_name}] layout 未配置或值无效: {layout}")
    return layout