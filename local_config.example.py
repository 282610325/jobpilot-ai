"""本地私有配置示例。

复制为 local_config.py（该文件已被 .gitignore 忽略，不会上传），按需修改。
它会被 config.py 自动加载，可覆盖 Chrome 路径、导出目录和各平台的账号显示名。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 导出目录（简历 PDF / 定制简历 / 岗位 Excel）
EXPORT_DIR = BASE_DIR.parent / "exports"

# 各平台登录后页面头部会出现的账号名（用于登录检测）
# 留空则用通用特征（出现「你好/消息/我的简历/个人中心/退出」且无「登录/注册/扫码」）
LOGIN_TEXT = {"zhaopin": "", "liepin": "", "boss": ""}

# Chrome 可执行文件（一般不用填；装在非标准路径时再指定）
# Windows 示例：CHROME = r"D:\Apps\Chrome\chrome.exe"
# macOS 示例：CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# CHROME = ""

# 专用配置目录（登录态存这里，一般不用改）
# Windows 默认：%LOCALAPPDATA%\JobBot\chrome-profile
# macOS 默认：~/Library/Application Support/JobBot/chrome-profile
# CHROME_PROFILE = BASE_DIR.parent / "chrome-profile"
