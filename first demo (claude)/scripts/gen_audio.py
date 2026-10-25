"""生成本地播报音频，运行时不联网。

- 提示音：总是生成（高风险为急促双音，其他为单音）。
- 中文语音：如果装了 pyttsx3 并有中文语音（Windows 自带 SAPI5 中文语音即可），为每类告警生成一段 WAV；
  否则跳过，只播提示音。

用法：python scripts/gen_audio.py      （Windows 上可先 pip install pyttsx3）
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import ROOT  # noqa: E402

OUT = ROOT / "assets" / "audio"
RATE = 16000
TEXTS = {
    "A1_BRIDGE_ABSENCE": "高风险告警。驾驶室无人在岗，请立即返回岗位。",
    "A2_DUTYROOM_ABSENCE": "高风险告警。值班室无人在岗，请立即返回岗位。",
    "A3_CAMERA_ABNORMAL": "视频监控设备异常，请检查摄像头。",
    "A5_AIS_OFFLINE": "AIS 设备异常，请检查 AIS 设备。",
    "A6_OVERSPEED": "船舶航速超过区域限速，请减速航行。",
}


def tone(freqs: list[tuple[float, float]], path: Path) -> None:
    parts = []
    for f, dur in freqs:
        t = np.arange(int(RATE * dur)) / RATE
        s = 0.0 if f == 0 else 0.6 * np.sin(2 * np.pi * f * t)
        env = np.minimum(1, np.minimum(t / 0.01, (dur - t) / 0.01)) if f else 1
        parts.append(s * env * np.ones_like(t))
    data = (np.concatenate(parts) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tone([(1400, 0.15), (0, 0.06), (1400, 0.15), (0, 0.06), (1400, 0.15), (0, 0.15)], OUT / "tone_high.wav")
    tone([(880, 0.35), (0, 0.15)], OUT / "tone_normal.wav")
    print("已生成提示音 tone_high.wav、tone_normal.wav")
    try:
        import pyttsx3
    except ImportError:
        print("未安装 pyttsx3，跳过中文语音（只播提示音）。Windows 上可 pip install pyttsx3 后重跑。")
        return
    engine = pyttsx3.init()
    zh = [v for v in engine.getProperty("voices") if "zh" in str(v.languages).lower() or "chinese" in v.name.lower()
          or "huihui" in v.name.lower()]
    if not zh:
        print("系统没有中文语音，跳过中文语音。")
        return
    engine.setProperty("voice", zh[0].id)
    for alarm, text in TEXTS.items():
        engine.save_to_file(text, str(OUT / f"{alarm}.wav"))
    engine.runAndWait()
    print(f"已用 {zh[0].name} 生成 {len(TEXTS)} 段中文语音")


if __name__ == "__main__":
    main()
