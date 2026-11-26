# D455 单机录制脚本

把一台 Intel RealSense D455 用 USB 线接到电脑上，运行一条命令，就把深度、彩色、左红外三路数据连同逐帧时间戳存到本地。一次只接一台相机，三台相机靠序列号区分，各自生成独立的 session 目录。

```
bash run.sh
```

按 `Ctrl+C` 结束。结束时自动生成一份体检报告，告诉你丢了多少帧、三路数据的时间差是多少。

## 网页界面 D455 Studio

不想敲命令时用这个：实时看彩色、深度、红外三路画面，在网页上开始和停止录制、调相机参数，回放录过的 session（三路按时间同步像视频一样播放，可以拖时间轴、逐帧看、把鼠标放在深度图上读距离）。

### 装到一台新电脑

**Mac**（装一次，以后插上相机、打开网页就能录，不再要密码）：

```
xcode-select --install       # 只有第一次需要，在弹窗里点"安装"，约 5 分钟
git clone https://github.com/Dongzl-2117/d455-recorder.git
cd d455-recorder
bash install_service.sh      # 先检查这台 Mac，再输一次开机密码，约 2 分钟，装完自动打开网页
```

安装脚本动手之前先检查：macOS 版本（12 以上）、芯片、终端是否在 Rosetta 下、账户是不是管理员、Xcode 命令行工具、能否访问 pypi.org 和 github.com、磁盘空间、端口 8455、录像目录的位置。有问题会先列出原因和办法，什么都不改动。网络屏蔽 pypi.org 或 github.com 时可以用镜像：`--index-url` 和 `--python-mirror`。

以后直接打开 http://127.0.0.1:8455/ （建议加书签，`~/D455` 里也有一个快捷方式）。更新：在这个文件夹里 `git pull`，再运行一次 `bash install_service.sh`（会再要一次密码，因为服务的程序归管理员所有）。仓库是私有的，对方需要先被加为协作者，或者把整个文件夹拷给他。

**Windows**：装好 Python 3.9 以上（安装时勾选 Add python.exe to PATH），双击 `studio.bat`。第一次会自动装依赖，之后浏览器自动打开网页。Windows 打开相机不需要管理员权限。

**Linux**：`bash studio.sh`。Ubuntu 和 Debian 先装 `sudo apt install python3-venv`。和命令行录制一样，需要先装一次 RealSense 的 udev 规则，普通用户才能打开相机：

```
sudo curl -fsSL https://raw.githubusercontent.com/IntelRealSense/librealsense/master/config/99-realsense-libusb.rules -o /etc/udev/rules.d/99-realsense-libusb.rules
sudo udevadm control --reload-rules && sudo udevadm trigger     # 然后拔插一次相机
```

RealSense 的 Python 包只发布了 x86 电脑的版本；树莓派、Jetson 这类 ARM 的 Linux 没有现成的包。安装失败时脚本会说明是版本不对还是网络不通。

不装服务也可以临时用：Mac 上 `bash studio.sh`，每次启动输一次密码。没有相机时 `bash studio.sh --simulate` 可以先看界面。

### 出问题时

- Mac 装了服务的：`bash install_service.sh doctor`
- 其他情况：`bash studio.sh --doctor`（Windows：`studio.bat --doctor`）
- 网页里：右上角设置 > 自检。实时页等不到相机超过几秒，也会直接在画面上写出原因。

检查的内容：系统版本和芯片、Python 和 SDK 版本、IMU 崩溃防护、系统自己有没有识别到相机（和 SDK 无关）以及 USB 速度（USB 2 会提示换线）、USB-C 口检测到插头却没有数据连接（充电线）、相机是否被别的程序或 macOS 相机服务占着、固件是否旧于 SDK 推荐、上一次打开相机是否卡死过、磁盘还能录多久、端口是否被占用、后台服务是否被关掉、服务日志里出现过的已知错误。每一条问题都写明原因和办法。

### 看别处拷来的录像，删掉不要的

