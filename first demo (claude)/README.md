# 船舶智能监控系统 第一版 Demo

依据课程需求（见仓库根目录的《船舶智能监控系统指南2.0》与《告警模型开发需求清单》）实现的单船、单路视频、6 类告警的完整闭环：

> 视频 → 人员检测 → ROI 与规则判定 → 船端本地语音告警 + 截图/短视频/MD5 → 上传岸端 → 网页确警处置 → 升级通知
> 断开船岸同步时船端照常告警和存证，恢复后自动补传，岸端不重复建事件。

完成情况、验证结果和与方案的差异见 [进度记录.md](进度记录.md)。

## 目录

| 目录 | 内容 |
| --- | --- |
| `common/` | 船岸共用的配置加载、消息结构、枚举、时间工具 |
| `edge/` | 船端：视频接入、人员检测、规则引擎、语音、证据、本地事件库、同步、本地控制台 |
| `shore/` | 岸端：FastAPI 接口、SQLite、离线检测与升级调度 |
| `web/` | 岸端网页（Vue 3 + Element Plus，免构建，依赖文件已放在 `web/vendor/`） |
| `configs/` | `demo.yaml` 演示参数、`standard.yaml` 资料参考参数、`scenarios/` 模拟船况 |
| `scripts/` | 下载模型、生成测试视频和提示音、初始化岸端、一键启动、控制命令、离线评测 |
| `tests/` | pytest：规则、升级、岸端接口、船岸同步端到端 |
| `runtime/` | 运行时数据库和证据（自动生成，不入库） |

## 在 Windows 上安装

需要 Python 3.11 及以上（开发时用的是 3.14）。

```powershell
cd "first demo (claude)"
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
```

Windows 下 `pip install torch` 默认就是 CPU 版。

## 在 Linux 上安装（长期环境）

环境建在 `~/venvs/ship-demo`（共享文件夹不支持符号链接；`/tmp` 是内存盘，重启即清空），
按 `requirements-lock-linux.txt` 装精确版本（torch 用 CPU 版），下载缓存保存在 `~/.cache/uv`。
需要先装好 [uv](https://docs.astral.sh/uv/)。在项目目录（`first demo (claude)`）中执行：

```bash
bash scripts/setup_linux_env.sh
```

只需执行一次；重启后环境仍在。脚本可重复运行：环境损坏时用它修复，缓存还在就不需要联网。
以后每次打开终端，执行下面这条进入环境（会激活环境、进入项目目录、让本机通信不走代理）：

```bash
source "<项目目录>/scripts/linux_env.sh"
```

环境用的是系统自带的 Python 3.14。如果以后系统升级到新的 Python 大版本，旧环境会失效，删除 `~/venvs/ship-demo` 后重跑安装脚本即可（届时需要重新下载对应版本的 torch）。
不要删除 `~/.cache/uv`：它是离线重建环境的来源（与环境共用硬链接，基本不额外占空间）。

## 首次准备（需要联网一次）

```powershell
python scripts/fetch_assets.py          # 下载 yolo11n.pt 到 models/，记录 SHA-256
python scripts/make_synthetic_video.py  # 生成 100 秒合成测试视频及真值 CSV
python scripts/gen_audio.py             # 生成提示音；装了 pyttsx3 的 Windows 还会生成中文语音
python scripts/init_shore.py            # 初始化岸端数据库，账号密码写入 runtime/shore/initial_accounts.txt
```

之后启动不再需要联网。

## 运行

```powershell
python scripts/run_all.py
```

- 岸端平台：<http://127.0.0.1:8000>（账号见 `runtime/shore/initial_accounts.txt`）
- 船端本地控制台：<http://127.0.0.1:8100>（断网时船员看本地告警、演示时注入故障）

也可以分别启动：`python -m shore` 和 `python -m edge --config configs/demo.yaml`。

演示操作（也可在船端控制台页面点按钮）：

```powershell
python scripts/edge_ctl.py sync off        # 断开船岸同步
python scripts/edge_ctl.py sync on
python scripts/edge_ctl.py fault black     # 注入黑屏；stream_cut 断流；clear 清除
python scripts/edge_ctl.py speed 10        # 模拟超速（限速区限速 8 节）
python scripts/edge_ctl.py ais off         # 模拟 AIS 无输出
python scripts/edge_ctl.py camera dutyroom-cam-01   # 切到值班室场景（A2）
```

## 测试

```powershell
python -m pytest                        # 规则、岸端接口、升级、船岸同步
python scripts/smoke_e2e.py              # 启动真实进程按演示脚本自动走查（约 3 分钟）
python scripts/eval_events.py --video data/synthetic_duty.mp4 --truth data/synthetic_duty_truth.csv
```

## 换成真实视频

1. 把视频放到 `data/`，修改 `configs/demo.yaml` 中摄像头的 `source`：
   - 文件：`{kind: file, path: data/xxx.mp4, loop: false, tag: LAB_REPLAY}`
   - USB 摄像头：`{kind: usb, index: 0, tag: LIVE_USB}`
   - RTSP：`{kind: rtsp, url: "rtsp://...", tag: LIVE_RTSP}`
2. 在网页“实时监控”中用 admin 账号重新绘制 ROI。
3. 按真值 CSV 格式标注后，用 `eval_events.py` 评测。

## 重要说明

- `data/synthetic_duty.mp4` 是用样例图片拼出来的合成视频，只用于联调，**不能**用来说明真实识别效果。
- 船况、AIS 全部是模拟数据，界面和事件中都标明 `SIMULATED`。
- 本版只验证单路、实验室条件，不代表已满足指南中的 90% 检出率/准确率、1.5 秒延迟、6 路 720P@15FPS 等指标。
- YOLO11 权重为 AGPL-3.0 许可，当前仅用于课程演示。
