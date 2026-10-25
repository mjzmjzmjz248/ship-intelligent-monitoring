# 进入 Demo 运行环境（bash）。用法：
#   source "<项目目录>/scripts/linux_env.sh"
# 之后即可直接运行 python scripts/run_all.py 等命令。环境路径可用 SHIP_DEMO_VENV 覆盖。

_ship_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
_ship_venv="${SHIP_DEMO_VENV:-$HOME/venvs/ship-demo}"
if [ -f "$_ship_venv/bin/activate" ]; then
    source "$_ship_venv/bin/activate"
    cd "$_ship_dir"
    # 船端与岸端之间是本机通信，不能走系统代理
    export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost"
    export no_proxy="$NO_PROXY"
    echo "已进入船舶智能监控 Demo 环境（$(python --version)，$_ship_venv）"
else
    echo "未找到环境 $_ship_venv，请先运行：bash \"$_ship_dir/scripts/setup_linux_env.sh\""
fi
unset _ship_dir _ship_venv
