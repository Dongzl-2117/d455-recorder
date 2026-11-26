#!/usr/bin/env bash
# One-step launcher for record_d455.py on macOS and Linux.
#   bash run.sh                  record until Ctrl+C
#   bash run.sh --duration 600   any record_d455.py argument is passed through
#   bash run.sh selftest         check the installation without a camera
# First run: creates .venv next to this file and installs the packages (needs internet once).
set -eo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV=".venv"
PYTHON_BIN="${PYTHON:-python3}"
OS="$(uname -s)"

if [ ! -f "$VENV/.installed" ]; then
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "找不到 python3。请先安装 Python 3.9 或更新版本 (macOS: 终端里运行 xcode-select --install，或从 python.org 下载)。"
    exit 1
  fi
  if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "需要 Python 3.9 或更新版本，当前 $PYTHON_BIN 不可用或版本过低: $("$PYTHON_BIN" --version 2>&1 || true)"
    echo "macOS 可以在终端运行 xcode-select --install，或从 python.org 安装后重试。也可以用 PYTHON=/path/to/python3 bash run.sh 指定解释器。"
    exit 1
  fi
  echo "首次运行: 正在创建 $VENV 并安装依赖 (只做一次，需要联网)..."
  rm -rf "$VENV"
  "$PYTHON_BIN" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  if ! "$VENV/bin/python" -m pip install -r requirements.txt; then
    if [ "$OS" = "Darwin" ]; then
      echo "默认安装失败。新版 pyrealsense2-macosx 只支持 macOS 15 + Apple 芯片，正在尝试旧版 2.54.2 (支持 macOS 12 以上，需要 Python 3.8 到 3.12)..."
      if ! "$VENV/bin/python" -m pip install "pyrealsense2-macosx==2.54.2" numpy opencv-python "mcap>=1.0" "protobuf>=3.20"; then
        echo "安装失败。请把上面的报错发给我，并附上: sw_vers 和 $PYTHON_BIN --version 的输出。"
        exit 1
      fi
    else
      echo "安装失败，请检查网络后重试。"
      exit 1
    fi
  fi
  touch "$VENV/.installed"
  echo "依赖安装完成。"
fi

# macOS 12+: librealsense can only open the camera with administrator rights (official installation_osx.md).
# Commands that never touch the camera run without sudo.
NEED_ROOT=0
if [ "$OS" = "Darwin" ] && [ "$(id -u)" -ne 0 ]; then
  NEED_ROOT=1
  for arg in "$@"; do
    case "$arg" in
      selftest|verify|repair|export|--simulate|--no-sudo|-h|--help|--version) NEED_ROOT=0 ;;
    esac
  done
fi

if [ "$NEED_ROOT" -eq 1 ]; then
  echo "macOS 需要管理员权限才能打开 RealSense 相机。如果提示 Password，请输入这台 Mac 的登录密码 (输入时不显示字符)。"
  exec sudo "$VENV/bin/python" record_d455.py "$@"
else
  exec "$VENV/bin/python" record_d455.py "$@"
fi
