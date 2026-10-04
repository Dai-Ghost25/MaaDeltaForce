import re
import logging

logger = logging.getLogger(__name__)


def parse_price(text: str) -> int | None:
    if not text:
        return None

    # 1. 全角转半角
    text = text.replace("，", ",").replace("：", ":").replace("　", " ")

    # 2. OCR 误识修正（全局替换）
    text = text.replace("l", "1").replace("I", "1")
    text = text.replace("O", "0").replace("o", "0")

    # 3. 只保留数字和逗号
    cleaned = re.sub(r"[^\d,]", "", text)

    # 4. 去掉千分位逗号
    cleaned = cleaned.replace(",", "")

    if not cleaned:
        logger.warning(f"[parse_price] 无法从 '{text}' 提取数字")
        return None

    try:
        return int(cleaned)
    except ValueError:
        logger.warning(f"[parse_price] 转换失败: '{cleaned}'")
        return None

def parse_price_safe(text: str, min_price: int = 1, max_price: int = 1_000_000) -> int | None:
    """带合理范围校验的解析"""
    price = parse_price(text)
    if price is None:
        return None
    if not (min_price <= price <= max_price):
        logger.warning(f"[parse_price] 价格 {price} 超出合理范围 [{min_price}, {max_price}]")
        return None
    return price