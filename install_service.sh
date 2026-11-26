#!/usr/bin/env bash
# macOS: run D455 Studio as a background service, so recording never asks for a password again.
#   bash install_service.sh            install or update it, start it now and at every boot (password once)
#   bash install_service.sh status     is it running, last log lines
#   bash install_service.sh stop       stop it until the next boot (frees the camera for run.sh)
#   bash install_service.sh start      start it again
#   bash install_service.sh uninstall  remove it (recordings are kept)
# Options for install: --sessions DIR (default ~/D455/sessions), --port N (default 8455).
#
# What it sets up, and why:
#   /Library/D455Studio/              program, its own Python and packages; owned by root and not writable by
#                                     your account, because this code runs with administrator rights
#   /Library/LaunchDaemons/local.d455studio.plist   starts it at boot and restarts it if it stops
#   /Library/Logs/D455Studio/         its log
#   ~/D455/sessions                   recordings, owned by you. macOS does not let background services write
#                                     into Desktop, Documents or Downloads, so they cannot go there.
# The web page is only reachable from this Mac (127.0.0.1).
set -euo pipefail

LABEL="local.d455studio"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
HOME_DIR="/Library/D455Studio"
CODE="$HOME_DIR/app"
VENV="$CODE/.venv"            # record_d455.py keeps its IMU crash guard library in .venv next to itself
PYROOT="$HOME_DIR/python"
SETTINGS="$HOME_DIR/settings.json"
LOGS="/Library/Logs/D455Studio"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_VERSION="3.12"

say() { printf '%s\n' "$*"; }
die() { printf '\n安装没有完成：%s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "这个脚本只用于 macOS。Windows 和 Linux 直接运行 studio.bat / studio.sh 即可，不需要密码。"

COMMAND="install"
SESSIONS=""
PORT="8455"
while [ $# -gt 0 ]; do
  case "$1" in
    install|uninstall|status|start|stop|restart|logs) COMMAND="$1" ;;
    --sessions) SESSIONS="${2:?--sessions needs a folder}"; shift ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "不认识的参数 $1（bash install_service.sh --help 查看用法）" ;;
  esac
  shift
done
case "$PORT" in ''|*[!0-9]*) die "--port 需要一个数字" ;; esac
URL="http://127.0.0.1:${PORT}/"

if [ "$COMMAND" = "status" ] || [ "$COMMAND" = "logs" ]; then
  if [ "$COMMAND" = "status" ]; then
    if [ -f "$PLIST" ]; then say "已安装：$PLIST"; else say "没有安装。"; fi
    launchctl print "system/$LABEL" 2>/dev/null | grep -E '^\s*(state|pid|last exit code) =' || say "服务没有在运行。"
    if curl -fsS --max-time 2 "${URL}api/ping" >/dev/null 2>&1; then say "网页正常：$URL"; else say "网页没有响应：$URL"; fi
  fi
  [ -f "$LOGS/studio.log" ] && { say "--- $LOGS/studio.log（最后几行）"; tail -n "$([ "$COMMAND" = logs ] && echo 200 || echo 15)" "$LOGS/studio.log"; }
  exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
  say "需要管理员权限来安装后台服务。如果提示 Password，请输入这台 Mac 的登录密码（输入时不显示字符）。"
  say "这是唯一一次：装好以后打开网页就能录，不再要密码。"
  exec sudo /bin/bash "$0" "$COMMAND" ${SESSIONS:+--sessions "$SESSIONS"} --port "$PORT"
fi
USER_NAME="${SUDO_USER:-}"
[ -n "$USER_NAME" ] && [ "$USER_NAME" != "root" ] || die "请在自己的账户里运行 bash install_service.sh（不要先切换到 root）。"
USER_UID="$(id -u "$USER_NAME")"
USER_GID="$(id -g "$USER_NAME")"
USER_HOME="$(dscl . -read "/Users/$USER_NAME" NFSHomeDirectory | awk '{print $2}')"
[ -d "$USER_HOME" ] || die "找不到 $USER_NAME 的个人文件夹。"

stop_service() {
  launchctl bootout "system/$LABEL" >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do  # bootout returns before the old process is gone
    launchctl print "system/$LABEL" >/dev/null 2>&1 || return 0
    sleep 0.25
  done
}
start_service() {
  launchctl enable "system/$LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap system "$PLIST" 2>/dev/null || launchctl kickstart -k "system/$LABEL"
}
wait_for_page() {
  for _ in $(seq 1 120); do
    curl -fsS --max-time 1 "${URL}api/ping" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}
