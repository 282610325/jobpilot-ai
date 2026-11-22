"""本地私有配置示例。

复制为 local_config.py（该文件已被 .gitignore 忽略，不会上传），按需修改。
它会被 config.py 自动加载，可覆盖导出目录和各平台的账号显示名。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 导出目录（简历 PDF / 定制简历 / 岗位 Excel）
EXPORT_DIR = BASE_DIR.parent / "exports"

# 各平台登录后页面头部会出现的账号名（用于登录检测）
# 留空则用通用特征（出现「你好/消息/我的简历/个人中心/退出」且无「登录/注册/扫码」）
LOGIN_TEXT = {"zhaopin": "", "liepin": "", "boss": ""}
