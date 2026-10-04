# core/config.py
import os
import sys
from pathlib import Path


# ---------- 路径推导（不依赖 cwd，从文件位置反推） ----------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
SETTINGS_FILE = PROJECT_ROOT / "settings.txt"
RESOURCE_DIR = PROJECT_ROOT / "assets" / "resource"
PIPELINE_DIR = RESOURCE_DIR / "pipeline"
LOG_DIR = PROJECT_ROOT / "logs"
DEPS_BIN_DIR = PROJECT_ROOT / "deps" / "bin"
DEBUG_DIR = PROJECT_ROOT / "debug" 

# 各配置文件统一从 CONFIG_DIR 拼
SETTINGS_FILE   = CONFIG_DIR / "settings.txt"
DEVICES_FILE    = CONFIG_DIR / "devices.yaml"
TRADE_FILE      = CONFIG_DIR / "trade_config.yaml"
SCHEDULES_FILE  = CONFIG_DIR / "schedules.yaml"
MAA_OPTION_FILE = CONFIG_DIR / "maa_option.json"

# 目录创建
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# ---------- MaaFramework 全局初始化 ----------

def _init_maa_option():
    """
    初始化 Maa 的全局选项：
      1. 保证 config/maa_option.json 存在
      2. 把工作目录设成 PROJECT_ROOT
      3. 让 debug/ 生成在 PROJECT_ROOT/debug
    """
    import json

    # 首次运行时创建默认选项
    if not MAA_OPTION_FILE.is_file():
        default = {
            "logging": True,
            "save_draw": False,
            "save_on_error": True,
            "stdout_level": 2,
        }
        with open(MAA_OPTION_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)

    try:
        from maa.toolkit import Toolkit
        Toolkit.init_option(str(PROJECT_ROOT))
    except Exception as e:
        # 初始化失败不阻断，只打印
        print(f"[config] Maa Toolkit.init_option 失败: {e}")


_init_maa_option()


# ---------- 任务清单（单一数据源） ----------

TASKS = {
    "启动游戏": {
        "label": "启动程序",
        "description": "拉起游戏并进入主界面。",
        "entry": "启动游戏",     # pipeline 里的入口节点名
        "enabled": True,
        "notify": "none",          # simple / report / none 是否发送通知
    },
    "一键收菜": {
        "label": "三角洲自动收菜",
        "description": "特勤处自动收取与建造",
        "entry": "三角洲自动收菜，启动！",
        "enabled": True,
        "notify": "report",
        "watch_nodes":{
             "制造剩余时间": "text",
             "进入技术中心": "node",
             "进入工作台": "node",
             "进入制药台": "node",
             "进入防具台": "node",
        },
    },
    "邮件检查": {
        "label": "邮件检查",
        "description": "检查补偿邮件，捕获过期时间。",
        "entry": "点击邮件",
        "enabled": True,
        "notify": "report",
        "watch_nodes":["过期时间"],
    },
    "关闭游戏": {
        "label": "关闭游戏",
        "description": "关闭游戏，释放资源。",
        "entry": "StopApp",
        "enabled": True,
        "notify": "none",
    },
    "切换到全面战场": {
        "label": "切换到全面战场",
        "description": "在烽火地带主页切换到全面战场",
        "entry": "切换到[全面战场]",
        "enabled": True,
        "notify": "none",
    },
    "切换到烽火地带": {
        "label": "切换到烽火地带",
        "description": "在全面战场主页切换到烽火地带",
        "entry": "切换到[烽火地带]",
        "enabled": True,
        "notify": "none",
    },
    "蜂医挂机": {
        "label": "蜂医挂机",
        "description": "在全面战场用蜂医挂机",
        "entry": "流水线1",
        "enabled": True,
        "notify": "report",
        "watch_nodes": {
            "识别进攻方图标":{"label":"攻防方","value":"进攻方"},
            "识别防守方图标":{"label":"攻防方","value":"防守方"},
            "读取全面战场战绩输赢":     {"label": "输赢"},
            "读取全面战场战绩得分":     {"label": "得分"},
            "读取全面战场战绩对局时间": {"label": "对局时间"},
            "读取全面战场战绩战斗时长": {"label": "战斗时长"},
            "曼德尔砖累计积分":         {"label": "曼德尔砖积分","strip_prefix":"累计积分："},
            "曼德尔砖周上限":         {"label": "曼德尔砖积分","strip_prefix":"制共享"},
        },
    },
    "测试":{
        "label": "test",
        "description": "测试用",
        "entry": "1test3",
        "enabled": True,
        "notify": "report",
        "watch_nodes": {
            "识别进攻方图标":{"label":"攻防方","value":"进攻方"},
            "识别防守方图标":{"label":"攻防方","value":"防守方"},
            "读取全面战场战绩输赢":     {"label": "输赢"},
            "读取全面战场战绩得分":     {"label": "得分"},
            "读取全面战场战绩对局时间": {"label": "对局时间"},
            "读取全面战场战绩战斗时长": {"label": "战斗时长"},
            "曼德尔砖累计积分":         {"label": "曼德尔砖积分","strip_prefix":"累计积分："},
            "曼德尔砖周上限":         {"label": "曼德尔砖积分","strip_prefix":"制共享"},
        },
    },
    "交易行": {
        "label": "交易行",
        "description": "交易行自动购买物品",
        "entry": "交易行",
        "notify": "none",
        "watch_nodes": {
            "识别价格": "text",
    },
},
    
}


# ---------- settings.txt 读取 ----------

def _parse_settings(path: Path) -> dict:
    config = {}
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    current_key = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" in raw and not raw.startswith((" ", "\t")):
                key, value = raw.split("=", 1)
                current_key = key.strip()
                config[current_key] = value.strip()
            elif current_key:
                config[current_key] += "\n" + stripped
    return config


# ---------- 对外接口 ----------

class Config:
    """配置容器，一次加载，全局复用"""

    def __init__(self):
        self.raw = _parse_settings(SETTINGS_FILE)

        # 通知
        self.webhook = self._require("dingtalk_webhook")
        self.secret = self.raw.get("dingtalk_secret", "")

        # ADB 可执行文件路径（通用，不随设备变）
        self.adb_path = self.raw.get("adb_path", "adb")

        # 路径
        self.project_root = PROJECT_ROOT
        self.resource_dir = RESOURCE_DIR
        self.pipeline_dir = PIPELINE_DIR
        self.log_dir = LOG_DIR
        self.deps_bin_dir = DEPS_BIN_DIR

        # 任务清单
        self.tasks = TASKS

    def _require(self, key: str) -> str:
        if key not in self.raw or not self.raw[key]:
            raise ValueError(f"settings.txt 缺少必要配置项: {key}")
        return self.raw[key]

    def get_task(self, name: str) -> dict:
        if name not in self.tasks:
            raise KeyError(f"未知任务: {name}")
        return self.tasks[name]

    def enabled_tasks(self) -> list[str]:
        return [k for k, v in self.tasks.items() if v.get("enabled", True)]


_config = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config