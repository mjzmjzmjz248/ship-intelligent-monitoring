/* 船舶智能监控 岸端平台：免构建的 Vue 3 + Element Plus 单页应用。
 * 页面：实时监控、告警列表（含详情）、设备状态、规则配置；右上角通知。
 * 数据每 1~2 秒轮询一次。 */
const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } = Vue;
const { ElMessage } = ElementPlus;

const ALARM_NAMES = {
  A1_BRIDGE_ABSENCE: '驾驶室航行中无人在岗', A2_DUTYROOM_ABSENCE: '企业值班室人员离岗',
  A3_CAMERA_ABNORMAL: '视频设备异常', A4_TERMINAL_OFFLINE: '终端离线',
  A5_AIS_OFFLINE: 'AIS设备异常', A6_OVERSPEED: '未按规定航速航行',
};
const SUBTYPES = { STREAM_LOST: '断流', BLACK_SCREEN: '明显黑屏' };
const RISK = { HIGH: '高风险', MEDIUM: '中风险', LOW: '低风险' };
const RISK_TAG = { HIGH: 'danger', MEDIUM: 'warning', LOW: 'info' };
const COND = { ACTIVE: '告警中', UNKNOWN: '状态未知', RECOVERED: '已恢复', PRECONDITION_ENDED: '前置条件结束', INTERRUPTED: '已中断' };
const COND_TAG = { ACTIVE: 'danger', UNKNOWN: 'warning', RECOVERED: 'success', PRECONDITION_ENDED: 'info', INTERRUPTED: 'info' };
const HANDLE = { PENDING: '待确警', CONFIRMED: '已确警', PROCESSING: '处理中', RESOLVED: '已处置', FALSE_POSITIVE: '误报' };
const HANDLE_TAG = { PENDING: 'danger', CONFIRMED: 'warning', PROCESSING: 'warning', RESOLVED: 'success', FALSE_POSITIVE: 'info' };
const EVI = { PENDING: '采集/编码中', READY: '船端已定稿', PARTIAL: '不完整', FAILED: '失败', NOT_APPLICABLE: '不适用', VERIFIED: '已校验' };
const EVI_TAG = { PENDING: 'warning', READY: 'warning', PARTIAL: 'warning', FAILED: 'danger', NOT_APPLICABLE: 'info', VERIFIED: 'success' };
const LEVEL = { HIGH_RISK: '高风险直送', REPEAT_2: '同日第2次', REPEAT_3: '同日第3次及以上', CAPTAIN_TIMEOUT: '船长超时未处置', DUTYROOM: '值班室离岗直送' };
const ROLE = { admin: '管理员', operator: '岸基值班员', captain: '船长', manager: '企业负责人', viewer: '只读' };
const QUALITY = { VALID: '有效', DARK: '画面过暗', NO_FRAME: '无画面', STALE: '识别滞后', WARMUP: '预热中', ENDED: '视频结束' };
const SOURCE = { SYNTHETIC: '合成测试视频', LAB_REPLAY: '实验室录像回放', LIVE_USB: 'USB摄像头', LIVE_RTSP: 'RTSP摄像头', SIMULATED: '模拟' };
const PAGES = [['monitor', '实时监控'], ['alarms', '告警列表'], ['devices', '设备状态'], ['config', '规则配置']];
const NARROW = 640;

async function api(path, opts = {}) {
  const init = { credentials: 'same-origin', ...opts };
  if (opts.body && typeof opts.body !== 'string') {
    init.body = JSON.stringify(opts.body);
    init.headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  }
  const r = await fetch(path, init);
  if (r.status === 401) { state.user = null; throw new Error('未登录'); }
  const data = r.headers.get('content-type')?.includes('json') ? await r.json() : null;
  if (!r.ok) throw new Error((data && data.detail) || `HTTP ${r.status}`);
  return data;
}
const fmt = s => s ? new Date(s).toLocaleString('zh-CN', { hour12: false }) : '-';
const secs = (a, b) => (a && b) ? ((new Date(b) - new Date(a)) / 1000).toFixed(2) + ' 秒' : '-';
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random());

const state = reactive({ user: null, page: 'monitor', detailId: null, profile: '-', vw: window.innerWidth });
window.addEventListener('resize', () => { state.vw = window.innerWidth; });

function usePoll(fn, ms) {
  let t = null;
  onMounted(() => { fn(); t = setInterval(fn, ms); });
  onUnmounted(() => clearInterval(t));
}

/* 状态点 + 文字；label 为前缀（如“监测”“处置”），保证两类状态分开可辨 */
const Pill = {
  props: { type: { type: String, default: 'info' }, label: String },
  template: `<span class="pill" :class="'pill-' + type"><span v-if="label" class="lb">{{ label }}</span><slot/></span>`,
};
/* 参数套标注：standard 中有“待确认”的项 */
const ProfileBadge = {
  props: { profile: String, version: [Number, String] },
  template: `<span class="tags"><span class="badge">参数 {{ profile }}<template v-if="version != null"> v{{ version }}</template></span>
    <span v-if="profile === 'standard'" class="badge pend">含待确认项</span></span>`,
};

