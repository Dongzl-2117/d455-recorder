#!/usr/bin/env bash
# macOS: install D455 Studio as a background service, so recording never asks for a password again.
#   bash install_service.sh            install or update it (password once), then open the page
#   bash install_service.sh doctor     check this Mac, the camera connection and the service; explains problems
#   bash install_service.sh status     installed? running right now?
#   bash install_service.sh logs       the service's log
#   bash install_service.sh stop       switch it off completely, also after a reboot (frees port and camera)
#   bash install_service.sh start      switch it on again
#   bash install_service.sh uninstall  remove it (recordings are kept)
# Options for install: --sessions DIR (default ~/D455/sessions), --port N (default 8455),
#   --index-url URL (a PyPI mirror), --python-mirror URL (a mirror of python-build-standalone),
#   when pypi.org or github.com are slow or blocked on this network.
# On a new Mac: install the Xcode command line tools once (xcode-select --install), then
#   git clone https://github.com/Dongzl-2117/d455-recorder.git && cd d455-recorder && bash install_service.sh
# Run the same command again after a git pull to update the service.
#
# How it runs: on demand. Nothing of it runs at boot; launchd (part of macOS, always running) only keeps
# port 8455 open. Opening the page starts the program (a few seconds), and it exits by itself 5 minutes after
# the last page was closed if nothing is recording. Then it uses no CPU or memory and macOS gets the camera
# back. The page also has a button to close it right away. Starting and closing it never needs a password.
#
# What it sets up, and why:
#   /Library/D455Studio/              program, its own Python and packages; owned by root and not writable by
#                                     your account, because this code runs with administrator rights
#   /Library/LaunchDaemons/local.d455studio.plist   the on-demand start on port 8455
#   /Library/Logs/D455Studio/         its log
#   ~/D455/sessions                   recordings, owned by you. macOS does not let background services write
#                                     into Desktop, Documents or Downloads, so they cannot go there.
# The web page is only reachable from this Mac (127.0.0.1).
set -Eeuo pipefail

LABEL="local.d455studio"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
HOME_DIR="/Library/D455Studio"
CODE="${HOME_DIR}/app"
VENV="${CODE}/.venv"            # record_d455.py keeps its IMU crash guard library in .venv next to itself
PYROOT="${HOME_DIR}/python"
SETTINGS="${HOME_DIR}/settings.json"
LOGS="/Library/Logs/D455Studio"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_VERSION="3.12"
EXIT_AFTER="300"                # seconds without any page before the program exits (launchd restarts it on demand)

say() { printf '%s\n' "$*"; }
STEP="准备"
die() {
  printf '\n没有完成（%s）：%s\n' "${STEP}" "$*" >&2
  printf '排查：bash install_service.sh doctor   把它的输出发给开发者也行。\n' >&2
  exit 1
}
on_error() { die "第 $1 行的命令出错（见上面的报错）"; }
trap 'on_error ${LINENO}' ERR

[ "$(uname -s)" = "Darwin" ] || die "这个脚本只用于 macOS。Windows 和 Linux 直接运行 studio.bat / studio.sh 即可，不需要密码。"

COMMAND="install"
SESSIONS=""
PORT=""
INDEX_URL=""
PYTHON_MIRROR=""
NO_BROWSER=""
while [ $# -gt 0 ]; do
  case "$1" in
    install|uninstall|status|start|stop|restart|logs|doctor) COMMAND="$1" ;;
    --sessions) SESSIONS="${2:?--sessions 后面要跟一个文件夹}"; shift ;;
    --port) PORT="${2:?--port 后面要跟一个数字}"; shift ;;
    --index-url) INDEX_URL="${2:?--index-url 后面要跟一个网址}"; shift ;;
    --python-mirror) PYTHON_MIRROR="${2:?--python-mirror 后面要跟一个网址}"; shift ;;
    --exit-after) EXIT_AFTER="${2:?}"; shift ;;  # tests
    --no-browser) NO_BROWSER=1 ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) die "不认识的参数 $1（bash install_service.sh --help 查看用法）" ;;
  esac
  shift
done

