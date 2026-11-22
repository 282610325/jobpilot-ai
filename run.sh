#!/bin/bash
# jobpilot 一键启动：建环境 → 装依赖 → 起服务 → 打开控制台
# 默认离线模式（不会访问招聘网站）。需要真实采集/投递时：
#   JOBPILOT_ALLOW_LIVE=1 ./run.sh
set -e
cd "$(dirname "$0")"

PY=""
for c in "$HOME/.local/bin/python3.11" python3.11 python3.12 python3.13 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -z "$PY" ] && { echo "未找到 python3.11+，请先安装"; exit 1; }

if [ ! -x ".venv/bin/python" ]; then
  echo "创建虚拟环境（$PY）..."
  "$PY" -m venv .venv
fi
./.venv/bin/pip install -q -r requirements.txt \
  -i https://pypi.tuna.tsinghua.edu.cn/simple

# 初始化数据库
./.venv/bin/python -c "import db; db.init()"

[ -z "$JOBPILOT_ALLOW_LIVE" ] && echo "提示：当前为离线模式（不访问招聘网站）。实测请加 JOBPILOT_ALLOW_LIVE=1"
(sleep 1.5; open "http://127.0.0.1:${JOBILOT_PORT:-8765}") 2>/dev/null &
exec ./.venv/bin/python app.py