/* ---------------- 登录 ---------------- */
const LoginPage = {
  setup() {
    const form = reactive({ username: 'operator', password: '' });
    const loading = ref(false);
    async function submit() {
      loading.value = true;
      try { state.user = await api('/api/auth/login', { method: 'POST', body: form }); }
      catch (e) { ElMessage.error(e.message); }
      finally { loading.value = false; }
    }
    return { form, loading, submit };
  },
  template: `
  <div class="login-wrap"><div class="login-card">
    <h2>船舶智能监控</h2>
    <p class="muted sub">岸端平台 · 第一版 Demo</p>
    <el-form @submit.prevent="submit" label-position="top">
      <el-form-item label="用户名"><el-input v-model="form.username" autocomplete="username"/></el-form-item>
      <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password autocomplete="current-password"/></el-form-item>
      <el-button type="primary" native-type="submit" :loading="loading" style="width:100%;margin-top:4px">登录</el-button>
    </el-form>
    <p class="note">演示账号与密码由 scripts/init_shore.py 生成，见 runtime/shore/initial_accounts.txt</p>
  </div></div>`,
};

/* ---------------- 实时监控 ---------------- */
const MonitorPage = {
  setup() {
    const cams = ref([]);
    const camId = ref(null);
    const tick = ref(0);
    const events = ref([]);
    const editing = ref(false);
    const points = ref([]);
    const canvas = ref(null);
    const cam = computed(() => cams.value.find(c => c.camera_id === camId.value));
    const pv = computed(() => cam.value && cam.value.preview);
    const stale = computed(() => !pv.value || pv.value.age_sec > 3);
    async function load() {
      try {
        cams.value = await api('/api/cameras');
        if (!camId.value && cams.value.length) {
          const live = cams.value.find(c => c.preview && c.preview.age_sec < 10);
          camId.value = (live || cams.value[0]).camera_id;
        }
        tick.value++;
        events.value = (await api('/api/events?limit=6')).items;
      } catch (e) { /* 轮询失败时保持上次数据 */ }
    }
    usePoll(load, 1000);
    function draw() {
      const c = canvas.value; if (!c) return;
      c.width = c.clientWidth; c.height = c.clientHeight;
      const g = c.getContext('2d'); g.clearRect(0, 0, c.width, c.height);
      if (!editing.value || !points.value.length) return;
      g.strokeStyle = '#00e5ff'; g.fillStyle = 'rgba(0,229,255,.18)'; g.lineWidth = 2;
      g.beginPath();
      points.value.forEach(([x, y], i) => i ? g.lineTo(x * c.width, y * c.height) : g.moveTo(x * c.width, y * c.height));
      g.closePath(); g.fill(); g.stroke();
      points.value.forEach(([x, y]) => { g.beginPath(); g.arc(x * c.width, y * c.height, 4, 0, 7); g.fillStyle = '#00e5ff'; g.fill(); });
    }
    function clickCanvas(ev) {
      if (!editing.value) return;
      const r = canvas.value.getBoundingClientRect();
      points.value.push([+((ev.clientX - r.left) / r.width).toFixed(4), +((ev.clientY - r.top) / r.height).toFixed(4)]);
      draw();
    }
    function startEdit() { editing.value = true; points.value = []; nextTick(draw); }
    function cancelEdit() { editing.value = false; points.value = []; nextTick(draw); }
    async function saveRoi() {
      if (points.value.length < 3) return ElMessage.warning('至少需要 3 个点');
      try {
        const r = await api(`/api/config/roi/${camId.value}`, { method: 'PUT', body: { points: points.value } });
        ElMessage.success(`已发布配置 v${r.version}，等待船端应用`);
        cancelEdit();
      } catch (e) { ElMessage.error(e.message); }
    }
    return { cams, camId, cam, pv, stale, tick, events, editing, points, canvas, clickCanvas, startEdit, cancelEdit, saveRoi,
      fmt, QUALITY, SOURCE, ALARM_NAMES, RISK, RISK_TAG, COND, COND_TAG, HANDLE, HANDLE_TAG, state };
  },
  template: `
  <div class="grid-2">
    <div class="card">
      <div class="card-head">
        <h3>实时监控</h3>
        <div class="seg"><button v-for="c in cams" :key="c.camera_id" :class="{on: c.camera_id === camId}" @click="camId = c.camera_id">{{ c.name }}</button></div>
        <div class="end" v-if="state.user.role==='admin'">
          <el-button v-if="!editing" size="small" @click="startEdit">绘制 ROI</el-button>
          <template v-else>
            <span class="muted">依次点击画面上的多边形顶点（已选 {{ points.length }} 个）</span>
            <el-button size="small" type="primary" @click="saveRoi">保存并下发</el-button>
            <el-button size="small" @click="cancelEdit">取消</el-button>
          </template>
        </div>
      </div>
      <div class="preview-box">
        <img v-if="pv" :src="'/api/cameras/' + camId + '/preview.jpg?t=' + tick">
        <canvas ref="canvas" :class="{editing}" @click="clickCanvas"></canvas>
        <div v-if="stale && !editing" class="stale">{{ pv ? '画面已过期（最后采集 ' + fmt(pv.captured_at) + '）' : '暂无画面：船端未连接或未上送' }}</div>
      </div>
      <p class="note">预览为船端每秒约 2 张的低帧率图片（已叠加人框和 ROI），仅供查看，不是告警证据，也不是实时视频流。</p>
    </div>
    <div class="side">
      <div class="card">
        <div class="card-head"><h3>当前状态</h3></div>
        <template v-if="pv">
          <div class="metric"><span class="n">{{ pv.roi_count ?? '-' }}</span><span class="l">ROI 内人数</span></div>
          <div class="kv">
            <span class="k">场景</span><span>{{ pv.scene }}</span>
            <span class="k">采集时间</span><span>{{ fmt(pv.captured_at) }}<span class="muted">（{{ pv.age_sec.toFixed(1) }} 秒前）</span></span>
            <span class="k">观测</span><span><pill :type="pv.quality === 'VALID' ? 'success' : 'warning'">{{ QUALITY[pv.quality] || pv.quality || '-' }}</pill></span>
            <span class="k">航行状态</span><span>{{ pv.nav_state === 'UNDERWAY' ? '航行' : pv.nav_state === 'MOORED' ? '停泊' : '-' }}<span class="muted">（模拟）</span></span>
            <span class="k">航速</span><span>{{ pv.speed_kn != null ? pv.speed_kn.toFixed(1) + ' 节' : '-' }}<span class="muted">（模拟）</span></span>
            <span class="k">参数套</span><span><profile-badge :profile="pv.profile"/></span>
            <span class="k">数据来源</span><span class="tags"><span class="badge sim">视频：{{ SOURCE[pv.source_tag] || pv.source_tag }}</span>
              <span class="badge sim">船况：模拟</span></span>
            <span class="k">故障注入</span><span>{{ pv.fault || '无' }}</span>
          </div>
        </template>
        <div v-else class="muted">暂无船端数据</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>最新告警</h3><div class="end"><button class="link-btn" @click="state.page = 'alarms'">全部告警 →</button></div></div>
        <div v-if="!events.length" class="muted">暂无告警</div>
        <div v-for="e in events" :key="e.event_id" class="list-item" @click="state.detailId=e.event_id">
          <div class="t"><b>{{ ALARM_NAMES[e.alarm_type] }}</b><span :class="'risk-' + e.risk">{{ RISK[e.risk] }}</span></div>
          <div class="tags">
            <pill label="监测" :type="COND_TAG[e.condition_status]">{{ COND[e.condition_status] }}</pill>
            <pill label="处置" :type="HANDLE_TAG[e.handling_status]">{{ HANDLE[e.handling_status] }}</pill>
            <span class="muted">{{ fmt(e.triggered_at) }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>`,
};