# What the installed service uses now (an update keeps the folder and port unless they are given again).
previous_arg() {
  [ -f "${PLIST}" ] || return 0
  local listing
  listing="$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments" "${PLIST}" 2>/dev/null || true)"
  printf '%s\n' "${listing}" \
    | awk -v key="$1" 'found { sub(/^[ \t]+/, ""); print; exit } { sub(/^[ \t]+/, "") } $0 == key { found = 1 }' || true
}
[ -n "${PORT}" ] || PORT="$(previous_arg --port)"
PORT="${PORT:-8455}"
case "${PORT}" in ''|*[!0-9]*) die "--port 需要一个数字" ;; esac
URL="http://127.0.0.1:${PORT}/"

service_state() {  # running / idle / not-loaded / not-installed
  [ -f "${PLIST}" ] || { echo not-installed; return; }
  local text
  text="$(launchctl print "system/${LABEL}" 2>/dev/null)" || { echo not-loaded; return; }
  if printf '%s\n' "${text}" | grep -qE '^[[:space:]]*pid = [0-9]+'; then echo running; else echo idle; fi
}

installed_python() { [ -x "${VENV}/bin/python" ] && [ -f "${CODE}/d455_studio.py" ]; }

# ---------------------------------------------------------------------------------------- checks
# Each check prints one line; a problem prints the reason and what to do. Run before the password prompt,
# so a Mac that cannot work says so before anything is changed.
PROBLEMS=0
ok()   { say "✓ $1"; }
warn() { say "! $1"; [ -n "${2:-}" ] && say "    办法：$2"; return 0; }
bad()  { say "✗ $1"; [ -n "${2:-}" ] && say "    办法：$2"; PROBLEMS=$((PROBLEMS + 1)); return 0; }

check_mac() {
  local version major arch translated
  version="$(sw_vers -productVersion 2>/dev/null || echo 0)"
  major="${version%%.*}"
  arch="$(uname -m)"
  translated="$(sysctl -n sysctl.proc_translated 2>/dev/null || echo 0)"
  if [ "${major}" -lt 12 ] 2>/dev/null; then
    bad "macOS ${version} 太旧，RealSense 的 macOS 版 SDK 需要 macOS 12 或更新" "升级 macOS，或者用 Windows / Ubuntu 电脑。"
  elif [ "${translated}" = "1" ]; then
    bad "这个终端在 Rosetta（Intel 模式）下运行" "在访达的「应用程序 > 实用工具」里选中「终端」，显示简介，取消「使用 Rosetta 打开」，重开终端再运行。"
  else
    local chip="Apple 芯片"; [ "${arch}" = "x86_64" ] && chip="Intel 芯片"
    ok "macOS ${version}，${chip}"
    if [ "${arch}" = "x86_64" ] || [ "${major}" -lt 15 ]; then
      say "    （这台 Mac 会用 pyrealsense2-macosx 2.54.2：新版只支持 macOS 15 以上的 Apple 芯片）"
    fi
  fi
}

check_admin() {
  if id -Gn "$(id -un)" 2>/dev/null | tr ' ' '\n' | grep -qx admin; then
    ok "当前账户 $(id -un) 是管理员"
  else
    bad "当前账户 $(id -un) 不是管理员，装不了后台服务" "用管理员账户登录再运行，或者请管理员在 系统设置 > 用户与群组 里给这个账户打开「允许用户管理这台电脑」。"
  fi
}

check_tools() {
  if xcode-select -p >/dev/null 2>&1 && /usr/bin/python3 -c 'import venv' >/dev/null 2>&1; then
    ok "Xcode 命令行工具已安装"
  else
    bad "缺 Xcode 命令行工具（编译 IMU 崩溃防护和启动安装程序都要用）" "终端运行 xcode-select --install，在弹窗里点「安装」（约 5 分钟），装完再运行这个脚本。"
  fi
}