open_page() { launchctl asuser "$USER_UID" sudo -u "$USER_NAME" open "$URL" >/dev/null 2>&1 || true; }

case "$COMMAND" in
  stop)
    stop_service; say "已停止（重启电脑后会自动再开；bash install_service.sh start 立即再开）。"; exit 0 ;;
  start|restart)
    [ -f "$PLIST" ] || die "还没有安装，先运行 bash install_service.sh"
    stop_service; start_service
    if wait_for_page; then say "已启动：$URL"; open_page; else say "已启动，但网页还没响应，看日志：bash install_service.sh logs"; fi
    exit 0 ;;
  uninstall)
    stop_service
    rm -f "$PLIST"
    rm -rf "$HOME_DIR" "$LOGS"
    say "已卸载。录制的数据没有动，仍在你选的文件夹里（默认 ~/D455/sessions）。"
    exit 0 ;;
esac

# ---------------------------------------------------------------------------------------------- install
for file in record_d455.py d455_studio.py requirements.txt studio/index.html studio/app.js studio/style.css run.sh; do
  [ -f "$SRC/$file" ] || die "缺少 $file，请在 d455_recorder 文件夹里运行这个脚本。"
done

SESSIONS="${SESSIONS:-$USER_HOME/D455/sessions}"
case "$SESSIONS" in /*) ;; *) SESSIONS="$USER_HOME/$SESSIONS" ;; esac
case "$SESSIONS/" in
  "$USER_HOME/Desktop/"*|"$USER_HOME/Documents/"*|"$USER_HOME/Downloads/"*|"$USER_HOME/Library/"*|/Volumes/*)
    die "macOS 不允许后台服务写入 $SESSIONS（桌面、文稿、下载、外接盘都受保护）。换一个文件夹，例如默认的 ~/D455/sessions。" ;;
esac

if [ -f "$PLIST" ]; then
  stop_service
elif curl -fsS --max-time 1 "${URL}api/ping" >/dev/null 2>&1; then
  die "端口 $PORT 上已经有一个 D455 Studio 在运行（大概是 studio.sh 开的终端窗口）。先在那个窗口按 Ctrl+C 关掉，再运行这个脚本。"
fi

WORK="$(mktemp -d /tmp/d455studio-install.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
chmod 700 "$WORK"

say "1/5 准备安装工具 uv（只用于这次安装）..."
UV="$(command -v uv || true)"
if [ -z "$UV" ]; then
  # The recorder's own .venv (created by run.sh as you) provides pip; uv comes from PyPI.
  sudo -H -u "$USER_NAME" bash "$SRC/run.sh" --version >/dev/null || die "run.sh 没能准备好 Python 环境。"
  mkdir -p "$WORK/user" && chown "$USER_NAME" "$WORK/user"
  sudo -H -u "$USER_NAME" "$SRC/.venv/bin/python" -m pip install --quiet --disable-pip-version-check \
    --target "$WORK/user/uv" uv || die "下载 uv 失败，请检查网络。"
  found="$(find "$WORK/user/uv" -path '*/bin/uv' -type f | head -n 1)"
  [ -n "$found" ] || die "uv 安装包里没有找到可执行文件。"
  install -m 755 -o root -g wheel "$found" "$WORK/uv"  # root runs its own copy, not a file your account can change
  UV="$WORK/uv"
fi
export UV_CACHE_DIR="$WORK/cache" UV_PYTHON_INSTALL_DIR="$PYROOT" UV_PYTHON_BIN_DIR="$WORK/bin" \
       UV_PYTHON_PREFERENCE="only-managed" UV_LINK_MODE="copy" UV_NO_CONFIG=1 HOME="/var/root"