/* ---------------- 告警列表 ---------------- */
const AlarmsPage = {
  setup() {
    const f = reactive({ alarm_type: '', risk: '', handling_status: '', condition_status: '', backfill: '' });
    const page = ref(1);
    const data = ref({ total: 0, items: [] });
    const stats = ref(null);
    async function load() {
      const q = new URLSearchParams({ limit: 20, offset: (page.value - 1) * 20 });
      Object.entries(f).forEach(([k, v]) => v !== '' && q.set(k, v));
      try {
        data.value = await api('/api/events?' + q);
        stats.value = await api('/api/stats/summary');
      } catch (e) { /* 保持上次数据 */ }
    }
    usePoll(load, 2000);
    watch(f, () => { page.value = 1; load(); });
    watch(page, load);
    const pct = v => v == null ? '-' : (v * 100).toFixed(0) + '%';
    const narrow = computed(() => state.vw < NARROW);
    const eviKeys = row => ['snapshot', 'video'].filter(k => row.evidence && k in row.evidence);
    return { f, page, data, stats, pct, narrow, eviKeys, fmt, state, ALARM_NAMES, SUBTYPES, RISK, RISK_TAG, COND, COND_TAG, HANDLE, HANDLE_TAG, EVI, EVI_TAG };
  },
  template: `
  <div>
    <template v-if="stats">
      <div class="stats">
        <div class="stat"><div class="n">{{ stats.total }}</div><div class="l">今日告警</div></div>
        <div class="stat"><div class="n" :class="{alert: stats.pending > 0}">{{ stats.pending }}</div><div class="l">待确警（全部）</div></div>
        <div class="stat"><div class="n">{{ stats.handled }}</div><div class="l">今日已处置/误报</div></div>
        <div class="stat"><div class="n">{{ pct(stats.handle_rate) }}</div><div class="l">今日处置率</div></div>
        <div class="stat"><div class="n">{{ pct(stats.false_positive_rate) }}</div><div class="l">误报占已处理</div></div>
        <div class="stat"><div class="n">{{ stats.backfill }}</div><div class="l">今日补传</div></div>
      </div>
      <div class="muted stats-foot">业务日 {{ stats.business_day }}（Asia/Shanghai）</div>
    </template>
    <div class="card">
      <div class="filters">
        <el-select v-model="f.alarm_type" placeholder="告警类型" clearable class="wide">
          <el-option v-for="(n,k) in ALARM_NAMES" :key="k" :label="n" :value="k"/></el-select>
        <el-select v-model="f.risk" placeholder="风险" clearable>
          <el-option v-for="(n,k) in RISK" :key="k" :label="n" :value="k"/></el-select>
        <el-select v-model="f.condition_status" placeholder="监测状态" clearable>
          <el-option v-for="(n,k) in COND" :key="k" :label="n" :value="k"/></el-select>
        <el-select v-model="f.handling_status" placeholder="处置状态" clearable>
          <el-option v-for="(n,k) in HANDLE" :key="k" :label="n" :value="k"/></el-select>
        <el-select v-model="f.backfill" placeholder="是否补传" clearable>
          <el-option label="实时" :value="0"/><el-option label="补传" :value="1"/></el-select>
      </div>
      <div v-if="narrow" class="alarm-cards">
        <div v-if="!data.items.length" class="muted">暂无数据</div>
        <div v-for="row in data.items" :key="row.event_id" class="list-item" @click="state.detailId = row.event_id">
          <div class="t"><b>{{ ALARM_NAMES[row.alarm_type] }}<span v-if="row.subtype" class="muted">（{{ SUBTYPES[row.subtype] || row.subtype }}）</span></b>
            <span :class="'risk-' + row.risk">{{ RISK[row.risk] }}</span></div>
          <div class="muted">{{ fmt(row.triggered_at) }} · {{ row.scene || '-' }} · {{ row.profile }} v{{ row.rule_version ?? '-' }}</div>
          <div class="tags">
            <pill label="监测" :type="COND_TAG[row.condition_status]">{{ COND[row.condition_status] }}</pill>
            <pill label="处置" :type="HANDLE_TAG[row.handling_status]">{{ HANDLE[row.handling_status] }}</pill>
            <pill v-for="k in eviKeys(row)" :key="k" :label="k==='snapshot'?'图':'视频'" :type="EVI_TAG[row.evidence[k]]">{{ EVI[row.evidence[k]] }}</pill>
            <span v-if="row.is_backfill" class="badge">补传</span>
          </div>
        </div>
      </div>
      <el-table v-else :data="data.items" size="small" @row-click="r => state.detailId = r.event_id">
        <el-table-column label="发生 / 接收时间" width="150"><template #default="{row}"><div class="cell-2">{{ fmt(row.triggered_at) }}<div class="sub">{{ fmt(row.received_at) }}</div></div></template></el-table-column>
        <el-table-column label="告警" min-width="170"><template #default="{row}"><div class="cell-2">
          {{ ALARM_NAMES[row.alarm_type] }}<span v-if="row.subtype" class="sub">（{{ SUBTYPES[row.subtype] || row.subtype }}）</span>
          <div class="sub">{{ row.scene || '-' }} · {{ row.profile }} v{{ row.rule_version ?? '-' }}<span v-if="row.is_backfill" class="badge" style="margin-left:6px">补传</span></div></div></template></el-table-column>
        <el-table-column label="风险" width="64"><template #default="{row}"><span :class="'risk-' + row.risk">{{ RISK[row.risk] }}</span></template></el-table-column>
        <el-table-column label="监测 / 处置" width="150"><template #default="{row}"><div class="stack">
          <pill label="监测" :type="COND_TAG[row.condition_status]">{{ COND[row.condition_status] }}</pill>
          <pill label="处置" :type="HANDLE_TAG[row.handling_status]">{{ HANDLE[row.handling_status] }}</pill></div></template></el-table-column>
        <el-table-column label="证据" width="150"><template #default="{row}"><div class="stack">
          <pill v-for="k in eviKeys(row)" :key="k" :label="k==='snapshot'?'图':'视频'" :type="EVI_TAG[row.evidence[k]]">{{ EVI[row.evidence[k]] }}</pill></div></template></el-table-column>
      </el-table>
      <el-pagination :layout="narrow ? 'prev, pager, next' : 'total, prev, pager, next'" :pager-count="narrow ? 5 : 7" :small="narrow"
        :total="data.total" :page-size="20" v-model:current-page="page"/>
    </div>
  </div>`,
};