check_network() {
  local pypi="${INDEX_URL:-https://pypi.org/simple/}" python="${PYTHON_MIRROR:-https://github.com}"
  if curl -fsS -o /dev/null --max-time 12 "${pypi%/}/uv/" 2>/dev/null || curl -fsS -o /dev/null --max-time 12 "${pypi}" 2>/dev/null; then
    ok "能访问 Python 软件包源（${pypi}）"
  else
    bad "访问不了 ${pypi}（下载依赖要用）" "检查网络或代理；这个网络屏蔽 pypi.org 的话，用一个 PyPI 镜像：bash install_service.sh --index-url 镜像地址"
  fi
  if [ -x "${PYROOT}/bin/python3" ] || ls -d "${PYROOT}"/cpython-"${PYTHON_VERSION}"* >/dev/null 2>&1; then
    return 0  # the service's Python is already there, nothing to download
  fi
  if curl -fsS -o /dev/null --max-time 12 "${python}" 2>/dev/null; then
    ok "能访问 ${python}（下载服务专用的 Python 要用）"
  else
    bad "访问不了 ${python}（下载服务专用的 Python 要用）" "检查网络或代理；也可以指定 python-build-standalone 的镜像：bash install_service.sh --python-mirror 镜像地址"
  fi
}

check_disk() {
  local free_kb
  free_kb="$(df -k /Library 2>/dev/null | awk 'NR == 2 { print $4 }' || true)"
  if [ "${free_kb:-0}" -lt 1500000 ]; then
    bad "系统盘只剩 $((free_kb / 1024)) MB，安装要约 1 GB" "清理一些空间再装。"
  else
    ok "系统盘剩余 $((free_kb / 1024 / 1024)) GB"
  fi
}