- 回放页左上角的「导入」：选一个录像文件夹（里面有 `recordings` 的那种，或者装着多个录像的上一级文件夹），或者直接选 `.mcap` 文件；也可以把它们拖到列表上。会复制一份到录像目录，原文件不动。网页是通过浏览器读这些文件的，所以桌面、下载、U 盘里的都能导入，不受 macOS 对后台服务的限制。只复制录像数据（`.mcap`、`.json`、`.csv`、图片等），程序和脚本一类的文件不复制；`derived/export` 里导出的单帧图片也不复制，需要时用 `export` 重新生成。
- 也可以在访达里直接把录像文件夹或 `.mcap` 文件拷进录像目录（设置 > 存储 > 在访达中打开），刷新列表就出现；放在下一级文件夹里的也认。要直接拷贝，不要放替身（符号链接）：替身不显示，也不会被删除。
- 没有 `manifest.json` 的单个 `.mcap` 文件，日期和时长从文件本身读出来。
- 删除：回放页的「选择」可以多选，或者在播放页右上角删当前这个。删除前会列出要删的录像和总大小再确认一次；正在录制的不能删。删除是直接从磁盘删掉，不经过废纸篓（后台服务没有权限使用废纸篓）。

### Mac 后台服务需要知道的几点

- 录制保存到 `~/D455/sessions`。macOS 不允许后台服务写入桌面、文稿和下载，所以不能存到 Downloads 下面；以前录的留在原处，命令行照常能读。换位置：`bash install_service.sh --sessions 其他文件夹`。录到外接盘（`/Volumes/...`）需要在"系统设置 > 隐私与安全性 > 完全磁盘访问权限"里加上服务自带的 Python，安装脚本会打印它的路径。
- 服务的程序、它自带的 Python 和依赖装在 `/Library/D455Studio`，归管理员所有，你的账户改不了。原因是这些代码以管理员权限运行，如果普通账户能改，任何以你身份运行的程序都能借它拿到管理员权限。
- 网页只有这台 Mac 自己能打开（127.0.0.1），局域网里的其他电脑连不上。别的网站也调不动它：服务只接受这个网页自己发出的请求。
- 服务以管理员身份运行，所以录像目录必须是你自己账户下的普通文件夹。如果它或它的上级被换成替身（符号链接），或者不归你所有，服务会停用录制、导入和删除，自检里写明原因。原因是：管理员程序如果跟着替身写文件、改文件归属，以你身份运行的任何程序都能借它改动系统文件。
- 正在录制时，`stop`、更新（再运行一次 `bash install_service.sh`）和 `uninstall` 会拒绝执行，提示先在网页里停止录制。确实要马上停，加 `--force`，录制会被收尾保存。
- 服务按需运行：开机后它并不运行，只有 macOS 自带的 launchd 守着 8455 端口。打开网页时才启动（多等两三秒），网页全部关掉 5 分钟后、且没有在录制时自动退出，退出后不占 CPU 和内存。注意：相机被这个服务或 `run.sh` 接管过以后，macOS 不会自己收回，Photo Booth、视频会议这类 Mac 自带的相机应用要等拔插一次相机才能再用它。网页在后台标签页里时不发任何请求，所以忘记关的标签页也不会让它一直运行。设置里的「关闭后台服务」可以马上退出。
- 网页打开期间：一分钟没人看实时画面、也没在录制时，相机暂停出图（红外投射器也随之关闭），回到实时页几秒后恢复。只看回放时不会去碰相机。相机拔掉再插上会自动重新连接。设置里有"硬件复位"（等于拔插一次）和"重启后台服务"，相机卡住时用。
- 服务运行时相机归它用。要用 `bash run.sh` 在命令行录制，先在网页设置里点「关闭后台服务」（或者关掉网页等 5 分钟）。`bash install_service.sh stop` 是彻底关掉，重启电脑后也不会再监听端口，`start` 恢复；这两条要输密码。`status` 看状态，`logs` 看日志，`uninstall` 卸载（录制数据保留）。
- macOS 13 以后，装好时系统可能提示"已添加后台项目"。如果在 系统设置 > 通用 > 登录项与扩展 里把它（显示为 python3.12）关掉，服务就起不来，`doctor` 会提示这一点。

网页录出来的 session 和命令行录的完全一样（同一套写盘和体检代码），`verify`、`repair`、`export` 照常可用；没有正常结束的 session 在回放页会提示，一键修复。

## 先说清楚：哪些验证过，哪些没有