/* ---------------- 告警详情（抽屉） ---------------- */
const DetailDrawer = {
  setup() {
    const d = ref(null);
    const note = ref('');
    const busy = ref(false);
    const open = computed({ get: () => !!state.detailId, set: v => { if (!v) state.detailId = null; } });
    async function load() {
      if (!state.detailId) return;
      try { d.value = await api('/api/events/' + state.detailId); } catch (e) { ElMessage.error(e.message); }
    }
    watch(() => state.detailId, id => { d.value = null; note.value = ''; if (id) load(); });
    let t = setInterval(() => open.value && load(), 2000);
    onUnmounted(() => clearInterval(t));
    const snap = computed(() => d.value && d.value.evidence.find(e => e.kind === 'snapshot'));
    const video = computed(() => d.value && d.value.evidence.find(e => e.kind === 'video'));
    const canHandle = computed(() => state.user && state.user.role !== 'viewer');
    const size = computed(() => state.vw < NARROW ? '100%' : state.vw < 1100 ? '88%' : '62%');
    const timeline = computed(() => {
      if (!d.value) return [];
      const e = d.value, items = [
        ['异常开始', e.started_at], ['持续阈值满足', e.payload.condition_satisfied_at], ['船端生成告警', e.triggered_at],
        ['船端开始播报', e.sound_started_at], ['岸端收到', e.received_at], ['异常恢复', e.recovered_at],
        ...e.actions.map(a => [`${a.username}（${ROLE[a.role] || a.role}）${a.action === 'confirm' ? '确警' : a.action === 'start' ? '开始处理' : a.action === 'resolve' ? '处置完成' : '标记误报'}${a.note ? '：' + a.note : ''}`, a.at]),
      ];
      return items.filter(x => x[1]).sort((a, b) => new Date(a[1]) - new Date(b[1]));
    });
    async function act(action) {
      busy.value = true;
      try {
        await api(`/api/events/${state.detailId}/actions`, { method: 'POST', body: { action, note: note.value || null, action_id: uid() } });
        note.value = ''; ElMessage.success('已提交'); load();
      } catch (e) { ElMessage.error(e.message); }
      finally { busy.value = false; }
    }
    return { d, open, note, busy, act, snap, video, canHandle, size, timeline, fmt, secs, ALARM_NAMES, SUBTYPES, RISK, RISK_TAG, COND, COND_TAG,
      HANDLE, HANDLE_TAG, EVI, EVI_TAG, LEVEL, ROLE, SOURCE };
  },
  template: `
  <el-drawer v-model="open" :size="size" :title="d ? ALARM_NAMES[d.alarm_type] : '告警详情'">
    <div v-if="d">
      <div class="tags" style="margin-bottom:16px">
        <pill :type="RISK_TAG[d.risk]">{{ RISK[d.risk] }}</pill>
        <pill label="监测" :type="COND_TAG[d.condition_status]">{{ COND[d.condition_status] }}</pill>
        <pill label="处置" :type="HANDLE_TAG[d.handling_status]">{{ HANDLE[d.handling_status] }}</pill>
        <span v-if="d.is_backfill" class="badge">补传（断网期间发生）</span>
        <span v-if="d.subtype" class="badge">{{ SUBTYPES[d.subtype] || d.subtype }}</span>
      </div>
      <div class="kv">
        <span class="k">场景 / 摄像头</span><span>{{ d.scene || '-' }} / {{ d.camera_id || '-' }}</span>
        <span class="k">来源</span><span>{{ d.origin === 'SHORE' ? '岸端判断' : '船端判断' }}
          <span v-if="d.source_kind" class="tags" style="display:inline-flex;margin-left:6px"><span class="badge sim">视频：{{ SOURCE[d.source_kind.video] || d.source_kind.video }}</span>
            <span class="badge sim">船况：{{ SOURCE[d.source_kind.telemetry] }}</span></span></span>
        <span class="k">参数</span><span><profile-badge :profile="d.profile" :version="d.rule_version ?? '-'"/>
          <span class="muted"><template v-if="d.payload.rule_params">持续 {{ d.payload.rule_params.trigger_sec }} 秒触发，重复间隔 {{ d.payload.rule_params.repeat_sec }} 秒</template>
          <template v-else-if="d.payload.basis">{{ d.payload.basis }}</template></span></span>
        <span class="k">船况（触发时）</span><span v-if="d.payload.telemetry">{{ d.payload.telemetry.nav_state }}，{{ d.payload.telemetry.speed_kn }} 节，位置 {{ d.payload.telemetry.position ? d.payload.telemetry.position.join(', ') : '缺失' }}<span class="muted">（模拟）</span></span><span v-else>-</span>
        <span class="k">模型</span><span>{{ d.payload.model || '-' }}</span>
        <span class="k">技术延迟</span><span>阈值满足 → 船端告警 {{ secs(d.payload.condition_satisfied_at, d.triggered_at) }}<br>
          → 开始播报 {{ secs(d.payload.condition_satisfied_at, d.sound_started_at) }}<br>→ 岸端收到 {{ secs(d.payload.condition_satisfied_at, d.received_at) }}</span>
        <span class="k">结束原因</span><span>{{ d.ended_reason || d.payload.reason || '-' }}</span>
      </div>
      <div class="cols">
        <div>
          <h4>截图</h4>
          <img v-if="snap && snap.status==='VERIFIED'" class="evidence-img" :src="'/api/evidence/' + snap.evidence_id + '/file'">
          <div v-else class="empty-box">{{ snap ? (EVI[snap.status] + (snap.missing_reason ? '：' + snap.missing_reason : '')) : '无' }}</div>
        </div>
        <div>
          <h4>告警视频（触发前后各 5 秒）</h4>
          <video v-if="video && video.status==='VERIFIED'" class="evidence-video" controls :src="'/api/evidence/' + video.evidence_id + '/file'"></video>
          <div v-else class="empty-box">{{ video ? (EVI[video.status] + (video.missing_reason ? '：' + video.missing_reason : '')) : '无' }}</div>
        </div>
      </div>
      <h4>证据文件</h4>
      <div v-if="!d.evidence.length" class="muted">无</div>
      <div v-for="row in [snap, video].filter(Boolean)" :key="row.evidence_id || row.kind" class="evi-row">
        <div class="tags"><b>{{ row.kind==='snapshot'?'截图':'视频' }}</b><pill :type="EVI_TAG[row.status]">{{ EVI[row.status] }}</pill>
          <span class="muted">{{ row.size ? (row.size/1024).toFixed(0)+' KB' : '' }}</span><span v-if="row.missing_reason" class="muted">{{ row.missing_reason }}</span></div>
        <div class="muted">MD5（岸端复核）<code>{{ row.md5 || '-' }}</code></div>
      </div>
      <div class="cols">
        <div>
          <h4>时间线</h4>
          <ul class="timeline"><li v-for="(t,i) in timeline" :key="i">{{ t[0] }}<span class="muted">{{ fmt(t[1]) }}</span></li></ul>
        </div>
        <div>
          <h4>通知与升级</h4>
          <div v-if="!d.notifications.length" class="muted">无</div>
          <div v-for="n in d.notifications" :key="n.id" style="margin-bottom:8px;font-size:13px">
            <span class="badge">{{ ROLE[n.role] }}</span> {{ LEVEL[n.level] || n.level }}
            <div class="muted">{{ n.reason }}；{{ fmt(n.created_at) }}<span v-if="n.deadline_at">，截止 {{ fmt(n.deadline_at) }}</span><span v-if="!n.realtime">，补传不弹窗</span></div>
          </div>
          <h4>处置</h4>
          <template v-if="canHandle">
            <el-input v-model="note" placeholder="处置备注（可选）" size="small"/>
            <div class="actions">
              <el-button size="small" type="primary" :loading="busy" :disabled="d.handling_status!=='PENDING'" @click="act('confirm')">确警</el-button>
              <el-button size="small" :loading="busy" :disabled="d.handling_status!=='CONFIRMED'" @click="act('start')">开始处理</el-button>
              <el-button size="small" :loading="busy" :disabled="!['CONFIRMED','PROCESSING'].includes(d.handling_status)" @click="act('resolve')">处置完成</el-button>
              <el-button size="small" text :loading="busy" :disabled="['RESOLVED','FALSE_POSITIVE'].includes(d.handling_status)" @click="act('false_positive')">标记误报</el-button>
            </div>
          </template>
          <div v-else class="muted">只读账号不能处置</div>
          <p class="note">监测状态（异常是否还在）与处置状态（人是否处理过）相互独立：人员回岗不代表已处置。</p>
        </div>
      </div>
      <el-collapse style="margin-top:16px"><el-collapse-item title="revision 历史（船端每次状态变化）">
        <div v-for="r in d.revisions" :key="r.revision" class="muted">rev {{ r.revision }}：{{ COND[r.condition_status] || r.condition_status }}，岸端收到 {{ fmt(r.received_at) }}<span v-if="r.reminder_count">，重复提醒 {{ r.reminder_count }} 次</span></div>
      </el-collapse-item></el-collapse>
    </div>
  </el-drawer>`,
};

