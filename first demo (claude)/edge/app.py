"""船端主程序：把视频、检测、规则、语音、证据、本地事件库和同步串起来。

主循环每 0.1 秒运行一次：取最新观测 → 判断 5 类船端告警 → 处理规则输出。
采集、检测、语音、编码、补传、心跳各在自己的线程里，互不阻塞。
控制台的操作（换摄像头、应用新配置等）放进命令队列，由主循环统一执行。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from common.config import RUNTIME_DIR, AppConfig, CameraConfig, RoiConfig, RuleParams, load_config
from common.enums import ALARM_NAMES, RISK_NAMES, VIDEO_ALARMS, AlarmType, ConditionStatus, Quality
from common.timeutil import SessionClock, local_str, now_iso, to_iso, utcnow
from edge.audio import AudioAnnouncer
from edge.detect import DetectorThread, PersonDetector, YoloPersonDetector
from edge.evidence import EvidenceManager, draw_annotations
from edge.roi import count_in_roi
from edge.rules.alarms import EvalContext, Observation, SpeedZoneLite, evaluate
from edge.rules.engine import DurationRule, OutputKind, RuleOutput, RuleParamsLite
from edge.sim import TelemetrySim
from edge.source import CaptureThread, FaultInjector
from edge.store import EdgeStore
from edge.sync import LinkThread, OutboxWorker, ShoreClient

log = logging.getLogger("edge.app")

TELEMETRY_ALARMS = (AlarmType.A5_AIS_OFFLINE, AlarmType.A6_OVERSPEED)


@dataclass
class Session:
    session_id: str
    camera: CameraConfig
    roi: RoiConfig
    start_ts: float
    capture: CaptureThread
    detector: DetectorThread
    rules: dict[AlarmType, DurationRule] = field(default_factory=dict)
    state: str = "RUNNING"  # RUNNING / ENDED / ERROR
    last_obs: Observation | None = None


def _lite(p: RuleParams) -> RuleParamsLite:
    return RuleParamsLite(p.trigger_sec, p.recover_sec, p.repeat_sec, p.risk)


class EdgeApp:
    def __init__(self, config_path: str | Path, *, camera_id: str | None = None, run_dir: Path | None = None,
                 shore_url: str | None = None, detector: PersonDetector | None = None,
                 enable_console: bool = True) -> None:
        self.cfg: AppConfig = load_config(config_path)
        self.run_dir = Path(run_dir or RUNTIME_DIR / "edge")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store = EdgeStore(self.run_dir / "edge.db")
        self.clock = SessionClock()
        self.faults = FaultInjector()
        self.sim = TelemetrySim(self.cfg.telemetry.scenario)
        self.audio = AudioAnnouncer(on_started=self._on_sound_started)
        self.evidence = EvidenceManager(self.cfg.evidence, self.store, self.run_dir / "evidence", self.clock)
        self.evidence.on_evidence_final = lambda eid: self._update_event(eid)
        token = os.environ.get(self.cfg.device.token_env, self.cfg.device.default_token)
        self.client = ShoreClient(shore_url or self.cfg.sync.shore_url, self.cfg.device.device_id, token,
                                  timeout=self.cfg.sync.timeout_sec)
        self.outbox = OutboxWorker(self.store, self.client)
        self.link = LinkThread(self.client, self.cfg.sync.heartbeat_sec, self.cfg.sync.preview_fps,
                               health=self.health, applied_version=lambda: self.config_version,
                               apply_config=lambda c: self.commands.put(("apply_config", c)),
                               preview=self.preview)
        self._detector = detector
        self.enable_console = enable_console

        self.profile = self.cfg.profile
        self.rule_params: dict[AlarmType, RuleParams] = dict(self.cfg.rules)
        self.rois: dict[str, RoiConfig] = {c.camera_id: c.roi for c in self.cfg.cameras}
        self.config_version = int(self.store.kv_get("config_version", "0") or 0)
        self._load_saved_config()

        self.camera_id = camera_id or self.cfg.active_camera
        self.session: Session | None = None
        self.tel_rules: dict[AlarmType, DurationRule] = {}
        self.commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self.loop_ms: float = 0.0

    # ================= 生命周期 =================
    def start(self) -> None:
        if self._detector is None:
            self._detector = YoloPersonDetector(self.cfg.detector)
        self._close_events_from_last_run()
        self.audio.start()
        self.outbox.start()
        self.link.start()
        if self.enable_console:
            from edge.console import start_console

            start_console(self, self.cfg.console_port)
        self._build_tel_rules()
        self._start_session()

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                self.step()
                time.sleep(0.1)
        finally:
            self.shutdown()

    def stop(self) -> None:
        self._stop.set()

    def shutdown(self) -> None:
        now = self.clock.now()
        self._end_session("船端程序停止")
        for rule in self.tel_rules.values():
            for o in rule.interrupt(now, "船端程序停止"):
                self._handle(o, None)
        self.evidence.stop()
        for t in (self.audio, self.outbox, self.link):
            t.stop()
        time.sleep(0.5)

    # ================= 主循环一步 =================
    def step(self) -> None:
        t0 = time.perf_counter()
        self._run_commands()
        now = self.clock.now()
        self.sim.tick(now)
        s = self.session
        outs: list[RuleOutput] = []
        obs = None
        if s is not None and s.state == "RUNNING":
            obs = self._observe(s, now)
            s.last_obs = obs
            if obs.quality == Quality.ENDED:
                self._on_source_ended()
                s = None
        ctx = self._context(now, obs)
        if s is not None and s.state == "RUNNING":
            for alarm, rule in s.rules.items():
                v, d = evaluate(alarm, ctx, self._extra(alarm))
                outs += rule.update(now, v, d)
        for alarm, rule in self.tel_rules.items():
            v, d = evaluate(alarm, ctx, self._extra(alarm))
            outs += rule.update(now, v, d)
        for o in outs:
            self._handle(o, obs)
        self.evidence.tick(now)
        self.loop_ms = (time.perf_counter() - t0) * 1000

    # ================= 会话 =================
    def _start_session(self) -> None:
        cam = self.cfg.camera(self.camera_id)
        roi = self.rois.get(cam.camera_id, cam.roi)
        start_ts = self.clock.now()
        self.evidence.new_session(start_ts)
        capture = CaptureThread(cam.source, self.clock, self.faults,
                                on_frame=lambda f: self.evidence.on_frame(f.ts, f.image),
                                dark_gray=self.cfg.quality.dark_gray, dark_ratio=self.cfg.quality.dark_ratio)
        detector = DetectorThread(self._detector, capture.get_latest, self.cfg.detector.target_fps)
        rules = {a: DurationRule(a, _lite(self.rule_params[a])) for a in cam.alarms
                 if a in self.rule_params and self.rule_params[a].enabled}
        self.session = Session(str(uuid.uuid4()), cam, roi, start_ts, capture, detector, rules)
        capture.start()
        detector.start()
        log.info("开始视频会话 %s：%s %s", self.session.session_id[:8], cam.name, cam.source.path or cam.source.url)

    def _end_session(self, reason: str) -> None:
        s = self.session
        if s is None:
            return
        now = self.clock.now()
        for rule in s.rules.values():
            for o in rule.interrupt(now, reason):
                self._handle(o, s.last_obs)
        s.capture.stop()
        s.detector.stop()
        self.evidence.flush(reason)
        self.session = None

    def _on_source_ended(self) -> None:
        s = self.session
        assert s is not None
        loop = s.camera.source.loop
        self._end_session("视频播放结束")
        if loop:
            self._start_session()

    def _build_tel_rules(self) -> None:
        now = self.clock.now()
        for rule in self.tel_rules.values():
            for o in rule.interrupt(now, "配置更新"):
                self._handle(o, None)
        self.tel_rules = {a: DurationRule(a, _lite(self.rule_params[a])) for a in TELEMETRY_ALARMS
                          if a in self.rule_params and self.rule_params[a].enabled}

    # ================= 观测 =================
    def _observe(self, s: Session, now: float) -> Observation:
        q = self.cfg.quality
        cap = s.capture
        if cap.ended:
            return Observation(now, Quality.ENDED)
        if now - s.start_ts < q.warmup_sec:
            return Observation(now, Quality.WARMUP)
        last = cap.last_frame_ts
        if last is None or now - last > q.no_frame_sec:
            return Observation(now, Quality.NO_FRAME)
        frame = cap.get_latest()
        if frame is not None and frame.dark:
            return Observation(now, Quality.DARK)
        det = s.detector.latest()
        if det is None or now - det.frame_ts > self.cfg.detector.max_result_age_sec:
            return Observation(now, Quality.STALE)
        count = count_in_roi(det.boxes, det.width, det.height, s.roi.points, s.roi.anchor)
        return Observation(now, Quality.VALID, roi_count=count, persons=det.boxes, infer_ms=det.infer_ms)

    def _context(self, now: float, obs: Observation | None) -> EvalContext:
        tc = self.cfg.telemetry
        return EvalContext(now=now, obs=obs, telemetry=self.sim.latest, last_ais_at=self.sim.last_ais_at,
                           telemetry_ttl=tc.ttl_sec, ais_gap=tc.ais_gap_sec,
                           speed_zones=[SpeedZoneLite(z.name, z.limit_kn, z.polygon) for z in tc.speed_zones])

    def _extra(self, alarm: AlarmType) -> dict[str, Any]:
        p = self.rule_params.get(alarm)
        return dict(p.model_extra or {}) if p else {}

    # ================= 规则输出 =================
    def _handle(self, o: RuleOutput, obs: Observation | None) -> None:
        if o.kind == OutputKind.CREATED:
            self._create_event(o, obs)
            return
        if o.kind == OutputKind.REMINDED:
            payload = self.store.get_event(o.event_id)
            if payload:
                self._update_event(o.event_id, reminder_count=payload.get("reminder_count", 0) + 1)
                self.audio.announce(o.alarm_type, self.rule_params[o.alarm_type].risk, o.event_id, "reminded")
            return
        changes: dict[str, Any] = {}
        if o.kind == OutputKind.RECOVERED:
            changes = {"condition_status": ConditionStatus.RECOVERED.value, "recovered_at": to_iso(self.clock.wall_of(o.at))}
        elif o.kind == OutputKind.UNKNOWN:
            changes = {"condition_status": ConditionStatus.UNKNOWN.value}
        elif o.kind == OutputKind.OBSERVABLE:
            changes = {"condition_status": ConditionStatus.ACTIVE.value}
        elif o.kind == OutputKind.PRECONDITION_ENDED:
            changes = {"condition_status": ConditionStatus.PRECONDITION_ENDED.value, "ended_reason": o.reason}
        elif o.kind == OutputKind.INTERRUPTED:
            changes = {"condition_status": ConditionStatus.INTERRUPTED.value, "ended_reason": o.reason}
        log.info("事件 %s %s → %s", o.event_id[:8], o.alarm_type.value, o.kind.value)
        self._update_event(o.event_id, **changes)

    def _create_event(self, o: RuleOutput, obs: Observation | None) -> None:
        alarm = o.alarm_type
        params = self.rule_params[alarm]
        s = self.session
        cam = s.camera if s else self.cfg.camera(self.camera_id)
        tel = self.sim.describe()
        frame = s.capture.get_latest() if s else None
        det = s.detector.latest() if s else None
        subtype = o.detail.get("subtype")
        payload: dict[str, Any] = {
            "event_id": o.event_id,
            "device_id": self.cfg.device.device_id,
            "vessel_id": self.cfg.vessel.vessel_id,
            "camera_id": cam.camera_id,
            "scene": cam.scene,
            "session_id": s.session_id if s else None,
            "alarm_type": alarm.value,
            "subtype": subtype,
            "risk": params.risk.value,
            "condition_status": ConditionStatus.ACTIVE.value,
            "started_at": to_iso(self.clock.wall_of(o.started_at)),
            "condition_satisfied_at": to_iso(self.clock.wall_of(o.condition_satisfied_at or o.at)),
            "triggered_at": now_iso(),
            "profile": self.profile,
            "rule_version": self.config_version,
            "rule_params": params.model_dump(mode="json"),
            "source_kind": {"video": cam.source.tag, "telemetry": "SIMULATED"},
            "telemetry": {k: tel[k] for k in ("nav_state", "speed_kn", "position")},
            "observation": ({"quality": obs.quality.value, "roi_count": obs.roi_count,
                             "persons": len(obs.persons)} if obs else None),
            "model": getattr(self._detector, "name", None),
            "reminder_count": 0,
        }
        # 先落库事件并播报（保证事件先于证据上传，且播报不等截图），再生成证据
        self._update_event(o.event_id, base=payload)
        log.warning("告警 %s %s（%s）", ALARM_NAMES[alarm], o.event_id[:8], params.risk.value)
        self.audio.announce(alarm, params.risk, o.event_id, "created")
        # 证据：断流时没有触发前后画面，只保留最后有效画面并标明时间
        boxes = det.boxes if (det and alarm in VIDEO_ALARMS and det.frame_ts >= (s.start_ts if s else 0)) else []
        stream_lost = subtype == "STREAM_LOST"
        lines = self._overlay_lines(alarm, params.risk, cam, tel)
        self.evidence.capture(
            o.event_id, o.at, frame.image if frame else None, frame.ts if frame else None, lines,
            roi=(s.roi.points if s else None), boxes=boxes,
            want_video=frame is not None and not stream_lost,
            video_na_reason="视频中断，无触发前后画面" if stream_lost else "无可用画面")
        self._update_event(o.event_id)

    def _overlay_lines(self, alarm: AlarmType, risk, cam: CameraConfig, tel: dict[str, Any]) -> list[str]:
        pos = tel.get("position")
        pos_s = f"{pos[0]:.5f}E, {pos[1]:.5f}N（模拟）" if pos else "缺失"
        spd = tel.get("speed_kn")
        spd_s = f"{spd:.1f} 节（模拟）" if spd is not None else "缺失"
        return [
            f"{cam.scene} | {self.cfg.vessel.name}",
            f"告警：{ALARM_NAMES[alarm]}（{RISK_NAMES[risk]}）",
            f"时间：{local_str(utcnow())}",
            f"位置：{pos_s}",
            f"航速：{spd_s}",
            f"来源：视频 {cam.source.tag} / 船况 SIMULATED / 参数 {self.profile}",
        ]

    def _update_event(self, event_id: str, base: dict[str, Any] | None = None, **changes: Any) -> None:
        """在最新内容上应用修改，附上当前证据状态，写入新 revision。"""
        with self._lock:
            payload = base or self.store.get_event(event_id)
            if payload is None:
                return
            payload = {**payload, **changes}
            payload["evidence"] = [self._evidence_ref(e) for e in self.store.evidence_for(event_id)]
            self.store.save_event_revision(payload)

    @staticmethod
    def _evidence_ref(e: dict[str, Any]) -> dict[str, Any]:
        return {"evidence_id": e["evidence_id"], "kind": e["kind"], "status": e["status"], "md5": e["md5"],
                "size": e["size"], "missing_reason": e["missing_reason"], "time_range": e["time_range"]}

    def _on_sound_started(self, event_id: str, kind: str, wall_ts: float) -> None:
        if kind != "created":
            return
        payload = self.store.get_event(event_id)
        if payload and not payload.get("sound_started_at"):
            self._update_event(event_id, sound_started_at=to_iso(datetime.fromtimestamp(wall_ts, timezone.utc)))

    def _close_events_from_last_run(self) -> None:
        """上次运行未结束的事件：无法证明期间持续异常，标记中断；未完成的视频证据标记失败。"""
        for ev_id in self.store.fail_pending_evidence("上次运行退出时尚未完成"):
            self._update_event(ev_id)
        for p in self.store.open_events():
            self._update_event(p["event_id"], condition_status=ConditionStatus.INTERRUPTED.value,
                               ended_reason="船端程序重启，期间无法连续观察")

    # ================= 配置 =================
    def _config_content(self) -> dict[str, Any]:
        return {"profile": self.profile,
                "rules": {a.value: p.model_dump(mode="json") for a, p in self.rule_params.items()},
                "rois": {cid: r.model_dump(mode="json") for cid, r in self.rois.items()}}

    def _load_saved_config(self) -> None:
        raw = self.store.kv_get("config_content")
        if raw:
            try:
                self._apply_content(json.loads(raw))
            except Exception:  # noqa: BLE001
                log.exception("本地保存的配置无效，使用配置文件")

    def _apply_content(self, content: dict[str, Any]) -> None:
        rules = {AlarmType(k): RuleParams.model_validate(v) for k, v in content["rules"].items()}
        rois = {cid: RoiConfig.model_validate(v) for cid, v in content.get("rois", {}).items()}
        self.rule_params = rules
        self.rois.update(rois)
        self.profile = content.get("profile", self.profile)

    def apply_config(self, cfg: dict[str, Any]) -> None:
        """岸端下发的配置。内容不变只记录版本；有变化则重建规则并开始新会话。"""
        version = int(cfg["version"])
        content = cfg["content"]
        try:
            old = json.dumps(self._config_content(), sort_keys=True)
            self._apply_content(content)
        except Exception as e:  # noqa: BLE001
            log.error("岸端配置 v%s 校验失败，继续使用当前配置：%s", version, e)
            return
        self.config_version = version
        self.store.kv_set("config_version", str(version))
        self.store.kv_set("config_content", json.dumps(content, ensure_ascii=False))
        if json.dumps(self._config_content(), sort_keys=True) != old:
            log.info("应用岸端配置 v%s，重新开始会话", version)
            self._build_tel_rules()
            self._end_session("配置更新")
            self._start_session()
        else:
            log.info("岸端配置 v%s 与当前一致", version)

    # ================= 控制命令 =================
    def _run_commands(self) -> None:
        while True:
            try:
                cmd, arg = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                if cmd == "apply_config":
                    self.apply_config(arg)
                elif cmd == "switch_camera":
                    self.cfg.camera(arg)
                    self._end_session("切换摄像头")
                    self.camera_id = arg
                    self._start_session()
                elif cmd == "restart_source":
                    self._end_session("人工重新开始")
                    self._start_session()
                elif cmd == "pause":
                    self.clock.pause()
                elif cmd == "resume":
                    self.clock.resume()
            except Exception:  # noqa: BLE001
                log.exception("执行命令 %s 失败", cmd)

    # ================= 状态输出 =================
    def health(self) -> dict[str, Any]:
        s = self.session
        obs = s.last_obs if s else None
        stats = self.store.outbox_stats()
        return {
            "camera_id": s.camera.camera_id if s else self.camera_id,
            "scene": s.camera.scene if s else None,
            "session_id": s.session_id if s else None,
            "source_state": ("ERROR" if s and s.capture.error else (s.state if s else "STOPPED")),
            "source_error": s.capture.error if s else None,
            "source_tag": s.camera.source.tag if s else None,
            "quality": obs.quality.value if obs else None,
            "roi_count": obs.roi_count if obs else None,
            "detect_fps": round(s.detector.actual_fps(), 2) if s else 0,
            "infer_ms": round(obs.infer_ms, 1) if obs and obs.infer_ms else None,
            "detector_error": s.detector.error if s else None,
            "model": getattr(self._detector, "name", None),
            "outbox_pending": stats["PENDING"],
            "outbox_dead": stats["DEAD"],
            "oldest_pending_sec": self.store.oldest_pending_age_sec(),
            "audio_backend": self.audio.backend,
            "audio_queue": self.audio.queued(),
            "evidence_jobs": self.evidence.pending_jobs(),
            "profile": self.profile,
            "fault": self.faults.mode,
            "sync_enabled": self.client.enabled,
            "telemetry": self.sim.describe(),
            "rules": {a.value: r.snapshot() for a, r in ((s.rules if s else {}) | self.tel_rules).items()},
            "loop_ms": round(self.loop_ms, 1),
        }

    def preview(self) -> tuple[bytes, dict[str, Any]] | None:
        s = self.session
        if s is None:
            return None
        frame = s.capture.get_latest()
        if frame is None:
            return None
        det = s.detector.latest()
        boxes = det.boxes if det and det.frame_ts >= s.start_ts else []
        img = draw_annotations(frame.image, [], s.roi.points, boxes, self.evidence.font)
        img = cv2.resize(img, (640, 360), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return None
        obs = s.last_obs
        tel = self.sim.describe()
        info = {
            "camera_id": s.camera.camera_id, "scene": s.camera.scene, "session_id": s.session_id,
            "captured_at": to_iso(self.clock.wall_of(frame.ts)),
            "quality": obs.quality.value if obs else None, "roi_count": obs.roi_count if obs else None,
            "roi": s.roi.model_dump(mode="json"), "profile": self.profile,
            "source_tag": s.camera.source.tag, "fault": self.faults.mode,
            "nav_state": tel["nav_state"], "speed_kn": tel["speed_kn"], "telemetry_source": "SIMULATED",
        }
        return buf.tobytes(), info

    def local_status(self) -> dict[str, Any]:
        return {"health": self.health(), "events": self.store.recent_events(30),
                "audio": self.audio.played[-10:], "link_online": self.link.online,
                "link_error": self.link.last_error, "outbox_error": self.outbox.last_error,
                "config_version": self.config_version, "boot_id": self.link.boot_id,
                "cameras": [{"camera_id": c.camera_id, "name": c.name} for c in self.cfg.cameras]}