| 项目 | 状态 |
|---|---|
| 采集、编码、写盘、读回、对齐分析全流程 | 已用官方 `rs.software_device` 模拟的 D455 跑通，SDK 2.59.2、2.56.5、2.54.2 和 Python 3.9、3.11、3.13 都通过；不经过 SDK 的 Python 模拟帧（Mac 自测用）在同样组合下也通过 |
| 与现有标注工具兼容 | 已用 `thermal-tof-ZhilinHome` 里的 `session_reader.py` 打开模拟 session，彩色、深度、红外通道和 meta 时间线都能读到 |
| Ctrl+C、强杀进程后恢复、相机断流、启动失败重试、磁盘写不过来 | 已用模拟相机逐项测试 |
| Mac 上安装 | 2026-11-26 在 macOS 27.2.1 + Apple 芯片上装好，`pyrealsense2-macosx` 2.56.5 |
| 真实 D455 接在 Mac 上录制 | **已录成**（2026-11-26，1.2.8，60 秒）：体检 PASS；深度、彩色、红外都是 29.97 fps，相机端和脚本端丢帧 0；彩色和深度的时间戳差中位数 0.008 ms、最大 1.1 ms，红外和深度为同一时刻 |
| 网页界面 D455 Studio | 用模拟相机跑通实时预览、录制、调参、回放、导入、删除、强杀后修复、按需启动和自动退出（`tests/`，GitHub 上在 Ubuntu、Windows、macOS 自动跑）；Mac 后台服务真机实测（2026-11-26，macOS 27.2.1）：三路出图时约 10% CPU、130 MB 内存；关网页 5 分钟后自动退出，之后 0 占用；没在运行时打开网页 0.4 秒响应，约 9 秒出图 |

Mac 是 RealSense 官方支持最弱的平台。官方安装文档原话是 "macOS support for the full range of functionality offered by the SDK is not yet complete"。2026-11-26 在 Mac 上实际遇到的问题和脚本的对应处理：

| 现象 | 原因 | 脚本现在的处理 |
|---|---|---|
| 不带管理员权限运行时进程直接崩溃（Segmentation fault） | SDK 没有 USB 权限时，第二次枚举设备会在 libusb 内部崩溃 | 需要相机的命令自动通过 `sudo` 重新运行 |
| SDK 一列出相机就崩溃（`IOHIDDeviceOpen`，访问地址 0x30），线和连接都正常时也每次复现 | SDK 自带的 hidapi（`hid_open_path`）在 macOS 给不出 IMU 的 HID 句柄时，仍拿空句柄去调用 `IOHIDDeviceOpen`。社区版 2.54.2 和 2.56.5 是同一段代码，换版本没用；相机掉线重连的瞬间也会走到这里 | 脚本在 Mac 上编译一个几十行的防护库 `.venv/hid_guard_arm64.dylib`，以管理员权限重启时注入进程，把这次空句柄调用改成返回错误。结果是 IMU 不可用，深度、彩色、红外不受影响。编译需要 Xcode 命令行工具。另外仍会先等 macOS 稳定识别相机 3 秒，再让 SDK 接触它 |
| 彩色时有时无：同一种打开顺序这次三路都出帧，下次 0 帧；只开彩色总是 0 帧；偶尔在 `libusb_get_device_list` 里崩溃 | SDK 每释放一个 USB 接口，它内置的 libusb 就可能把整台相机交还给 macOS（重新枚举），系统摄像头服务 UVCAssistant 随即抢回彩色接口，SDK 手里其余通道失效（`pipe ref not recognized`）。libusb 在 macOS 上按整台设备计数，接管和交还的次数对不上 | 1.2.4 起，录制开始前脚本先用同一份 libusb 把相机整体接管一次，并把计数抬到 SDK 在一次录制里放不完的高度，录制期间不再交还。录制开头会打印一行 `USB hold: active`。实测（2026-11-26 21:32）交还次数为 0，没有再崩溃。接管后系统不会自动收回相机，拔插一次才会还给 macOS |
| 相机刚从系统手里接管过来时，第一次打开彩色收不到帧（深度和红外正常） | 系统摄像头服务留下的状态，具体原因未查明。第二次打开就正常，之后在同一次插入期间都正常 | 1.2.5 起，开始录制时脚本先确认每一路都出了第一帧（这段不写盘），有一路 5 秒内没出就全部停下重开，最多 3 次；仍不行就按 `--start-retries` 从头重试，第 2 次起会给相机发硬件复位。终端里会看到一行 `no data from color ... starting all streams again`，属于正常 |
| 录制卡在相机信息那几行，三路已经开起来，之后既不写盘也不能 Ctrl+C | SDK 2.56.5 的缺陷，只在 macOS 上出现：Python 调 `get_supported_options()`（列出传感器支持哪些参数）时，SDK 给相机接口的供电计数加一次、减两次。正在出帧的传感器计数被减到 0，SDK 就去关这个接口；关闭要等帧回调结束，帧回调要等 Python 锁，而锁在调用方手里，形成死锁。Linux 和 Windows 上不会发生，Intel 的最新源码已经修掉 | 1.2.8 起不再调用这个函数，改为逐个询问 `supports()` 再读值，这条路径的计数是平衡的 |
| `selftest` 崩溃（malloc double free） | 社区版 SDK 2.56.5 的模拟相机注入帧时内部出错，40 行的最小程序也能复现 | Mac 上的自测改用 Python 生成的模拟帧，录制、编码、写盘、读回、对齐分析走的是同一套代码 |