/* ---------------- 设备状态 ---------------- */
const DevicesPage = {
  setup() {
    const list = ref([]);
    usePoll(async () => { try { list.value = await api('/api/devices'); } catch (e) {} }, 1500);
    const link = d => d.link_status === 'ONLINE' ? ['在线', 'success'] : d.link_status === 'OFFLINE' ? ['离线', 'danger'] : ['从未连接', 'info'];
    return { list, link, fmt, QUALITY };
  },
  template: `
  <div class="card">
    <div class="card-head"><h3>设备状态</h3></div>
    <div v-for="d in list" :key="d.device_id" class="device">
      <div class="device-head"><b>{{ d.device_id }}</b><span class="muted">{{ d.vessel_id }}</span>
        <pill :type="link(d)[1]">{{ link(d)[0] }}</pill>
        <span class="muted">最后心跳 {{ fmt(d.last_seen_at) }}<span v-if="d.last_seen_age_sec!=null">（{{ d.last_seen_age_sec.toFixed(0) }} 秒前）</span></span></div>
      <div class="kv" v-if="d.health">
        <span class="k">识别健康</span><span>
          <pill :type="d.health.detector_error || d.health.source_error ? 'danger' : 'success'">{{ d.health.detector_error || d.health.source_error ? '异常' : '正常' }}</pill>
          <span class="muted"> {{ d.health.detector_error || d.health.source_error || '' }}</span></span>
        <span class="k">视频源</span><span>{{ d.health.camera_id }}（{{ d.health.scene }}），{{ d.health.source_state }}，观测 {{ QUALITY[d.health.quality] || d.health.quality }}</span>
        <span class="k">人员检测</span><span>{{ d.health.model }}，实际 {{ d.health.detect_fps }} 次/秒，单帧 {{ d.health.infer_ms ?? '-' }} ms</span>
        <span class="k">待补传</span><span>{{ d.health.outbox_pending }} 条<span v-if="d.health.oldest_pending_sec">（最早 {{ d.health.oldest_pending_sec.toFixed(0) }} 秒前）</span>，失败 {{ d.health.outbox_dead }} 条</span>
        <span class="k">配置版本</span><span>船端已应用 v{{ d.applied_config_version }} / 最新 v{{ d.latest_config_version }}
          <pill :type="d.applied_config_version===d.latest_config_version?'success':'warning'">{{ d.applied_config_version===d.latest_config_version ? '已应用' : '等待船端应用' }}</pill></span>
        <span class="k">本地播报</span><span>{{ d.health.audio_backend === 'none' ? '无音频设备（只记录日志，未实际播放）' : d.health.audio_backend }}，排队 {{ d.health.audio_queue }}</span>
        <span class="k">故障注入</span><span>{{ d.health.fault || '无' }}；同步 {{ d.health.sync_enabled ? '开启' : '关闭' }}</span>
      </div>
      <p class="note">心跳每 5 秒一次；岸端只能看到“链路是否在线”，无法仅凭心跳区分断电、程序退出或网络中断。</p>
    </div>
    <div v-if="!list.length" class="muted">无设备</div>
  </div>`,
};

