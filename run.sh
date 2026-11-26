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
PIP_LOG="logs/pip_install.log"
UDEV_RULES="99-realsense-libusb.rules"

# pip with its output kept, so that a failure can be explained (pip's own message is easy to misread)
pip_install() {
  mkdir -p logs
  "$VENV/bin/python" -m pip install "$@" 2>&1 | tee "$PIP_LOG"
}
explain_pip_failure() {
  local version arch
  version="$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
  arch="$(uname -m)"
  if grep -qiE "No matching distribution found for pyrealsense2|satisfies the requirement pyrealsense2" "$PIP_LOG"; then
    echo "安装失败：RealSense 的 Python 包（pyrealsense2）没有适合 Python ${version}、${OS} ${arch} 的版本。这不是网络问题。"
    if [ "$OS" = "Linux" ] && { [ "$arch" = "aarch64" ] || [ "$arch" = "arm64" ]; }; then
      echo "RealSense 没有为 ARM 的 Linux（树莓派、Jetson 等）发布 Python 包：要么从源码编译 librealsense，要么换一台 x86 电脑录。"
    else
      echo "装一个 Python 3.12，再运行：rm -rf .venv && PYTHON=python3.12 bash run.sh selftest"
    fi
  elif grep -qiE "name resolution|Failed to establish a new connection|timed out|ProxyError|SSLError|CERTIFICATE_VERIFY_FAILED|Network is unreachable|Connection refused" "$PIP_LOG"; then
    echo "安装失败：连不上 Python 软件包源（pypi.org）。检查网络和代理后重试；这个网络访问 pypi.org 慢的话，可以用镜像：PIP_INDEX_URL=镜像地址 bash run.sh selftest"
  elif grep -qiE "No space left on device" "$PIP_LOG"; then
    echo "安装失败：磁盘空间不够。清出几 GB 再重试。"
  else
    echo "安装失败。完整输出在 ${PIP_LOG}，请把最后 30 行发给维护者，并附上 uname -a 和 $PYTHON_BIN --version 的输出。"
  fi
}

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
  if ! "$PYTHON_BIN" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    version="$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    if [ "$OS" = "Linux" ]; then
      echo "这个 Python 缺少创建虚拟环境的模块（Ubuntu 和 Debian 默认不带）。先运行："
      echo "  sudo apt install python3-venv      （还不行就 sudo apt install python${version}-venv）"
      echo "然后再运行这个命令。"
    else
      echo "这个 Python ($PYTHON_BIN, ${version}) 不能创建虚拟环境（缺 venv 或 ensurepip）。换一个 Python 3.9 以上：PYTHON=/path/to/python3 bash run.sh"
    fi
    exit 1
  fi
  echo "首次运行: 正在创建 $VENV 并安装依赖 (只做一次，需要联网)..."
  rm -rf "$VENV"
  "$PYTHON_BIN" -m venv "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip || true
  if ! pip_install -r requirements.txt; then
    if [ "$OS" = "Darwin" ] && grep -qiE "satisfies the requirement pyrealsense2|No matching distribution found for pyrealsense2" "$PIP_LOG"; then
      echo "默认安装失败。新版 pyrealsense2-macosx 只支持 macOS 15 + Apple 芯片，正在尝试旧版 2.54.2 (支持 macOS 12 以上，需要 Python 3.8 到 3.12)..."
      if ! pip_install "pyrealsense2-macosx==2.54.2" numpy opencv-python "mcap>=1.0" "protobuf>=3.20"; then
        explain_pip_failure
        echo "Mac 上请同时附上 sw_vers 的输出。"
        exit 1
      fi
    else
      explain_pip_failure
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

# Linux: without RealSense's udev rules only root can open the camera
if [ "$OS" = "Linux" ] && [ "$(id -u)" -ne 0 ] && [ ! -f "/etc/udev/rules.d/$UDEV_RULES" ] \
   && [ ! -f "/lib/udev/rules.d/$UDEV_RULES" ] && [ ! -f "/usr/lib/udev/rules.d/$UDEV_RULES" ]; then
  case " $* " in
    *" selftest "*|*" verify "*|*" repair "*|*" export "*|*" --simulate "*|*" --version "*|*" -h "*|*" --help "*) ;;
    *)
      echo "提示：还没装 RealSense 的 udev 规则，普通用户打不开相机。运行一次下面两行，再拔插相机："
      echo "  sudo curl -fsSL https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/$UDEV_RULES -o /etc/udev/rules.d/$UDEV_RULES"
      echo "  sudo udevadm control --reload-rules && sudo udevadm trigger" ;;
  esac
fi

if [ "$NEED_ROOT" -eq 1 ]; then
  echo "macOS 需要管理员权限才能打开 RealSense 相机。如果提示 Password，请输入这台 Mac 的登录密码 (输入时不显示字符)。"
  exec sudo "$VENV/bin/python" record_d455.py "$@"
else
  exec "$VENV/bin/python" record_d455.py "$@"
fi