say "2/5 安装服务专用的 Python $PYTHON_VERSION（下载约 25 MB，装好约 70 MB，在 $PYROOT）..."
mkdir -p "$HOME_DIR"
"$UV" python install "$PYTHON_VERSION" >/dev/null || die "下载 Python 失败，请检查网络（需要能访问 github.com）。"
BASE_PY="$("$UV" python find "$PYTHON_VERSION")" || die "装好的 Python 找不到。"
case "$BASE_PY" in "$PYROOT"/*) ;; *) die "找到的 Python 不在 $PYROOT：$BASE_PY" ;; esac

say "3/5 复制程序并安装依赖..."
rm -rf "$CODE"
mkdir -p "$CODE/studio"
for file in record_d455.py d455_studio.py requirements.txt read_session_example.py README.md; do
  [ -f "$SRC/$file" ] && cp "$SRC/$file" "$CODE/$file"
done
cp "$SRC/studio/index.html" "$SRC/studio/app.js" "$SRC/studio/style.css" "$CODE/studio/"
"$UV" venv --quiet --python "$BASE_PY" "$VENV" || die "创建 Python 环境失败。"
if ! "$UV" pip install --quiet --python "$VENV/bin/python" -r "$CODE/requirements.txt"; then
  say "默认版本安装失败，改试 pyrealsense2-macosx 2.54.2（支持 macOS 12 以上）..."
  "$UV" pip install --quiet --python "$VENV/bin/python" "pyrealsense2-macosx==2.54.2" numpy opencv-python \
    "mcap>=1.0" "protobuf>=3.20" || die "依赖安装失败，请把上面的报错发给我。"
fi

say "4/5 编译 IMU 崩溃保护（需要 Xcode 命令行工具）..."
guard="$(env -u SUDO_UID -u SUDO_GID -u SUDO_USER "$VENV/bin/python" -c '
import sys; sys.path.insert(0, sys.argv[1])
import record_d455 as rd
path = rd.prepare_macos_hid_guard()
print(path if path else "FAILED: " + rd.HID_GUARD_STATE["note"])' "$CODE")"
case "$guard" in
  FAILED:*) die "${guard#FAILED: }。没有它，SDK 以管理员身份打开相机时会崩溃（终端运行 xcode-select --install 后重试）。" ;;
esac
"$VENV/bin/python" -c "import numpy, cv2, mcap, pyrealsense2" || die "依赖装好了但导入失败，请把上面的报错发给我。"

chown -R root:wheel "$HOME_DIR"
chmod -R u+rwX,go+rX,go-w "$HOME_DIR"
mkdir -p "$LOGS" && chown root:wheel "$LOGS" && chmod 755 "$LOGS"
sudo -u "$USER_NAME" mkdir -p "$SESSIONS" || die "不能创建 $SESSIONS"

say "5/5 注册开机服务并启动..."
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
cat > "$PLIST.new" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$VENV/bin/python")</string>
    <string>$(xml "$CODE/d455_studio.py")</string>
    <string>--no-browser</string>
    <string>--port</string><string>${PORT}</string>
    <string>--sessions</string><string>$(xml "$SESSIONS")</string>
    <string>--settings</string><string>$(xml "$SETTINGS")</string>
    <string>--log-dir</string><string>$(xml "$LOGS")</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- record_d455 gives new recordings to this user (it reads the variables sudo would set) -->
    <key>SUDO_UID</key><string>${USER_UID}</string>
    <key>SUDO_GID</key><string>${USER_GID}</string>
    <key>SUDO_USER</key><string>$(xml "$USER_NAME")</string>
    <key>HOME</key><string>/var/root</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PYTHONUTF8</key><string>1</string>
  </dict>
  <key>WorkingDirectory</key><string>$(xml "$CODE")</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$(xml "$LOGS/studio.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOGS/studio.log")</string>
</dict>
</plist>
PLIST_END
plutil -lint "$PLIST.new" >/dev/null || die "生成的 plist 有误。"
chown root:wheel "$PLIST.new" && chmod 644 "$PLIST.new" && mv -f "$PLIST.new" "$PLIST"
start_service

webloc="$(dirname "$SESSIONS")/D455 Studio.webloc"
sudo -u "$USER_NAME" /bin/sh -c 'cat > "$1"' sh "$webloc" <<WEBLOC_END || true
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>${URL}</string></dict></plist>
WEBLOC_END

if wait_for_page; then
  say ""
  say "装好了。以后打开 $URL 就能看画面和录制，不需要再输密码（建议把这个网址加到书签）。"
  say "录制保存在 $SESSIONS ；快捷方式：$webloc"
  say "查看状态：bash install_service.sh status    卸载：bash install_service.sh uninstall"
  open_page
else
  say ""
  say "服务已注册，但网页 1 分钟内没有响应。请运行 bash install_service.sh logs 把输出发给我。"
  exit 1
fi