/* ---------------- 规则配置 ---------------- */
const ConfigPage = {
  setup() {
    const cfg = ref(null);
    const edit = ref({});
    async function load() {
      try {
        cfg.value = await api('/api/config');
        edit.value = JSON.parse(JSON.stringify(cfg.value.current.content.rules));
      } catch (e) {}
    }
    onMounted(load);
    const rows = computed(() => cfg.value ? Object.entries(edit.value).map(([k, v]) => ({ key: k, ...v })) : []);
    const isAdmin = computed(() => state.user && state.user.role === 'admin');
    async function switchProfile(p) {
      try { const r = await api('/api/config/profile/' + p, { method: 'POST' }); ElMessage.success(`已发布 v${r.version}`); load(); }
      catch (e) { ElMessage.error(e.message); }
    }
    async function saveRules() {
      try { const r = await api('/api/config/rules', { method: 'PUT', body: { rules: edit.value } }); ElMessage.success(`已发布 v${r.version}`); load(); }
      catch (e) { ElMessage.error(e.message); }
    }
    return { cfg, edit, rows, isAdmin, switchProfile, saveRules, load, fmt, ALARM_NAMES, RISK };
  },
  template: `
  <div v-if="cfg">
    <div class="card">
      <div class="card-head">
        <h3>当前参数</h3><profile-badge :profile="cfg.current.profile" :version="cfg.current.version"/>
        <div v-if="isAdmin" class="end">
          <div class="seg">
            <button :class="{on: cfg.current.profile==='demo'}" @click="switchProfile('demo')">demo</button>
            <button :class="{on: cfg.current.profile==='standard'}" @click="switchProfile('standard')">standard</button>
          </div>
          <el-button size="small" type="primary" @click="saveRules">保存修改并下发</el-button>
        </div>
      </div>
      <p class="note" style="margin:0 0 12px">demo 为演示参数（压短等待时间）；standard 为资料参考参数，带“待确认”的项需老师确认后才可作为验收口径。终端离线（A4）参数在岸端判断。</p>
      <el-table :data="rows" size="small">
        <el-table-column label="告警 / 依据" min-width="200"><template #default="{row}"><div class="rule-name">{{ ALARM_NAMES[row.key] }}<span class="muted">{{ row.basis }}</span></div></template></el-table-column>
        <el-table-column label="启用" width="64"><template #default="{row}"><el-switch v-model="edit[row.key].enabled" :disabled="!isAdmin" size="small"/></template></el-table-column>
        <el-table-column label="触发持续(秒)" width="124"><template #default="{row}">
          <el-input-number v-model="edit[row.key].trigger_sec" :min="0" size="small" :disabled="!isAdmin" controls-position="right" style="width:100px"/>
          <div v-if="row.pending.includes('trigger_sec')"><span class="badge pend">待确认</span></div></template></el-table-column>
        <el-table-column label="恢复持续(秒)" width="124"><template #default="{row}">
          <el-input-number v-model="edit[row.key].recover_sec" :min="0" size="small" :disabled="!isAdmin" controls-position="right" style="width:100px"/></template></el-table-column>
        <el-table-column label="重复间隔(秒)" width="124"><template #default="{row}">
          <el-input-number v-model="edit[row.key].repeat_sec" :min="1" size="small" :disabled="!isAdmin" controls-position="right" style="width:100px"/>
          <div v-if="row.pending.includes('repeat_sec')"><span class="badge pend">待确认</span></div></template></el-table-column>
        <el-table-column label="风险" width="64"><template #default="{row}"><span :class="'risk-' + row.risk">{{ RISK[row.risk] }}</span></template></el-table-column>
      </el-table>
      <div class="kv" style="margin-top:16px">
        <span class="k">终端离线</span><span>{{ cfg.current.content.shore.offline_after_sec }} 秒无心跳判定离线
          <span v-if="cfg.current.content.shore.offline_pending" class="badge pend">待确认</span>
          <span class="muted">{{ cfg.current.content.shore.offline_basis }}</span></span>
        <span class="k">船长处置时限</span><span>{{ cfg.current.content.shore.captain_deadline_sec }} 秒<span class="muted">（指南 2.5：15 分钟）</span></span>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>下发状态与历史</h3></div>
      <div v-for="a in cfg.applied" :key="a.device_id" class="tags" style="margin-bottom:8px">设备 {{ a.device_id }}：已应用 v{{ a.applied_config_version }}
        <pill :type="a.applied_config_version===cfg.current.version?'success':'warning'">{{ a.applied_config_version===cfg.current.version?'已应用最新':'等待船端应用' }}</pill></div>
      <el-table :data="cfg.history" size="small" style="margin-top:8px">
        <el-table-column label="版本" prop="version" width="64"/><el-table-column label="参数套" prop="profile" width="96"/>
        <el-table-column label="时间" width="160"><template #default="{row}">{{ fmt(row.created_at) }}</template></el-table-column>
        <el-table-column label="操作人" prop="created_by" width="90"/><el-table-column label="说明" prop="note" min-width="160"/>
      </el-table>
    </div>
  </div>`,
};

