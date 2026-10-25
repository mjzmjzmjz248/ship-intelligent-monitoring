"""事件级离线评测：逐帧回放视频，用与船端相同的检测、ROI 和规则代码判断，再与人工真值比对。

不按实时节奏播放（跑得比实时快），评价的是"识别 + 规则"的事件效果，不评价实时延迟。

用法：
  python scripts/eval_events.py --video data/synthetic_duty.mp4 --truth data/synthetic_duty_truth.csv \
      [--config configs/demo.yaml] [--alarm A2_DUTYROOM_ABSENCE]

真值 CSV 列：video_id,start_s,end_s,state,note；state 取 present / absent / dark。
匹配口径（本项目约定，待老师确认）：同类型告警的触发时刻落在
[真值开始 + 触发持续, 真值开始 + 触发持续 + 2 秒] 内算正检；同一真值最多匹配一个告警。
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.config import load_config, resolve  # noqa: E402
from common.enums import AlarmType, NavState, Quality  # noqa: E402
from edge.detect import YoloPersonDetector  # noqa: E402
from edge.roi import count_in_roi  # noqa: E402
from edge.rules.alarms import EvalContext, Observation, Telemetry, evaluate  # noqa: E402
from edge.rules.engine import DurationRule, OutputKind, RuleParamsLite  # noqa: E402
from edge.source import is_dark  # noqa: E402

TRUTH_STATE = {AlarmType.A1_BRIDGE_ABSENCE: "absent", AlarmType.A2_DUTYROOM_ABSENCE: "absent",
               AlarmType.A3_CAMERA_ABNORMAL: "dark"}
MATCH_SLACK = 2.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--config", default="configs/demo.yaml")
    ap.add_argument("--camera", default="bridge-cam-01")
    ap.add_argument("--alarm", action="append", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    cam = cfg.camera(args.camera)
    alarms = [AlarmType(a) for a in args.alarm] if args.alarm else [a for a in cam.alarms if a in TRUTH_STATE]
    rules = {a: DurationRule(a, RuleParamsLite(cfg.rules[a].trigger_sec, cfg.rules[a].recover_sec,
                                                cfg.rules[a].repeat_sec, cfg.rules[a].risk)) for a in alarms}
    det = YoloPersonDetector(cfg.detector)
    q = cfg.quality

    cap = cv2.VideoCapture(str(resolve(args.video)))
    fps = cap.get(cv2.CAP_PROP_FPS) or 15
    step = max(1, round(fps / cfg.detector.target_fps))
    created: dict[AlarmType, list[float]] = {a: [] for a in alarms}
    frame_no, infer_ms, absent_frames, wrong_count = 0, [], 0, 0
    t_start = time.perf_counter()
    while True:
        ok, img = cap.read()
        if not ok:
            break
        t = frame_no / fps
        frame_no += 1
        if (frame_no - 1) % step:
            continue
        img = cv2.resize(img, (1280, 720))
        if t < q.warmup_sec:
            obs = Observation(t, Quality.WARMUP)
        elif is_dark(img, q.dark_gray, q.dark_ratio):
            obs = Observation(t, Quality.DARK)
        else:
            s = time.perf_counter()
            boxes = det.detect(img)
            infer_ms.append((time.perf_counter() - s) * 1000)
            n = count_in_roi(boxes, 1280, 720, cam.roi.points, cam.roi.anchor)
            obs = Observation(t, Quality.VALID, roi_count=n, persons=boxes)
        ctx = EvalContext(now=t, obs=obs, telemetry=Telemetry(t, NavState.UNDERWAY, 6.0, [0, 0]), last_ais_at=t)
        for a, r in rules.items():
            v, d = evaluate(a, ctx, {})
            for o in r.update(t, v, d):
                if o.kind == OutputKind.CREATED:
                    created[a].append(o.at)
    cap.release()
    wall = time.perf_counter() - t_start

    with open(resolve(args.truth), encoding="utf-8") as f:
        truth = [r for r in csv.DictReader(f)]
    # 帧级人数对照（只看真值为有人/无人的区段），帮助定位是检测问题还是规则问题
    print(f"视频 {args.video}，{frame_no / fps:.0f} 秒，处理用时 {wall:.1f} 秒；"
          f"单帧推理平均 {sum(infer_ms) / max(len(infer_ms), 1):.0f} ms（{len(infer_ms)} 帧）")
    print(f"参数：{cfg.profile}；匹配窗口：阈值满足后 {MATCH_SLACK} 秒内")
    for a in alarms:
        tp_list, fn_list = [], []
        p = cfg.rules[a]
        hits = list(created[a])
        for row in truth:
            if row["state"] != TRUTH_STATE[a]:
                continue
            start, end = float(row["start_s"]), float(row["end_s"])
            if end - start < p.trigger_sec:
                continue  # 持续时间不足，不应报警；如果报了会在下面算误检
            due = start + p.trigger_sec
            m = next((h for h in hits if due - 0.5 <= h <= due + MATCH_SLACK), None)
            if m is not None:
                hits.remove(m)
                tp_list.append((start, m - due))
            else:
                fn_list.append(start)
        fp = len(hits)
        tp, fn = len(tp_list), len(fn_list)
        recall = tp / (tp + fn) if tp + fn else None
        precision = tp / (tp + fp) if tp + fp else None
        print(f"\n[{a.value}] 真值事件 {tp + fn}，正检 {tp}，漏检 {fn}，误检 {fp}；"
              f"检出率 {'-' if recall is None else f'{recall:.0%}'}，准确率 {'-' if precision is None else f'{precision:.0%}'}")
        for s, lag in tp_list:
            print(f"  正检：真值 {s:.0f}s 开始，触发比阈值满足晚 {lag:.2f}s")
        for s in fn_list:
            print(f"  漏检：真值 {s:.0f}s 开始")
        for h in hits:
            print(f"  误检：{h:.1f}s 触发")
    print("\n注意：样本量很小，只能说明在该视频上的表现，不能推出实船检出率/准确率。")


if __name__ == "__main__":
    main()
