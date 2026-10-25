#!/usr/bin/env bash
# 在 Linux 上建立（或修复）长期使用的 Demo 运行环境。重复运行是安全的。
#
#   bash scripts/setup_linux_env.sh [环境路径]        默认 ~/venvs/ship-demo
#
# - 环境放在 home 下：共享文件夹不支持符号链接，/tmp 是内存盘、重启即清空。
# - 按 requirements-lock-linux.txt 安装精确版本；先只用本地缓存（~/.cache/uv），缺包时才联网补齐。
# - 用系统 Python（默认 /usr/bin/python3）；可用环境变量 SHIP_DEMO_PYTHON 指定别的解释器。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${1:-$HOME/venvs/ship-demo}"
PYTHON="${SHIP_DEMO_PYTHON:-/usr/bin/python3}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"

command -v uv >/dev/null || { echo "需要先安装 uv：https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }

if [ ! -x "$VENV/bin/python" ]; then
    mkdir -p "$(dirname "$VENV")"
    uv venv --python "$PYTHON" "$VENV"
fi

args=(-r "$PROJECT_DIR/requirements-lock-linux.txt"
      --extra-index-url https://download.pytorch.org/whl/cpu
      --index-strategy unsafe-best-match)
if ! uv pip install --python "$VENV/bin/python" --offline "${args[@]}"; then
    echo "本地缓存缺少部分安装包，改为联网补齐……"
    uv pip install --python "$VENV/bin/python" "${args[@]}"
fi

"$VENV/bin/python" -c "import torch, cv2, ultralytics; print('torch', torch.__version__, '| opencv', cv2.__version__, '| ultralytics', ultralytics.__version__)"
echo "环境就绪：$VENV"
echo "日常使用：source \"$PROJECT_DIR/scripts/linux_env.sh\""