/* ---------------- 外壳 ---------------- */
const Shell = {
  components: { MonitorPage, AlarmsPage, DevicesPage, ConfigPage, DetailDrawer },
  setup() {
    const notes = ref([]);
    const seen = new Set();
    let first = true;
    usePoll(async () => {
      try {
        notes.value = await api('/api/notifications');
        const s = await api('/api/stats/summary'); state.profile = s.profile;
        for (const n of notes.value) {
          if (!seen.has(n.id)) {
            seen.add(n.id);
            const mine = ['captain', 'manager'].includes(state.user.role) ? n.role === state.user.role : true;
            if (!first && n.realtime && !n.read_at && mine)
              ElementPlus.ElNotification({ title: `${ROLE[n.role]}：${LEVEL[n.level] || n.level}`, message: `${ALARM_NAMES[n.alarm_type]}\n${n.reason}`, type: n.risk === 'HIGH' ? 'error' : 'warning', duration: 6000 });
          }
        }
        first = false;
      } catch (e) {}
    }, 2000);
    const unread = computed(() => notes.value.filter(n => !n.read_at && (['captain', 'manager'].includes(state.user.role) ? n.role === state.user.role : true)).length);
    async function markRead(n) { await api(`/api/notifications/${n.id}/read`, { method: 'POST' }); n.read_at = 'x'; state.detailId = n.event_id; }
    async function logout() { await api('/api/auth/logout', { method: 'POST' }).catch(() => {}); state.user = null; }
    const popWidth = computed(() => Math.min(360, state.vw - 32));
    return { state, notes, unread, markRead, logout, popWidth, fmt, ROLE, LEVEL, ALARM_NAMES, PAGES };
  },
  template: `
  <div>
    <header class="topbar"><div class="topbar-in">
      <span class="brand">船舶智能监控<small>岸端平台 · 第一版 Demo</small></span>
      <nav class="nav"><a v-for="p in PAGES" :key="p[0]" :class="{on: state.page===p[0]}" @click="state.page = p[0]">{{ p[1] }}</a></nav>
      <div class="top-right">
        <profile-badge :profile="state.profile"/>
        <el-popover placement="bottom-end" :width="popWidth" trigger="click">
          <template #reference>
            <el-badge :value="unread" :hidden="!unread"><button class="icon-btn" title="通知" aria-label="通知">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg></button></el-badge>
          </template>
          <div style="max-height:420px;overflow:auto">
            <div v-if="!notes.length" class="muted">暂无通知</div>
            <div v-for="n in notes" :key="n.id" class="list-item" :style="{opacity: n.read_at ? .55 : 1}" @click="markRead(n)">
              <div class="t"><b>{{ ROLE[n.role] }} · {{ LEVEL[n.level] || n.level }}</b><span v-if="!n.read_at" class="pill pill-danger plain">未读</span></div>
              <div>{{ ALARM_NAMES[n.alarm_type] }}</div>
              <div class="muted">{{ fmt(n.created_at) }}{{ n.realtime ? '' : '（补传）' }}</div>
            </div>
          </div>
        </el-popover>
        <span class="user">{{ state.user.display_name }}（{{ ROLE[state.user.role] }}）</span>
        <button class="link-btn" :title="state.user.display_name + '（' + ROLE[state.user.role] + '）'" @click="logout">退出</button>
      </div>
    </div></header>
    <main class="main">
      <monitor-page v-if="state.page==='monitor'"/>
      <alarms-page v-else-if="state.page==='alarms'"/>
      <devices-page v-else-if="state.page==='devices'"/>
      <config-page v-else-if="state.page==='config'"/>
    </main>
    <detail-drawer/>
  </div>`,
};

const Root = {
  components: { LoginPage, Shell },
  setup() {
    onMounted(async () => { try { state.user = await api('/api/me'); } catch (e) {} });
    return { state };
  },
  template: `<shell v-if="state.user"/><login-page v-else/>`,
};

createApp(Root).component('pill', Pill).component('profile-badge', ProfileBadge)
  .use(ElementPlus, { locale: ElementPlusLocaleZhCn }).mount('#app');
