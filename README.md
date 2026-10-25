# 船舶智能监控系统

课程项目：面向船舶驾驶室与企业值班室的智能视频监控与告警。仓库包含第一版可运行 Demo 及其依据的需求资料。

第一版 Demo 实现单船、单路视频、6 类告警的完整闭环：

> 视频 → 人员检测（YOLO11）→ ROI 与规则判定 → 船端本地语音告警 + 截图/短视频/MD5 存证 → 上传岸端 → 网页确警处置 → 升级通知
> 断开船岸同步时船端照常告警和存证，恢复后自动补传，岸端不重复建事件。

## 仓库内容

| 路径 | 说明 |
| --- | --- |
| [`first demo (claude)/`](first%20demo%20(claude)/) | 第一版 Demo 源码（Python：FastAPI + OpenCV + Ultralytics；岸端网页 Vue 3 + Element Plus，免构建、可离线运行） |
| [`船舶智能监控系统指南2.0.pdf`](船舶智能监控系统指南2.0.pdf) | 系统指南（需求来源） |
| [`附件1+告警模型开发需求清单.docx`](附件1+告警模型开发需求清单.docx) | 告警模型开发需求清单 |

## 快速开始

安装、首次准备、运行、测试和演示操作见 [Demo 说明](first%20demo%20(claude)/README.md)；完成情况、验证结果与已知问题见 [进度记录](first%20demo%20(claude)/进度记录.md)。

简要步骤（Windows，需要 Python 3.11+）：

```powershell
cd "first demo (claude)"
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python scripts/fetch_assets.py
python scripts/make_synthetic_video.py
python scripts/gen_audio.py
python scripts/init_shore.py
python scripts/run_all.py
```

岸端平台 <http://127.0.0.1:8000>，船端本地控制台 <http://127.0.0.1:8100>。模型权重和测试视频不在仓库中，由上面的脚本下载或生成。

## 重要说明

- 测试视频是用样例图片拼出来的合成视频，只用于联调，不能说明真实识别效果；船况和 AIS 全部是模拟数据。
- 本版只在单路、实验室条件下验证，不代表已满足指南中的检出率、延迟、多路并发等指标。
- 代码以 [AGPL-3.0](LICENSE) 发布（人员检测依赖的 Ultralytics YOLO11 为 AGPL-3.0）。`web/vendor/` 中的 Vue 与 Element Plus 为 MIT 许可。
- 两份需求资料（PDF、docx）为课程资料，版权归原作者，不适用本仓库的代码许可。