如果防护库在 Mac 上也挡不住，剩下两条路：在 Mac 上从源码编译一份修好这处的 SDK，或者用一台 Windows / Ubuntu 电脑录，脚本和数据格式完全一样。

## 第一次在 Mac 上要做的四件事

1. **自测安装**（不需要相机，约 10 秒）：

   ```
   bash run.sh selftest
   ```

   首次运行会自动创建 `.venv` 并安装依赖，需要联网。最后一行出现 `SELF-TEST PASSED` 说明软件环境没问题。

2. **确认线是 USB 3**：用 USB 3 数据线把 D455 直接插到 Mac 上（不要经过扩展坞），等 10 秒，然后

   ```
   system_profiler SPUSBHostDataType | grep -i -A9 RealSense
   ```

   要能看到 RealSense，并且速度那一行是 `5 Gb/s` 或更高（D455 是 USB 3.1 Gen 1，5 Gb/s 就是它的上限）。显示 `480 Mb/s` 说明线只有 USB 2 线芯，换线；什么都不显示说明数据线没接通。D455 的 USB-C 口比外壳凹进去一截，插头没推到底时也会什么都不显示，先拔下重插到底再判断线。这一步不涉及脚本和 SDK。

3. **诊断**（约 1 分钟）：

   ```
   bash run.sh diagnose
   ```

   会要求输入 Mac 登录密码，原因见"常见问题"。它记录 macOS 看到的 USB 状态，再在三个单独的进程里各测一次：录制时的打开顺序（有一路没出帧时像录制那样重开一次）、同一进程里停下再开一遍、只开彩色，每次拉 5 秒并按路数帧，最后的 `summary` 和 `RESULT:` 给出结论。每个测试先接管相机（报告里 `USB hold: active`），`summary` 里每行末尾的三个 libusb 计数（交还 macOS、SDK 自己接管、失效通道）正常都是 0。两次测试之间会等 macOS 把相机重新挂载完。USB 库 libusb 自己的日志存在 `logs/diagnose_<时间>_libusb_<n>.txt`。报告开头一行 `IMU crash guard:` 应该是 `active`，原因见上表第二行。报告存在 `logs/diagnose_<时间>.txt`，SDK 自己的调试日志和崩溃现场也在 `logs/` 里。

4. **试录 1 分钟并看报告**：

   ```
   bash run.sh --duration 60 --preview
   ```

   `--preview` 会开一个窗口显示彩色、深度、红外，方便对准画面。结束后看报告第一行是 `[PASS]`、`[WARN]` 还是 `[FAIL]`。把 `sessions/<session>/derived/verify_report.json` 发给我，我可以根据真实数据调整默认参数。

## 环境要求

| | 要求 |
|---|---|
| macOS | macOS 15 + Apple 芯片用 `pyrealsense2-macosx` 2.56.5；macOS 12 到 14 或 Intel Mac 会自动退到 2.54.2（需要 Python 3.9 到 3.12） |
| Mac 上的 Xcode 命令行工具 | 编译防护库要用，只编译一次。终端运行 `xcode-select -p` 能打印出路径就是装好了；没有的话运行一次 `xcode-select --install` |
| Windows / Linux | 官方 `pyrealsense2`，pip 会按 Python 版本自动选对应的 SDK 版本 |
| Python | 3.9 或更新 |
| 线和口 | USB 3 口 + USB 3 数据线（标 5Gbps、10Gbps 或雷电）。默认三路流的原始数据约 735 Mb/s（深度 195、彩色 442、红外 98），USB 2 标称 480 Mb/s，实际可用约 300 Mb/s，装不下。很多 USB-C 对 USB-C 的充电线只有 USB 2 线芯 |
| 磁盘 | 实测（室内、默认设置）约 15 MB/s，即每小时约 54 GB：深度 PNG 5.8、彩色 JPEG 6.0、红外 JPEG 3.3 MB/s。画面细节多时会更大。录制时状态行实时显示 MB/s。剩 70 GB 的磁盘约能录 1 小时出头，长时间录制建议录到外接 SSD（`--out /Volumes/<盘名>/sessions`）；不需要红外时去掉它能省约 22% |

