"""jobpilot 全局配置：路径、城市码、规则底稿、限速与风控默认值。

设计原则（来自 GitHub 方案调研）：
- 代码级常量放这里（改动需重启）；运营参数放 SQLite settings 表（页面即时改）。
- BOSS 采集走页面内 API（joblist.json），不再模拟点击翻列表。
- 开发期 ALLOW_LIVE=0，任何真实平台访问都被硬闸拦下。
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = BASE_DIR.parent                        # 简历/
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.environ.get("JOBILOT_DB", "")) if os.environ.get("JOBILOT_DB") \
    else DATA_DIR / "jobs.db"
EXPORT_DIR = Path(os.environ.get("JOBPILOT_EXPORT_DIR") or (WORKSPACE / "exports"))
COOKIES_PATH = DATA_DIR / "cookies.json"

# ---------------- 平台差异：Chrome 与专用配置目录 ----------------
IS_WINDOWS = sys.platform.startswith("win")


def _first_existing(paths):
    for p in paths:
        if p and Path(p).exists():
            return str(p)
    return ""


if IS_WINDOWS:
    _chrome = _first_existing([
        os.environ.get("JOBPILOT_CHROME"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ])
    CHROME = _chrome or os.environ.get(
        "JOBPILOT_CHROME",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    CHROME_PROFILE = Path(os.environ.get("JOBPILOT_PROFILE") or (
        Path(os.environ.get("LOCALAPPDATA") or Path.home())
        / "JobBot" / "chrome-profile"))
else:
    CHROME = os.environ.get(
        "JOBPILOT_CHROME",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    # 复用旧程序的登录目录（智联/猎聘登录态还在里面）
    CHROME_PROFILE = Path(os.environ.get("JOBPILOT_PROFILE") or (
        Path.home() / "Library" / "Application Support" / "JobBot" / "chrome-profile"))


def chrome_exists():
    """本机是否真的存在 Chrome 可执行文件（用于导出 PDF / 启动浏览器前的提示）。"""
    return bool(CHROME) and Path(CHROME).exists()

HOST, PORT = "127.0.0.1", int(os.environ.get("JOBILOT_PORT", "8765"))
# 真实访问开关：未设置 JOBPILOT_ALLOW_LIVE=1 时，Runtime 拒绝启动浏览器（开发期保护）
ALLOW_LIVE = os.environ.get("JOBPILOT_ALLOW_LIVE") == "1"

# ---------------- 平台与登录 ----------------
LOGIN_URLS = {"zhaopin": "https://www.zhaopin.com/",
              "liepin": "https://c.liepin.com/",
              "boss": "https://www.zhipin.com/web/geek/jobs"}
# 登录后页面头部会出现的账号名（用于登录检测）。
# 默认留空 → 用通用特征判断；本机账号名放在 local_config.py（已被 .gitignore 忽略）。
LOGIN_TEXT = {"zhaopin": "", "liepin": "", "boss": ""}
PLATFORM_NAMES = {"zhaopin": "智联", "liepin": "猎聘", "boss": "BOSS"}

CITIES = ["珠海", "中山", "广州", "深圳", "东莞"]
ZL_CITY = {"珠海": 766, "中山": 780, "广州": 763, "深圳": 765, "东莞": 779}
BOSS_CITY = {"珠海": "101280700", "中山": "101281700", "广州": "101280100",
             "深圳": "101280600", "东莞": "101281600"}

# BOSS 页面内搜索 API（在已登录页面里发请求，返回明文薪资 + 每条新鲜 securityId）
BOSS_API = "/wapi/zpgeek/search/joblist.json"
BOSS_JOBS = "https://www.zhipin.com/web/geek/jobs"
PUA_BASE = 0xE031          # 万一回退 DOM，薪资数字仍是私有区字体（0xE031..0xE03A → 0..9）

# ---------------- 风控与失效信号 ----------------
BLOCK_WORDS = ["账号行为异常", "安全验证", "访问频繁", "操作过于频繁", "请输入验证码",
               "行为异常", "安全中心"]
CLOSED_HINTS = ["职位已关闭", "该职位已关闭", "已停止招聘", "停止招聘", "职位已下线",
                "该职位已下线", "职位不存在", "该职位不存在", "招聘已结束", "该职位已结束",
                "已过期", "已失效", "职位已删除", "走丢了", "自动进入"]
HR_REJECT_WORDS = ["不合适", "不太合适", "不够匹配", "不匹配", "不太匹配", "不很匹配",
                   "不是很匹配", "较不匹配", "不适合", "不考虑", "不再沟通",
                   "已入职", "已招到", "已招满", "招聘结束", "岗位关闭",
                   "有合适机会", "合适机会之后", "很遗憾", "抱歉", "方向不符"]

# ---------------- 筛选规则底稿 ----------------
SALARY_FLOOR = 27000            # 默认薪资底线（settings 表可即时调）
SALARY_HARD_CAP = 20000         # 上限≤20K 无条件过滤，不受底线调整影响
ALLOW_DIRECTOR_LOW = ("珠海", "中山")   # 该城市总监级低于底线可保留
WHITELIST_KW = ["IT总监", "IT经理", "信息化", "信息部", "信息中心", "CIO", "数智",
                "信息技术", "数字化", "项目经理", "项目总监", "交付经理",
                "架构师", "AI产品", "AI应用", "AI项目", "流程IT", "IT负责人"]
BLACKLIST_KW = ["运维工程师", "技术支持", "桌面", "网络工程师", "Java", "全栈", "前端",
                "后端", "算法", "嵌入式", "开发工程师", "品类产品", "智能硬件产品",
                "选品", "工程项目经理", "土建", "施工", "仓储主管", "物业", "SMT",
                "数据库架构师", "安全架构师", "数据架构", "运维经理", "运维服务",
                "研发总监", "生产经理", "品质经理", "销售", "客服"]
HARD_GATE_PATTERNS = ["统招本科", "全日制本科", "全日制", "硕士及以上", "博士", "985",
                      "211", "35岁以下", "30岁以下", "年龄不超过", "英语可作为工作语言",
                      "雅思", "托福", "英语流利", "Fluent English"]

# ---------------- 限速与预算默认值（settings 表按平台可覆盖） ----------------
# interval=动作最小间隔  daily_apply=每日投递  daily_collect=每日采集轮次
# daily_api=BOSS API 请求上限  page/kw/city_pause=采集分层停顿秒区间
# pages_per_round=单关键词单轮最大页数  gap=API翻页间隔
RATE = {
    "zhaopin": {"interval": 8.0, "daily_apply": 18, "daily_collect": 2,
                "page_pause": (5.0, 9.0), "kw_pause": (6.0, 10.0),
                "city_pause": (15.0, 25.0), "pages_per_round": 3},
    "liepin":  {"interval": 15.0, "daily_apply": 12, "daily_collect": 2,
                "page_pause": (8.0, 14.0), "kw_pause": (8.0, 12.0),
                "city_pause": (15.0, 25.0), "pages_per_round": 3},
    "boss":    {"interval": 20.0, "daily_collect": 1,
                "daily_api": 60, "api_gap": (12.0, 22.0),
                "page_pause": (10.0, 18.0), "kw_pause": (12.0, 20.0),
                "city_pause": (25.0, 40.0), "pages_per_round": 10},
}
SESSION_PAGE_BUDGET = 30        # 单平台单次采集页数硬预算
COOLDOWN_MIN = 60               # 触发风控后的冷却分钟数
COLLECT_KEYWORDS = {"zhaopin": ["IT总监", "信息化经理", "信息部经理", "数字化总监",
                                "IT经理", "项目经理"],
                    "liepin": None,
                    "boss": ["IT经理", "信息化经理", "数字化", "IT总监"]}


# ---------------- 本地私有配置（公开发布时不包含，见 .gitignore） ----------------
# 复制 local_config.example.py 为 local_config.py，可覆盖导出目录、账号显示名等。
try:
    import local_config as _local
    for _k in ("LOGIN_TEXT", "EXPORT_DIR", "CHROME", "CHROME_PROFILE"):
        if hasattr(_local, _k):
            globals()[_k] = getattr(_local, _k)
except Exception:
    pass