check_sessions_folder() {  # $1 = folder
  case "$1/" in
    "${2}/Desktop/"*|"${2}/Documents/"*|"${2}/Downloads/"*|"${2}/Library/"*)
      bad "录像目录 $1 在桌面、文稿或下载里，macOS 不允许后台服务写入这些位置" "换一个文件夹，例如默认的 ~/D455/sessions：bash install_service.sh --sessions ~/D455/sessions" ;;
    /Volumes/*)
      warn "录像目录 $1 在外接盘上：要额外给服务的 Python 打开「完全磁盘访问权限」，装完会告诉你怎么做" ;;
    *) ok "录像目录 $1" ;;
  esac
}

check_port() {
  local owner
  owner="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | awk 'NR == 2 { print $1 " (pid " $2 ")" }' || true)"
  if [ -z "${owner}" ]; then
    ok "端口 ${PORT} 空闲"
  elif [ "${owner%% *}" = "launchd" ] && [ -f "${PLIST}" ]; then
    ok "端口 ${PORT} 由已安装的服务使用"
  elif curl -fsS --max-time 2 "${URL}api/ping" 2>/dev/null | grep -q version && [ ! -f "${PLIST}" ]; then
    bad "端口 ${PORT} 上有一个 studio.sh 开的 D455 Studio 正在运行" "在那个终端窗口按 Ctrl+C 关掉它，再运行这个脚本。"
  elif [ -f "${PLIST}" ] && curl -fsS --max-time 2 "${URL}api/ping" 2>/dev/null | grep -q version; then
    ok "端口 ${PORT} 由 D455 Studio 使用"
  else
    bad "端口 ${PORT} 被 ${owner} 占着" "关掉那个程序，或者换一个端口：bash install_service.sh --port 8456"
  fi
}

preflight() {  # before sudo: everything that can be checked as the user
  say "检查这台 Mac..."
  check_mac
  check_admin
  check_tools
  check_network
  check_disk
  local folder="${SESSIONS:-$(previous_arg --sessions)}"
  folder="${folder:-${HOME}/D455/sessions}"
  case "${folder}" in /*) ;; "~"/*) folder="${HOME}/${folder#\~/}" ;; *) folder="${HOME}/${folder}" ;; esac
  check_sessions_folder "${folder}" "${HOME}"
  check_port
  if [ "${PROBLEMS}" -gt 0 ]; then
    say ""
    say "有 ${PROBLEMS} 个问题要先解决（上面标 ✗ 的），什么都还没有改动。"
    exit 1
  fi
  say ""
}

doctor() {
  say "D455 Studio 体检（不改动任何东西）"
  say ""
  check_mac
  check_tools
  case "$(service_state)" in
    running) ok "后台服务已安装，正在运行" ;;
    idle) ok "后台服务已安装，现在没有运行（打开网页时自动启动，这是正常的）" ;;
    not-loaded) bad "后台服务已安装但被关掉了" "运行 bash install_service.sh start。如果是在 系统设置 > 通用 > 登录项与扩展 > 允许在后台 里关掉了 python3.12，把它打开。" ;;
    not-installed) say "· 没有装成后台服务（bash install_service.sh 安装；或者用 bash studio.sh 临时运行）" ;;
  esac
  if installed_python; then
    local sessions
    sessions="$(previous_arg --sessions)"
    say ""
    "${VENV}/bin/python" "${CODE}/d455_studio.py" --doctor --port "${PORT}" --sessions "${sessions:-${HOME}/D455/sessions}" \
      --log-dir "${LOGS}" || true
  elif [ -x "${SRC}/.venv/bin/python" ]; then
    say ""
    "${SRC}/.venv/bin/python" "${SRC}/d455_studio.py" --doctor --port "${PORT}" --sessions "${SESSIONS:-sessions}" || true
  else
    check_port
    say ""
    say "（Python 环境还没装好，只做了上面这些检查）"
  fi
  if [ -f "${LOGS}/studio.log" ]; then
    say ""
    say "--- 服务日志最后 15 行（${LOGS}/studio.log）"
    tail -n 15 "${LOGS}/studio.log"
  fi
}

case "${COMMAND}" in
  status)
    case "$(service_state)" in
      running) say "已安装，正在运行：${URL}" ;;
      idle) say "已安装，按需启动：现在没有运行，打开 ${URL} 时自动启动。" ;;
      not-loaded) say "已安装，但被关掉了：bash install_service.sh start 打开。" ;;
      not-installed) say "没有安装。" ;;
    esac
    [ -f "${LOGS}/studio.log" ] && { say "--- ${LOGS}/studio.log（最后几行）"; tail -n 15 "${LOGS}/studio.log"; }
    exit 0 ;;
  logs)
    [ -f "${LOGS}/studio.log" ] && tail -n 200 "${LOGS}/studio.log" || say "还没有日志。"
    exit 0 ;;
  doctor)
    doctor; exit 0 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  [ "${COMMAND}" = "install" ] && preflight
  say "需要管理员权限。如果提示 Password，请输入这台 Mac 的登录密码（输入时不显示字符）。"
  [ "${COMMAND}" = "install" ] && say "这是唯一一次：装好以后打开网页就能录，服务自己按需启动和退出，不再要密码。"
  exec sudo /bin/bash "$0" "${COMMAND}" ${SESSIONS:+--sessions "${SESSIONS}"} --port "${PORT}" \
    ${INDEX_URL:+--index-url "${INDEX_URL}"} ${PYTHON_MIRROR:+--python-mirror "${PYTHON_MIRROR}"} --exit-after "${EXIT_AFTER}" \
    ${NO_BROWSER:+--no-browser}
fi
USER_NAME="${SUDO_USER:-}"
[ -n "${USER_NAME}" ] && [ "${USER_NAME}" != "root" ] || die "请在自己的账户里运行 bash install_service.sh（不要先切换到 root）。"
USER_UID="$(id -u "${USER_NAME}")"
USER_GID="$(id -g "${USER_NAME}")"
USER_HOME="$(dscl . -read "/Users/${USER_NAME}" NFSHomeDirectory | awk '{print $2}')"
[ -d "${USER_HOME}" ] || die "找不到 ${USER_NAME} 的个人文件夹。"

stop_service() {
  launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do  # bootout returns before the old process is gone
    launchctl print "system/${LABEL}" >/dev/null 2>&1 || return 0
    sleep 0.25
  done
}
start_service() {
  launchctl enable "system/${LABEL}" >/dev/null 2>&1 || true
  launchctl bootstrap system "${PLIST}" 2>/dev/null || launchctl kickstart "system/${LABEL}" >/dev/null 2>&1 || true
  launchctl print "system/${LABEL}" >/dev/null 2>&1 \
    || die "launchd 没有接受这个服务。端口 ${PORT} 可能被占用（bash install_service.sh doctor 查看），或者在 系统设置 > 通用 > 登录项与扩展 里关掉了它的后台运行。"
}
wait_for_page() {  # the first request also starts the program
  for _ in $(seq 1 120); do
    curl -fsS --max-time 2 "${URL}api/ping" >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  return 1
}
open_page() { [ -z "${NO_BROWSER}" ] || return 0; launchctl asuser "${USER_UID}" sudo -u "${USER_NAME}" open "${URL}" >/dev/null 2>&1 || true; }

case "${COMMAND}" in
  stop)
    stop_service
    launchctl disable "system/${LABEL}" >/dev/null 2>&1 || true
    say "已关闭：端口 ${PORT} 不再监听，重启电脑后也不会再启动，相机完全交给系统。bash install_service.sh start 重新打开。"
    exit 0 ;;
  start|restart)
    [ -f "${PLIST}" ] || die "还没有安装，先运行 bash install_service.sh"
    stop_service; start_service
    if wait_for_page; then say "已打开：${URL}"; open_page; else die "服务注册了，但网页 1 分钟内没有响应"; fi
    exit 0 ;;
  uninstall)
    stop_service
    launchctl enable "system/${LABEL}" >/dev/null 2>&1 || true  # forget an earlier stop
    rm -f "${PLIST}"
    rm -rf "${HOME_DIR}" "${LOGS}"
    say "已卸载。录制的数据没有动，仍在你选的文件夹里（默认 ~/D455/sessions）。"
    exit 0 ;;
esac

# ---------------------------------------------------------------------------------------------- install
STEP="检查文件"
for file in record_d455.py d455_studio.py requirements.txt studio/index.html studio/app.js studio/style.css; do
  [ -f "${SRC}/${file}" ] || die "缺少 ${file}，请在录制器文件夹（有 record_d455.py 的那个）里运行这个脚本。"
done
xcode-select -p >/dev/null 2>&1 && /usr/bin/python3 -c 'import venv' >/dev/null 2>&1 \
  || die "缺 Xcode 命令行工具：终端运行 xcode-select --install，装完再运行这个脚本。"

[ -n "${SESSIONS}" ] || SESSIONS="$(previous_arg --sessions)"
SESSIONS="${SESSIONS:-${USER_HOME}/D455/sessions}"
EXTERNAL=0
case "${SESSIONS}" in /*) ;; "~"/*) SESSIONS="${USER_HOME}/${SESSIONS#\~/}" ;; *) SESSIONS="${USER_HOME}/${SESSIONS}" ;; esac
case "${SESSIONS}/" in
  "${USER_HOME}/Desktop/"*|"${USER_HOME}/Documents/"*|"${USER_HOME}/Downloads/"*|"${USER_HOME}/Library/"*)
    die "macOS 不允许后台服务写入 ${SESSIONS}（桌面、文稿、下载都受保护）。换一个文件夹，例如默认的 ~/D455/sessions。" ;;
  /Volumes/*) EXTERNAL=1 ;;
esac

if [ -f "${PLIST}" ]; then
  stop_service
elif curl -fsS --max-time 1 "${URL}api/ping" >/dev/null 2>&1; then
  die "端口 ${PORT} 上已经有一个 D455 Studio 在运行（大概是 studio.sh 开的终端窗口）。先在那个窗口按 Ctrl+C 关掉，再运行这个脚本。"
fi
owner="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | awk 'NR == 2 { print $1 " (pid " $2 ")" }' || true)"
[ -z "${owner}" ] || die "端口 ${PORT} 被 ${owner} 占着。关掉它，或者换端口：bash install_service.sh --port 8456"

WORK="$(mktemp -d /tmp/d455studio-install.XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT
chmod 700 "${WORK}"

STEP="1/5 准备安装工具"
say "1/5 准备安装工具 uv（只用于这次安装）..."
export HOME="/var/root"  # nothing below may write root-owned files into your home folder
# A throwaway environment from Apple's python3 (root-owned) fetches uv from PyPI; root never runs a file
# that your account could have changed.
/usr/bin/python3 -m venv "${WORK}/boot" || die "创建临时 Python 环境失败。"
"${WORK}/boot/bin/python" -m pip install --quiet --disable-pip-version-check --no-cache-dir \
  ${INDEX_URL:+--index-url "${INDEX_URL}"} uv \
  || die "下载 uv 失败，请检查网络（需要能访问 ${INDEX_URL:-pypi.org}）。"
UV="${WORK}/boot/bin/uv"
[ -x "${UV}" ] || die "uv 安装包里没有找到可执行文件。"
export UV_CACHE_DIR="${WORK}/cache" UV_PYTHON_INSTALL_DIR="${PYROOT}" UV_PYTHON_BIN_DIR="${WORK}/bin" \
       UV_PYTHON_PREFERENCE="only-managed" UV_LINK_MODE="copy" UV_NO_CONFIG=1
[ -n "${INDEX_URL}" ] && export UV_INDEX_URL="${INDEX_URL}" UV_DEFAULT_INDEX="${INDEX_URL}"
[ -n "${PYTHON_MIRROR}" ] && export UV_PYTHON_INSTALL_MIRROR="${PYTHON_MIRROR}"

STEP="2/5 安装服务专用的 Python"
say "2/5 安装服务专用的 Python ${PYTHON_VERSION}（下载约 25 MB，装好约 70 MB，在 ${PYROOT}）..."
mkdir -p "${HOME_DIR}"
"${UV}" python install "${PYTHON_VERSION}" >"${WORK}/uv-python.log" 2>&1 \
  || { cat "${WORK}/uv-python.log"; die "下载 Python 失败，请检查网络（需要能访问 ${PYTHON_MIRROR:-github.com}）。"; }
BASE_PY="$("${UV}" python find "${PYTHON_VERSION}")" || die "装好的 Python 找不到。"
case "${BASE_PY}" in "${PYROOT}"/*) ;; *) die "找到的 Python 不在 ${PYROOT}：${BASE_PY}" ;; esac

STEP="3/5 复制程序并安装依赖"
say "3/5 复制程序并安装依赖..."
rm -rf "${CODE}"
mkdir -p "${CODE}/studio"
for file in record_d455.py d455_studio.py requirements.txt read_session_example.py README.md; do
  [ -f "${SRC}/${file}" ] && cp "${SRC}/${file}" "${CODE}/${file}"
done
cp "${SRC}/studio/index.html" "${SRC}/studio/app.js" "${SRC}/studio/style.css" "${CODE}/studio/"
xattr -cr "${CODE}" 2>/dev/null || true  # a downloaded zip marks every file as quarantined; nothing here needs it
"${UV}" venv --quiet --python "${BASE_PY}" "${VENV}" || die "创建 Python 环境失败。"
if ! "${UV}" pip install --quiet --python "${VENV}/bin/python" -r "${CODE}/requirements.txt"; then
  say "默认版本安装失败，改试 pyrealsense2-macosx 2.54.2（支持 macOS 12 以上和 Intel 芯片）..."
  "${UV}" pip install --quiet --python "${VENV}/bin/python" "pyrealsense2-macosx==2.54.2" numpy opencv-python \
    "mcap>=1.0" "protobuf>=3.20" || die "依赖安装失败，请把上面的报错发给开发者。"
fi

STEP="4/5 编译 IMU 崩溃保护"
say "4/5 编译 IMU 崩溃保护（需要 Xcode 命令行工具）..."
guard="$(env -u SUDO_UID -u SUDO_GID -u SUDO_USER "${VENV}/bin/python" -c '
import sys; sys.path.insert(0, sys.argv[1])
import record_d455 as rd
path = rd.prepare_macos_hid_guard()
print(path if path else "FAILED: " + rd.HID_GUARD_STATE["note"])' "${CODE}")"
case "${guard}" in
  FAILED:*) die "${guard#FAILED: }。没有它，SDK 以管理员身份打开相机时会崩溃（终端运行 xcode-select --install 后重试）。" ;;
esac
"${VENV}/bin/python" -c "import numpy, cv2, mcap, pyrealsense2" || die "依赖装好了但导入失败，请把上面的报错发给开发者。"

chown -R root:wheel "${HOME_DIR}"
chmod -R u+rwX,go+rX,go-w "${HOME_DIR}"
mkdir -p "${LOGS}" && chown root:wheel "${LOGS}" && chmod 755 "${LOGS}"
sudo -u "${USER_NAME}" mkdir -p "${SESSIONS}" || die "不能创建 ${SESSIONS}"

STEP="5/5 注册服务"
say "5/5 注册服务（按需启动）..."
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
cat > "${PLIST}.new" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "${VENV}/bin/python")</string>
    <string>$(xml "${CODE}/d455_studio.py")</string>
    <string>--no-browser</string>
    <string>--service</string>
    <string>--launchd-socket</string><string>Listeners</string>
    <string>--exit-after</string><string>${EXIT_AFTER}</string>
    <string>--port</string><string>${PORT}</string>
    <string>--sessions</string><string>$(xml "${SESSIONS}")</string>
    <string>--settings</string><string>$(xml "${SETTINGS}")</string>
    <string>--log-dir</string><string>$(xml "${LOGS}")</string>
  </array>
  <!-- On demand: launchd keeps the port open and starts the program on the first connection. The program
       exits by itself when no page has been open for a while, and nothing runs until the next visit. -->
  <key>Sockets</key>
  <dict>
    <key>Listeners</key>
    <dict>
      <key>SockNodeName</key><string>127.0.0.1</string>
      <key>SockServiceName</key><string>${PORT}</string>
      <key>SockType</key><string>stream</string>
      <key>SockFamily</key><string>IPv4</string>
    </dict>
  </dict>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- record_d455 gives new recordings to this user (it reads the variables sudo would set) -->
    <key>SUDO_UID</key><string>${USER_UID}</string>
    <key>SUDO_GID</key><string>${USER_GID}</string>
    <key>SUDO_USER</key><string>$(xml "${USER_NAME}")</string>
    <key>HOME</key><string>/var/root</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PYTHONUTF8</key><string>1</string>
  </dict>
  <key>WorkingDirectory</key><string>$(xml "${CODE}")</string>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$(xml "${LOGS}/studio.log")</string>
  <key>StandardErrorPath</key><string>$(xml "${LOGS}/studio.log")</string>
</dict>
</plist>
PLIST_END
plutil -lint "${PLIST}.new" >/dev/null || die "生成的 plist 有误。"
chown root:wheel "${PLIST}.new" && chmod 644 "${PLIST}.new" && mv -f "${PLIST}.new" "${PLIST}"
start_service

webloc="$(dirname "${SESSIONS}")/D455 Studio.webloc"
sudo -u "${USER_NAME}" /bin/sh -c 'cat > "$1"' sh "${webloc}" <<WEBLOC_END || true
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>URL</key><string>${URL}</string></dict></plist>
WEBLOC_END

STEP="启动检查"
wait_for_page || die "服务注册了，但网页 1 分钟内没有响应。日志：bash install_service.sh logs"
say ""
say "自检："
sudo -u "${USER_NAME}" "${VENV}/bin/python" "${CODE}/d455_studio.py" --doctor --port "${PORT}" --sessions "${SESSIONS}" \
  --log-dir "${LOGS}" | sed -n '3,$p' || true
say ""
say "装好了。以后打开 ${URL} 就能看画面和录制，不需要再输密码（建议把这个网址加到书签）。"
if [ "${EXIT_AFTER}" -ge 120 ]; then wait_text="$((EXIT_AFTER / 60)) 分钟"; else wait_text="${EXIT_AFTER} 秒"; fi
say "服务按需运行：打开网页时自动启动，网页关掉 ${wait_text}后自动退出，平时不占 CPU 和内存。"
say "录制保存在 ${SESSIONS} ；快捷方式：${webloc}"
if [ "${EXTERNAL}" = 1 ]; then
  say ""
  say "录到外接盘还差一步：系统设置 > 隐私与安全性 > 完全磁盘访问权限，点 +，按 Command+Shift+G 输入下面这个路径并添加，"
  say "然后运行 bash install_service.sh start："
  say "  $(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${BASE_PY}")"
fi
say "出问题时：bash install_service.sh doctor    关掉服务：bash install_service.sh stop    卸载：bash install_service.sh uninstall"
open_page