官方没有 macOS 的 pip 包，Mac 上用的是社区编译的同一份 SDK（[cansik/pyrealsense2-macosx](https://github.com/cansik/pyrealsense2-macosx)），导入名仍是 `pyrealsense2`。`run.sh` 会自动选对的包。

## 常用命令

Mac 和 Linux 用 `bash run.sh`，Windows 用 `run.bat`，后面的参数相同。

| 命令 | 作用 |
|---|---|
| `bash run.sh` | 等相机插入，录到按 `Ctrl+C` |
| `bash run.sh --duration 600` | 录 10 分钟自动停 |
| `bash run.sh --preview` | 录制时显示实时画面，按 `q` 或 `Esc` 也能停 |
| `bash run.sh --loop` | 录完一台后等下一台插入，适合三台相机轮流录 |
| `bash run.sh --location "3F 走廊" --note "相机A 高2.6m"` | 把场景信息写进 manifest |
| `bash run.sh selftest` | 不接相机的全流程自测 |
| `bash run.sh info` | 列出已连接的相机和可用分辨率 |
| `bash run.sh diagnose` | 找不到相机或拉不到帧时，生成一份完整的诊断报告到 `logs/` |
| `bash run.sh verify sessions/<session>` | 重新生成体检报告，加 `--deep` 会逐条重读 MCAP |
| `bash run.sh export sessions/<session>` | 导出成 PNG/JPG 图片 + CSV，加 `--every 30` 每 30 帧取一帧 |
| `bash run.sh repair sessions/<session>` | 进程被强杀或断电后，修复没有正常关闭的 session |
| `bash run.sh --simulate --duration 10` | 用模拟相机走一遍录制流程，熟悉输出 |

录制时的状态行：

```
00:01:23 | depth 30.0 | color 30.0 | ir_left 30.0 | lost 0/0 | color-depth +0.42 ms | buf 3 MB | 14.8 MB/s | free 412 GB
```

依次是已录时长、各路实际帧率、丢帧数（相机或 USB 丢的 / 本机来不及写丢的）、彩色与深度最近一帧的时间差、内存里排队的数据量、写盘速度、磁盘剩余。

## 录下来的数据

```
sessions/2026-11-26_17-30-05_d455_<序列号>/
  manifest.json                     设备信息、各路分辨率、内参、外参、相机选项、统计
  recordings/<session>_000.mcap     原始数据，每 10 分钟切一个文件
  derived/frame_index.csv           每帧一行，含全部时间戳
  derived/alignment.csv             以深度帧为基准，每帧对应的彩色帧、红外帧及时间差
  derived/verify_report.json        体检报告
  annotations/                      预留给标注
```

目录结构、`manifest.json` 的字段、topic 命名都沿用现有 ToF/IRA session 的约定。`recordings/` 是唯一的原始数据，`derived/` 里的文件都可以用 `verify --deep` 从 MCAP 重新生成。

MCAP 里的 topic（`<dev>` 形如 `d455_<序列号>`）：

| topic | 内容 | 编码 |
|---|---|---|
| `/<dev>/camera/jpeg` | 彩色 1280x720，JPEG 质量 90 | `foxglove.CompressedImage` |
| `/<dev>/depth/png` | 深度 848x480，16 位 PNG，无损，存的是相机原始 Z16 值 | `foxglove.CompressedImage` |
| `/<dev>/ir_left/jpeg` | 左红外 848x480，8 位灰度 | `foxglove.CompressedImage` |
| `/<dev>/{camera,depth,ir_left}/info` | 每帧的时间戳、曝光、增益和全部相机元数据 | `realsense.FrameInfo` |
| `/<dev>/{camera,depth,ir_left}/calibration` | 内参，每个文件开头一条 | `foxglove.CameraCalibration` |
| `/<dev>/meta` | 每个深度帧一条，标注工具用它做主时间线 | JSON `sense.FrameMeta` |

深度换算成米：`像素值 × depth_scale_m`，D455 默认是 0.001，即像素值的单位是毫米，0 表示该点没有测到。

左红外和深度出自同一个成像器，像素一一对应、曝光时刻相同。红外投射器开着的时候（默认），红外图上能看到点阵。

## 时间戳是怎么对齐的

**原理。** D455 的深度、红外、彩色都由相机内部同一个硬件时钟打时间戳，所以三路之间的相对时间由硬件保证，和 USB 传输延迟、电脑负载无关。SDK 的 global time 功能持续把这个硬件时钟拟合到电脑的系统时间，给出 epoch 毫秒。脚本采用官方工具 `rs-data-collect` 的底层回调方式采集，每帧原样落盘，没有经过会丢帧或重复帧的同步器。

**每帧存了四个时间**，都在 `frame_index.csv` 和 `info` topic 里：

| 字段 | 含义 | 用途 |
|---|---|---|
| `timestamp_ms` / MCAP `log_time` | 硬件时钟换算成的 epoch 时间（读出开始时刻），由 SDK 换算 | 三路之间对齐默认用这个。注意它对电脑时钟的绝对值不稳：Mac 实测开流后头约 4 秒偏差可达 100 ms，之后仍会慢慢移动几毫秒。要跨设备对绝对时间时，用体检报告 `device_clock.host_clock_line` 从 `hw_timestamp_us` 换算（实测残差 p99 0.2 ms） |
| `hw_timestamp_us` | 相机硬件时钟原始值，微秒 | 三路之间最精确的相对时间。32 位计数器，约 71.6 分钟回绕一次，分析代码已处理 |
| `sensor_timestamp_us` | 曝光中点的硬件时钟值 | 需要知道画面真正的拍摄时刻时用 |
| `host_receive_epoch_ns` / MCAP `publish_time` | 电脑收到这帧的系统时间 | 排查延迟和时钟问题 |

**深度和彩色的同步条件。** RealSense 官方支持给出的条件是两路帧率相同，并且关闭彩色的 Auto-Exposure Priority。脚本默认两路都是 30 fps，启动时自动把这个选项设为 0。副作用是暗环境下彩色画面会偏暗，因为相机不再为了提亮而降帧率。

**脚本不假设同步成立，它会测。** 每次录制结束，报告里有一行：

```
color vs depth: matched 1800 unmatched 0  |dt| median 0.4 ms p95 0.6 max 1.1  device clock |dt| median 0.4 p95 0.6
```

`device clock |dt|` 是用硬件时钟原始值算出的彩色帧与最近深度帧的时间差。30 fps 下超过 8.3 ms（四分之一帧）会给出 WARNING。`alignment.csv` 里有逐帧的数值。

**对齐方式是"最近邻配对"，数据本身不重采样。** 以深度帧为基准，每个深度帧找时间最近的彩色帧和红外帧，时间差超过半帧（16.7 ms）就算没配上。

**一个你需要知道的边界。** 这套时间戳保证的是 D455 内部三路对齐，以及 D455 与这台电脑系统时钟对齐。它和热成像、ToF 数据的对齐，前提是那些设备也记录了同一台电脑（或已校时电脑）的 epoch 时间，并且同时在录。分开录的数据之间没有逐帧对应关系。

## 读取数据

**Python。** 把 `record_d455.py` 放在同一目录，直接用里面的 `Session`：

```python
from record_d455 import Session

session = Session("sessions/2026-11-26_17-30-05_d455_xxxx")
for item in session.aligned_frames():            # 每个深度帧一组
    depth_m = item["depth"] * session.depth_scale  # 米，0 表示无测量
    color = item["color"]                          # BGR，没配上时为 None
    ir = item["ir_left"]
    t = item["timestamp_ms"]                       # epoch 毫秒
    dt = item["delta_ms"]                          # 各路相对深度帧的时间差
```

完整示例见 `read_session_example.py`，加 `--show` 可以回放。内参在 `session.manifest["streams"]["depth"]["intrinsics"]`，外参在 `session.manifest["extrinsics"]`。

**不想碰 MCAP。** `bash run.sh export sessions/<session>` 会在 `derived/export/` 下导出逐帧图片和带时间戳的 CSV，图片字节与录制时完全一致，不重新压缩。

**Foxglove。** 把 `.mcap` 拖进 Foxglove 即可看彩色和红外。16 位深度图在普通图片查看器里几乎全黑，这是正常的，数值都在。

**现有标注工具。** session 目录可以直接打开，彩色在 `/<dev>/camera/jpeg`。

## 主要参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--streams` | `depth,color,ir_left` | 可选 `ir_right`、`imu`。至少要有一路图像 |
| `--fps` | 30 | 深度、红外、彩色共用，保持一致是同步条件之一 |
| `--depth-size` | 848x480 | D455 深度的最佳分辨率。红外跟随深度 |
| `--color-size` | 1280x720 | |
| `--ir-codec` | jpeg | `png` 为无损，体积约 4 倍 |
| `--color-codec` | jpeg | `png` 为无损，体积很大 |
| `--jpeg-quality` | 90 | |
| `--split-minutes` | 10 | 每隔多久切一个 MCAP 文件 |
| `--emitter` | keep | `on` / `off` 控制红外投射器。关掉后红外图没有点阵，但白墙等无纹理表面的深度会变差 |
| `--set` | | 设置任意 SDK 选项，例如 `--set depth.laser_power=150`、`--set color.power_line_frequency=1`（50 Hz 防频闪） |
| `--out` | `./sessions` | 输出根目录 |
| `--serial` | | 同时插了多台时指定用哪台 |
| `--start-retries` | 3 | 相机启动失败时的重试次数 |
| `--max-buffer-mb` | 1024 | 磁盘跟不上时内存里最多排多少数据，超过才丢帧 |
| `--min-free-gb` | 2 | 磁盘剩余低于这个值自动停止并正常收尾 |

`python record_d455.py --help` 有完整列表。

关于 IMU：它是相机里的加速度计和陀螺仪，记录相机自身的姿态和晃动。相机固定安装时用处很小，而且 macOS 上 IMU 是已知的不稳定来源，所以默认不录。需要时加 `--streams depth,color,ir_left,imu`，启动不了会自动跳过并给出提示。

## 常见问题

**为什么 Mac 上要输入密码？** 官方安装文档写明 macOS 12 及以后，使用 libusb 的 RealSense 程序必须以管理员权限运行，原因是 macOS 的 USB 安全机制和系统自带 UVC 驱动的占用。`run.sh` 只在需要访问相机的命令上加 `sudo`。录完的文件会自动改回你的用户所有；如果进程被强杀导致文件属于 root，运行 `sudo chown -R $USER sessions`。

**提示 `macOS does not list a RealSense device on USB yet`。** macOS 自己都没识别到相机，和脚本、SDK 无关。按第 2 步检查线。Mac 上脚本会先等 macOS 稳定识别相机 3 秒，才让 SDK 打开它，因为在相机掉线重连的瞬间打开会让 SDK 崩溃。

**`failed to set power state` / `failed to claim usb interface` / SDK 找不到相机。** 脚本会自动重试 3 次，第 2 次起会给相机发硬件复位。仍然失败时按顺序排查：确认是通过 `run.sh` 运行（带 sudo）；退出所有可能占用摄像头的程序（浏览器视频标签页、微信或 Zoom 视频、Photo Booth）；拔掉相机等 5 秒再插；运行 `bash run.sh diagnose`，把 `logs/` 里最新的 `diagnose_*` 文件发给我。

**显示 USB 2.x 或 480 Mb/s。** 线或口不支持 USB 3。很多 USB-C 充电线只有 USB 2 数据能力。USB-C 插头正反插有时结果不同，可以翻面试一次。

**诊断报告里写着 `IMU crash guard: NOT active`。** 后面会跟原因。最常见的是没装 Xcode 命令行工具，运行一次 `xcode-select --install` 再重跑 `bash run.sh diagnose`。如果写的是 `injected but did not load`，说明这个 Python 不接受注入，把报告发给我。设置环境变量 `D455_NO_HID_GUARD=1` 可以关掉防护，用于对比测试。

**录制停在相机信息那几行不动。** 1.2.8 修掉了已知的原因（见上面 Mac 问题表）。如果还会发生：什么都不用做，等它自己结束。1.2.7 起，启动 40 秒还没完成时，脚本会自动给 SDK 内部拍一张快照（`sessions/<session>/derived/start_hang_sample.txt`，仅 macOS），并把各线程当时在做什么写进同一目录的 `start_hang_stacks.txt`；150 秒时脚本自己退出。这几步都不依赖 Python 主线程，所以 SDK 卡死在底层时也能完成。终端里可能看到一行 `starting the camera has not finished`，也可能什么都没有，两种情况都会留下文件。之后拔插一次相机再重跑，并把这两个文件发给我。

**录制或诊断时打印 `USB hold: NOT active`。** 后面会跟原因。`it needs administrator rights` 说明没有通过 `run.sh` 运行；`macOS did not hand the camera over` 时先退出所有可能用摄像头的程序再试。接管不成功时脚本照常继续，只是彩色可能时有时无。加 `--no-usb-hold` 可以关掉接管，用于对比测试。

**Mac 上的 `selftest` 为什么不用 SDK 的模拟相机。** 社区版 SDK 2.56.5 在模拟相机注入帧时会在内部崩溃，和这台 Mac 的设置无关。Mac 上自测改用 Python 生成的帧，录制、编码、写盘、读回、对齐分析用的是和真机录制同一套代码，只是帧不经过 SDK。Windows 和 Linux 上仍用 SDK 的模拟相机，`selftest --engine python` 或 `--engine sdk` 可以手动指定。

**报告里有丢帧。** 看状态行的 `lost a/b`。`a` 是相机或 USB 链路丢的，换线、换口、去掉扩展坞；`b` 是本机编码写盘来不及丢的，关掉其他占 CPU 的程序，或用 `--fps 15`。零星几帧（低于 1%）不会触发警告。

**报告里时间戳域是 `system_time`，结果为 FAIL。** 说明逐帧元数据没有到达 SDK，时间戳退化成了电脑收到数据的时刻，精度只有 USB 抖动水平。Mac 和 Linux 上一般不会出现；Windows 上需要以管理员身份运行一次 `python record_d455.py enable-metadata`，然后重新插拔相机。

**录制中途程序崩溃或电脑断电。** 数据边录边写盘，程序崩溃最多丢最后约 1 秒，断电最多丢最后几秒。下次运行时脚本会提示有未关闭的 session，运行 `bash run.sh repair sessions/<session>` 修复索引并补全报告。修复要先写一份和原文件差不多大的副本，所以需要那么多剩余空间（10 分钟一段约 9 GB）；空间不够、或者读写出错时，修复会停下并说明原因，原文件保持不动，可以腾出空间后再修。

**合上笔记本盖子。** 脚本会阻止系统空闲休眠，但合盖仍会休眠并中断录制。长时间录制请保持开盖并接电源。

**连续录很久。** 没有时长上限。文件每 10 分钟切分一次，硬件时钟每 71.6 分钟的回绕已处理。

## 设计依据

- 采集方式：官方工具 [rs-data-collect](https://github.com/realsenseai/librealsense/tree/master/tools/data-collect) 的底层 sensor 回调模式，以及官方 Python 示例 [wrappers/python/examples](https://github.com/realsenseai/librealsense/tree/master/wrappers/python/examples)
- 时间戳含义：[doc/frame_metadata.md](https://github.com/realsenseai/librealsense/blob/master/doc/frame_metadata.md) 和 SDK 源码 `src/global_timestamp_reader.cpp`
- 深度与彩色同步条件：[librealsense issue #11669](https://github.com/IntelRealSense/librealsense/issues/11669) 中 RealSense 支持的答复
- macOS 权限要求：[doc/installation_osx.md](https://github.com/realsenseai/librealsense/blob/master/doc/installation_osx.md)
- macOS 稳定性报告：[issue #14325](https://github.com/realsenseai/librealsense/issues/14325)、[issue #14648](https://github.com/realsenseai/librealsense/issues/14648)
- 自测用的模拟相机：Windows 和 Linux 上是官方 `rs.software_device`，见示例 [align-with-software-device.py](https://github.com/realsenseai/librealsense/blob/master/wrappers/python/examples/align-with-software-device.py)；Mac 上是脚本内的 Python 模拟帧，原因见常见问题
- 为什么不用 SDK 自带的录制：2.59 写出的是 `.db3`，Mac 上的 2.56 写出的是 `.bag`，两者不通用，只能用 SDK 回放，也不带丢帧和对齐的体检信息

## 文件清单

| 文件 | 作用 |
|---|---|
| `record_d455.py` | 全部功能都在这一个文件里：录制、自测、诊断、体检、修复、导出、读取 |
| `run.sh` / `run.bat` | 一键启动，首次运行自动装环境 |
| `requirements.txt` | 依赖列表 |
| `read_session_example.py` | 读取对齐帧的最小示例 |
| `d455_studio.py` + `studio/` | 网页界面：本机网页服务（只用 Python 标准库）和页面 |
| `studio.sh` / `studio.bat` | 启动网页界面 |
| `install_service.sh` | macOS：把网页界面装成按需启动的后台服务，以后录制不用输密码；`doctor` 排查问题 |
| `tests/` | 不需要相机的自动测试：`python tests/test_studio.py`（接口和按需启动），`node tests/test_page.js`（浏览器里的网页），`sudo python tests/test_root_safety.py`（服务以管理员身份运行时不跟随替身、不写进别人的文件夹），`python tests/test_repair.py`（修复遇到读写错误或空间不够时原文件不动） |
| `.github/workflows/test.yml` | 每次推送在 Ubuntu、Windows、macOS 上跑上面的测试，并在一台干净的 Mac 上安装、使用、卸载后台服务 |
