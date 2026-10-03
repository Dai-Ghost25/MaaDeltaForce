# core/trade_logic.py —— 纯逻辑，零外部依赖

def decide_buy(game_price: int, buy_below: int) -> bool:
    """买入判断：价格低于阈值就买"""
    return game_price <= buy_below


def decide_sell(game_price: int, sell_above: int) -> bool:
    """卖出判断：价格高于阈值就卖"""
    return game_price >= sell_above