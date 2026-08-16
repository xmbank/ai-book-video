<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  Check,
  ChevronRight,
  CircleEllipsis,
  ClipboardCheck,
  Download,
  ExternalLink,
  FileAudio,
  FileText,
  Film,
  Image as ImageIcon,
  LayoutGrid,
  Link,
  LoaderCircle,
  Menu,
  PencilLine,
  Play,
  Plus,
  RefreshCw,
  Save,
  Search,
  Settings,
  Sparkles,
  X,
} from "@lucide/vue";

type StageRecord = { status: string; message?: string; error?: { message?: string; retryable?: boolean }; artifacts?: string[] };
type EditableSection = "repair" | "script" | "book" | "styles";
type TaskSummary = {
  id: string;
  title: string;
  author: string;
  book_title?: string;
  mode: string;
  overall_status: string;
  created_at: string;
  updated_at: string;
  duration_seconds?: number;
  metrics: Record<string, { value?: number } | number>;
  stages: Record<string, StageRecord>;
  has_video: boolean;
  video_url?: string;
};
type TaskDetail = TaskSummary & {
  options: Record<string, any>;
  meta: Record<string, any>;
  active_artifacts: Record<string, string>;
  raw_transcript?: { full_text: string; segments?: any[] };
  repaired_transcript?: { raw_text: string; cleaned_text: string; repairs: string[]; findings: any[] };
  rewrite_candidates?: { candidates: any[]; content_card?: any; recommended_candidate_id?: string; rewrite_mode?: string };
  selected_script?: { script: string; candidate_id?: string; hook?: string; label?: string };
  tts_plan?: { segments: any[]; target_segment_seconds: number };
  tts_metadata?: { duration_seconds: number; segment_count: number; segments: any[]; provider: string; voice_type?: string };
  subtitles?: { items: any[]; duration_seconds: number };
  scene_manifest?: { count: number; grid_count: number; contact_sheet_count?: number; briefs: any[]; grids_urls: string[]; contact_sheets_urls?: string[]; scenes_urls: string[]; mode: string; quality?: { status: string; flagged_count: number } };
  book_info?: { book_title: string; book_author: string; confidence: number; evidence: string; needs_review: boolean; selling_points?: string[]; book_cover?: string; asset_warnings?: string[]; product_ready?: boolean };
  style_config?: { selected: string[]; counts: Record<string, number>; declaration: string; output_count: number };
  output_index?: { outputs: any[] };
  review_report?: { status: string; checks: Record<string, any>; outputs: any[] };
  audio_url?: string;
  source_video_url?: string;
};

const stages = [
  { id: "repair", label: "ASR 校对稿", hint: "只纠错与去噪，不在本步做二次创作", icon: FileText },
  { id: "book_info", label: "书籍与商品", hint: "先确认具体书籍、封面和真实卖点", icon: BookOpen },
  { id: "rewrite", label: "三策略二创", hint: "反常识、人群痛点、商品方案分别创作并评分", icon: PencilLine },
  { id: "audio", label: "分段音频", hint: "长文案切段并合成 TTS", icon: FileAudio },
  { id: "scene_images", label: "竖屏视觉分镜", hint: "视觉导演拆镜并逐张生成原生竖图", icon: ImageIcon },
  { id: "styles", label: "成片风格与数量", hint: "多风格矩阵号批量出片", icon: LayoutGrid },
  { id: "outputs", label: "批量成片", hint: "HyperFrames 合成与验收", icon: Film },
  { id: "review", label: "日志 / 人工确认", hint: "低置信度与合规复核", icon: ClipboardCheck },
];

const stylePresets = [
  { id: "clean-narration", label: "清雅语录", color: "#b44b3d", note: "强观点开头，适合观点型逐字稿" },
  { id: "typewriter-dark", label: "黑底打字机", color: "#242424", note: "文字节奏更强，适合金句观点" },
  { id: "dark-knowledge", label: "暗色知识卡", color: "#356478", note: "暗色底图，适合知识类内容" },
  { id: "book-broadcast", label: "图书口播卡", color: "#d7ae26", note: "书名与声明更突出，适合带货" },
];

const tasks = ref<TaskSummary[]>([]);
const current = ref<TaskDetail | null>(null);
const selectedTaskId = ref("");
const view = ref<"collection" | "detail">("collection");
const selectedStage = ref("repair");
const query = ref("");
const statusFilter = ref("all");
const busy = ref(false);
const loading = ref(true);
const error = ref("");
const showSettings = ref(false);
const mobileSteps = ref(false);
const candidateTab = ref("A");
const repairDraft = ref("");
const scriptDraft = ref("");
const bookDraft = ref({ book_title: "", book_author: "", confidence: 1, selling_points_text: "", book_cover: "", rewrite_mode: "medium", target_seconds: 60 });
const styleDraft = ref({ scene_count: 18, declaration: "", counts: {} as Record<string, number> });
const draftDirty = ref<Record<EditableSection, boolean>>({ repair: false, script: false, book: false, styles: false });
const settingsData = ref<Record<string, any>>({});
const settingsDraft = ref<Record<string, string>>({});
const createForm = ref({
  share_text: "",
  keyword: "健康图书",
  scene_count: 18,
  target_seconds: 60,
  book_title: "",
  selling_points_text: "",
  rewrite_mode: "medium",
});
let timer: number | undefined;
let detailRequestVersion = 0;

const filteredTasks = computed(() => {
  const term = query.value.trim().toLowerCase();
  return tasks.value.filter((task) => {
    const statusOk = statusFilter.value === "all" || task.overall_status === statusFilter.value;
    const queryOk = !term || `${task.title} ${task.author} ${task.book_title || ""}`.toLowerCase().includes(term);
    return statusOk && queryOk;
  });
});
const selectedStageRecord = computed(() => current.value?.stages?.[selectedStage.value]);
const activeCandidate = computed(() => current.value?.rewrite_candidates?.candidates?.find((item: any) => item.id === candidateTab.value));
const stageIndex = computed(() => stages.findIndex((item) => item.id === selectedStage.value));
const completedStages = computed(() => stages.filter((item) => current.value?.stages?.[item.id]?.status === "succeeded").length);

function humanStatus(value: string) {
  return ({
    rendered: "已成片", processing: "处理中", failed: "异常", outdated: "待重跑", draft: "待处理",
    succeeded: "已完成", running: "处理中", queued: "排队中", stale: "待重跑", review: "待确认",
    not_started: "待办", cancelled: "已取消",
  } as Record<string, string>)[value] || value;
}
function metric(task: TaskSummary, key: string) {
  const item: any = task.metrics?.[key];
  const value = typeof item === "object" ? item?.value : item;
  return value == null ? "--" : new Intl.NumberFormat("zh-CN").format(value);
}
function duration(value?: number) {
  if (value == null) return "--";
  const minutes = Math.floor(value / 60);
  const seconds = Math.round(value % 60);
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}
function date(value?: string) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}
function mediaUrl(path: string) {
  if (!current.value || !path) return "";
  const absoluteMarker = `/data/tasks/${current.value.id}/`;
  const normalized = path.includes(absoluteMarker) ? path.split(absoluteMarker)[1] : path;
  return `/api/tasks/${current.value.id}/media/${normalized}`;
}

function markDraftDirty(section: EditableSection) {
  draftDirty.value[section] = true;
}
function resetDraftDirty() {
  draftDirty.value = { repair: false, script: false, book: false, styles: false };
}
function syncDraftsFromServer(data: TaskDetail, force = false) {
  if (force || !draftDirty.value.repair) {
    repairDraft.value = data.repaired_transcript?.cleaned_text || data.raw_transcript?.full_text || "";
  }
  if (force || !draftDirty.value.script) {
    scriptDraft.value = data.selected_script?.script || "";
  }
  if (force || !draftDirty.value.book) {
    bookDraft.value = {
      book_title: data.book_info?.book_title || data.options?.book_title || "",
      book_author: data.book_info?.book_author || data.options?.book_author || "",
      confidence: data.book_info?.confidence ?? 1,
      selling_points_text: (data.book_info?.selling_points || data.options?.selling_points || []).join("\n"),
      book_cover: data.book_info?.book_cover || data.options?.book_cover || "",
      rewrite_mode: data.options?.rewrite_mode || "medium",
      target_seconds: data.options?.target_seconds ?? 60,
    };
  }
  if (force || !draftDirty.value.styles) {
    styleDraft.value = {
      scene_count: data.options?.scene_count ?? 18,
      declaration: data.style_config?.declaration || data.options?.declaration || "",
      counts: { ...Object.fromEntries(stylePresets.map((item) => [item.id, 0])), ...(data.style_config?.counts || data.options?.style_counts || {}) },
    };
  }
}
function acceptSavedDetail(data: TaskDetail, section: EditableSection) {
  // Invalidate detail polls that started before this save completed, so an
  // older response can never overwrite the newly accepted server version.
  detailRequestVersion += 1;
  draftDirty.value[section] = false;
  current.value = data;
  syncDraftsFromServer(data);
}

async function api(path: string, options: RequestInit = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}
async function loadTasks() {
  try { tasks.value = await api("/api/tasks"); }
  catch (exception: any) { error.value = exception.message; }
  finally { loading.value = false; }
}
async function loadDetail(taskId = selectedTaskId.value, quiet = false) {
  if (!taskId) return;
  const requestVersion = ++detailRequestVersion;
  try {
    const data = await api(`/api/tasks/${taskId}`);
    if (requestVersion !== detailRequestVersion || taskId !== selectedTaskId.value) return;
    const taskChanged = current.value?.id !== data.id;
    const candidates = data.rewrite_candidates?.candidates || [];
    const keepCandidateTab = current.value?.id === data.id
      && candidates.some((candidate: any) => candidate.id === candidateTab.value);
    if (taskChanged) resetDraftDirty();
    current.value = data;
    syncDraftsFromServer(data, taskChanged);
    if (!keepCandidateTab) {
      candidateTab.value = data.selected_script?.candidate_id || candidates[0]?.id || "A";
    }
  } catch (exception: any) { if (!quiet) error.value = exception.message; }
}
async function openTask(taskId: string) {
  selectedTaskId.value = taskId;
  selectedStage.value = "repair";
  view.value = "detail";
  await loadDetail(taskId);
}
async function poll() {
  await loadTasks();
  if (view.value === "detail") await loadDetail(selectedTaskId.value, true);
}

async function createTask(mode: "real" | "demo") {
  busy.value = true; error.value = "";
  try {
    const payload = mode === "demo" ? {
      mode: "demo", share_text: "", keyword: "时间管理图书", book_title: "把时间当作朋友",
      selling_points: ["从长期视角理解时间管理", "适合反复阅读"], rewrite_mode: "medium",
      scene_count: 18, target_seconds: 60, subtitle_mode: "proportional",
      styles: ["clean-narration"], style_counts: { "clean-narration": 1 },
    } : {
      mode: "real", share_text: createForm.value.share_text, keyword: createForm.value.keyword,
      book_title: createForm.value.book_title, selling_points: createForm.value.selling_points_text.split(/[\n,，；;]/).map((item) => item.trim()).filter(Boolean),
      rewrite_mode: createForm.value.rewrite_mode, scene_count: Number(createForm.value.scene_count), target_seconds: Number(createForm.value.target_seconds),
      subtitle_mode: "proportional", styles: ["clean-narration"], style_counts: { "clean-narration": 1 },
    };
    const task = await api("/api/tasks", { method: "POST", body: JSON.stringify(payload) });
    await loadTasks();
    await openTask(task.id);
  } catch (exception: any) { error.value = exception.message; }
  finally { busy.value = false; }
}
async function runFrom(stage: string) {
  if (!current.value) return;
  busy.value = true; error.value = "";
  try {
    await api(`/api/tasks/${current.value.id}/stages/${stage}/run`, { method: "POST" });
    current.value.overall_status = "processing";
    current.value.stages[stage] = { ...current.value.stages[stage], status: "queued", message: "已进入后台队列" };
  } catch (exception: any) { error.value = exception.message; }
  finally { busy.value = false; }
}
async function saveRepair() {
  if (!current.value) return;
  busy.value = true;
  try {
    const data = await api(`/api/tasks/${current.value.id}/repair`, { method: "PATCH", body: JSON.stringify({ cleaned_text: repairDraft.value }) });
    acceptSavedDetail(data, "repair");
  }
  catch (exception: any) { error.value = exception.message; }
  finally { busy.value = false; }
}
async function saveScript() {
  if (!current.value) return;
  busy.value = true;
  try {
    const data = await api(`/api/tasks/${current.value.id}/script`, { method: "PATCH", body: JSON.stringify({ script: scriptDraft.value }) });
    acceptSavedDetail(data, "script");
  }
  catch (exception: any) { error.value = exception.message; }
  finally { busy.value = false; }
}
function chooseCandidate(candidate: any) {
  scriptDraft.value = candidate.script;
  markDraftDirty("script");
}
async function saveBook() {
  if (!current.value) return;
  busy.value = true;
  try {
    const payload = {
      book_title: bookDraft.value.book_title,
      book_author: bookDraft.value.book_author,
      confidence: bookDraft.value.confidence,
      selling_points: bookDraft.value.selling_points_text.split(/[\n,，；;]/).map((item) => item.trim()).filter(Boolean),
      book_cover: bookDraft.value.book_cover || null,
      rewrite_mode: bookDraft.value.rewrite_mode,
      target_seconds: Number(bookDraft.value.target_seconds),
    };
    const data = await api(`/api/tasks/${current.value.id}/book`, { method: "PATCH", body: JSON.stringify(payload) });
    acceptSavedDetail(data, "book");
  }
  catch (exception: any) { error.value = exception.message; }
  finally { busy.value = false; }
}
function changeStyleCount(id: string, delta: number) {
  styleDraft.value.counts[id] = Math.max(0, Math.min(5, (styleDraft.value.counts[id] || 0) + delta));
  markDraftDirty("styles");
}
async function saveStyles() {
  if (!current.value) return;
  const selected = stylePresets.filter((item) => styleDraft.value.counts[item.id] > 0).map((item) => item.id);
  busy.value = true;
  try {
    const data = await api(`/api/tasks/${current.value.id}/styles`, {
      method: "PATCH",
      body: JSON.stringify({ styles: selected, style_counts: styleDraft.value.counts, declaration: styleDraft.value.declaration, scene_count: Number(styleDraft.value.scene_count) }),
    });
    acceptSavedDetail(data, "styles");
  } catch (exception: any) { error.value = exception.message; }
  finally { busy.value = false; }
}
async function openSettingsPanel() {
  settingsData.value = await api("/api/settings");
  settingsDraft.value = {
    llm_base_url: settingsData.value.llm_base_url || "", llm_model: settingsData.value.llm_model || "", llm_api_key: "",
    image_base_url: settingsData.value.image_base_url || "", image_model: settingsData.value.image_model || "gpt-image-2", image_size: settingsData.value.image_size || "1024x1536", image_api_key: "",
    volc_tts_endpoint: settingsData.value.volc_tts_endpoint || "", volc_tts_resource_id: settingsData.value.volc_tts_resource_id || "seed-tts-2.0", volc_tts_voice_type: settingsData.value.volc_tts_voice_type || "zh_female_vv_uranus_bigtts", volc_tts_api_key: "",
  };
  showSettings.value = true;
}
async function saveSettings() {
  busy.value = true;
  try { settingsData.value = await api("/api/settings", { method: "PATCH", body: JSON.stringify(settingsDraft.value) }); showSettings.value = false; }
  catch (exception: any) { error.value = exception.message; }
  finally { busy.value = false; }
}

onMounted(async () => { await loadTasks(); timer = window.setInterval(poll, 2500); });
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div class="app">
    <header class="global-header">
      <div class="brand"><span>书</span><div><strong>AI视频带货工具</strong><small>VIDEOHAO · BOOK COMMERCE PIPELINE</small></div></div>
      <div class="header-actions"><button class="ghost-btn" @click="loadTasks"><RefreshCw :size="16" />刷新数据</button><button class="icon-btn" title="服务设置" @click="openSettingsPanel"><Settings :size="18" /></button></div>
    </header>

    <div v-if="error" class="error-bar"><AlertTriangle :size="17" /><span>{{ error }}</span><button @click="error = ''"><X :size="16" /></button></div>

    <main v-if="view === 'collection'" class="collection-page">
      <section class="page-title actions-only"><button class="demo-btn" :disabled="busy" @click="createTask('demo')"><Play :size="16" />运行文章同款离线样片</button></section>

      <section class="import-band">
        <div class="band-heading"><Link :size="19" /><div><h2>URL 导入</h2><p>支持抖音短链、分享文案或完整作品链接</p></div></div>
        <div class="import-row">
          <textarea class="source-input" v-model="createForm.share_text" rows="2" placeholder="粘贴抖音分享链接或完整分享文案"></textarea>
          <label>主题关键词<input v-model="createForm.keyword" /></label>
          <label>目标时长<select v-model.number="createForm.target_seconds"><option v-for="seconds in [30,45,60,90,120]" :key="seconds" :value="seconds">{{ seconds }} 秒</option></select></label>
          <label>竖屏镜头<select v-model.number="createForm.scene_count"><option v-for="count in [6,9,12,15,18,24,30,36]" :key="count" :value="count">{{ count }} 张</option></select></label>
          <label>书名（可选）<input v-model="createForm.book_title" placeholder="留空则先识别" /></label>
          <label class="selling-points-input">已确认卖点（可选）<input v-model="createForm.selling_points_text" placeholder="用逗号分隔，禁止填写未经确认的信息" /></label>
          <label>二创强度<select v-model="createForm.rewrite_mode"><option value="light">轻度</option><option value="medium">中度</option><option value="deep">深度</option></select></label>
          <button class="primary-btn" :disabled="busy || !createForm.share_text.trim()" @click="createTask('real')"><LoaderCircle v-if="busy" class="spin" :size="16" /><Sparkles v-else :size="16" />按 URL 导入并制片</button>
        </div>
      </section>

      <section class="collection-results">
        <div class="section-head"><div><h2>采集结果</h2><p>每条素材保留来源数据、逐字稿和生产任务。</p></div><span>{{ filteredTasks.length }} 条素材</span></div>
        <div class="filters"><div class="search"><Search :size="15" /><input v-model="query" placeholder="搜索标题、作者、书名" /></div><div class="segmented"><button v-for="item in [{id:'all',label:'全部'},{id:'processing',label:'处理中'},{id:'rendered',label:'已成片'},{id:'failed',label:'异常'}]" :key="item.id" :class="{active:statusFilter===item.id}" @click="statusFilter=item.id">{{ item.label }}</button></div></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>素材</th><th>标题 / 作者</th><th>书籍</th><th>时长</th><th>点赞</th><th>评论</th><th>分享</th><th>任务</th><th>操作</th></tr></thead>
            <tbody>
              <tr v-for="task in filteredTasks" :key="task.id">
                <td><span class="source-tag">{{ task.mode === 'demo' ? '样片' : 'URL 导入' }}</span></td>
                <td class="title-cell"><strong>{{ task.title }}</strong><span>{{ task.author }} · {{ date(task.created_at) }}</span></td>
                <td>{{ task.book_title || '待 AI 识别' }}</td><td>{{ duration(task.duration_seconds) }}</td><td>{{ metric(task,'like') }}</td><td>{{ metric(task,'comment') }}</td><td>{{ metric(task,'share') }}</td>
                <td><span class="task-status" :class="task.overall_status">{{ humanStatus(task.overall_status) }}</span></td>
                <td><button class="table-action" @click="openTask(task.id)">制任务<ChevronRight :size="14" /></button></td>
              </tr>
              <tr v-if="!filteredTasks.length && !loading"><td colspan="9" class="empty-row">暂无素材，粘贴一条抖音链接开始。</td></tr>
            </tbody>
          </table>
        </div>
      </section>
    </main>

    <main v-else-if="current" class="detail-shell">
      <aside class="step-sidebar" :class="{ open: mobileSteps }">
        <div class="step-sidebar-head"><button class="back-btn" @click="view='collection';mobileSteps=false"><ArrowLeft :size="16" />返回热点采集</button><button class="icon-btn mobile-close" @click="mobileSteps=false"><X :size="18" /></button></div>
        <nav><button v-for="(stage,index) in stages" :key="stage.id" :class="[current.stages[stage.id]?.status,{active:selectedStage===stage.id}]" @click="selectedStage=stage.id;mobileSteps=false"><span class="step-number"><Check v-if="current.stages[stage.id]?.status==='succeeded'" :size="14" /><LoaderCircle v-else-if="['running','queued'].includes(current.stages[stage.id]?.status)" class="spin" :size="14" /><span v-else>{{ String(index+1).padStart(2,'0') }}</span></span><span><strong>{{ stage.label }}</strong><small>{{ humanStatus(current.stages[stage.id]?.status || 'not_started') }}</small></span></button></nav>
        <div class="current-task-card"><small>当前任务</small><strong>#{{ current.id.slice(-6) }}</strong><span>{{ humanStatus(current.overall_status) }}</span></div>
        <div class="policy-card"><small>流程策略</small><strong>异常才人工介入</strong><span>书名低置信度、合规风险和生成失败时暂停确认。</span></div>
      </aside>

      <section class="task-detail">
        <button class="mobile-step-menu" @click="mobileSteps=true"><Menu :size="18" />任务步骤</button>
        <header class="task-hero"><div><span>TASK DETAIL / PRODUCTION FLOW</span><h1>{{ current.title }}</h1><p>{{ current.author }} · {{ current.book_title || '书籍待确认' }}</p></div><div class="hero-actions"><button class="ghost-btn" :disabled="busy" @click="runFrom(selectedStage)"><RefreshCw :size="16" />从本步重跑</button><a v-if="current.video_url" class="primary-btn" :href="current.video_url" download><Download :size="16" />下载当前成片</a></div></header>

        <div class="progress-strip"><button v-for="(stage,index) in stages" :key="stage.id" :class="[current.stages[stage.id]?.status,{active:selectedStage===stage.id}]" :aria-label="stage.label" @click="selectedStage=stage.id"><span>{{ String(index+1).padStart(2,'0') }}</span><strong>{{ stage.label }}</strong><small>{{ humanStatus(current.stages[stage.id]?.status || 'not_started') }}</small></button><div class="progress-caption">第 {{ stageIndex + 1 }} 步 / 共 8 步 · 进度 {{ Math.round(completedStages / 8 * 100) }}%</div></div>

        <section class="task-stats"><div><small>状态</small><strong>{{ humanStatus(current.overall_status) }}</strong></div><div><small>处理方式</small><strong>{{ current.mode === 'demo' ? '离线参考样片' : '全自动' }}</strong></div><div><small>作者</small><strong>{{ current.author }}</strong></div><div><small>原视频时长</small><strong>{{ duration(current.duration_seconds) }}</strong></div><div><small>点赞</small><strong>{{ metric(current,'like') }}</strong></div><div><small>评论</small><strong>{{ metric(current,'comment') }}</strong></div><div><small>分享</small><strong>{{ metric(current,'share') }}</strong></div><div><small>成片数量</small><strong>{{ current.output_index?.outputs?.length || 0 }} 条</strong></div></section>

        <section class="step-intro"><span>STEP {{ String(stageIndex+1).padStart(2,'0') }}</span><h2>{{ stages[stageIndex]?.label }}</h2><p>{{ stages[stageIndex]?.hint }}</p><strong>{{ selectedStageRecord?.message || humanStatus(selectedStageRecord?.status || 'not_started') }}</strong></section>
        <div v-if="selectedStageRecord?.status==='failed'" class="failure"><AlertTriangle :size="19" /><div><strong>本步骤运行失败</strong><p>{{ selectedStageRecord.error?.message || selectedStageRecord.message }}</p><small v-if="selectedStageRecord.error?.retryable">这是临时错误，可以直接重试，不需要重新创建任务。</small></div><button class="ghost-btn" @click="runFrom(selectedStage)">重试</button></div>

        <section v-if="selectedStage==='repair'" class="workspace-panel two-cols">
          <div class="editor-box"><header><div><small>RAW ASR</small><h3>原始逐字稿</h3></div><span>{{ current.raw_transcript?.full_text?.length || 0 }} 字</span></header><textarea :value="current.raw_transcript?.full_text || ''" readonly></textarea></div>
          <div class="editor-box"><header><div><small>ASR PROOFREAD</small><h3>ASR 校对稿</h3></div><span v-if="current.repaired_transcript?.findings?.length" class="risk-badge">{{ current.repaired_transcript.findings.length }} 项风险</span></header><div class="stage-note">本步只纠错、去水印和降级风险表达，因此与原稿接近是正常的；吸引力与原创表达在“三策略二创”完成。</div><textarea v-model="repairDraft" @input="markDraftDirty('repair')"></textarea><footer><span>{{ current.repaired_transcript?.repairs?.join(' · ') }}</span><button class="primary-btn" @click="saveRepair"><Save :size="15" />确认校对结果</button></footer></div>
        </section>

        <section v-else-if="selectedStage==='rewrite'" class="workspace-panel rewrite-workspace">
          <div class="candidate-side">
            <div v-if="current.rewrite_candidates?.content_card" class="content-card"><small>单一核心观点</small><strong>{{ current.rewrite_candidates.content_card.core_claim }}</strong><span>{{ current.rewrite_candidates.content_card.target_audience }}</span></div>
            <div class="candidate-tabs"><button v-for="candidate in current.rewrite_candidates?.candidates || []" :key="candidate.id" :class="{active:candidateTab===candidate.id}" @click="candidateTab=candidate.id">{{ candidate.id }} · {{ candidate.label }}</button></div>
            <article v-if="activeCandidate"><div><span>{{ activeCandidate.id===current.rewrite_candidates?.recommended_candidate_id ? '系统推荐 · ' : '' }}{{ activeCandidate.label }}</span><span>{{ activeCandidate.char_count }} 字 · {{ duration(activeCandidate.estimated_seconds) }}</span></div><h3>{{ activeCandidate.hook }}</h3><div class="quality-row"><span>综合 {{ activeCandidate.quality?.overall_score || '--' }}</span><span>原创 {{ activeCandidate.quality?.originality_score || '--' }}</span><span>商品 {{ activeCandidate.quality?.product_score || '--' }}</span><span>风险 {{ activeCandidate.findings?.length || 0 }}</span></div><p>{{ activeCandidate.script }}</p><button class="ghost-btn" @click="chooseCandidate(activeCandidate)">采用此候选<ChevronRight :size="15" /></button></article>
          </div>
          <div class="editor-box"><header><div><small>FINAL NARRATION</small><h3>最终配音文案</h3></div></header><textarea v-model="scriptDraft" @input="markDraftDirty('script')"></textarea><footer><span>{{ scriptDraft.length }} 字</span><button class="primary-btn" @click="saveScript"><Save :size="15" />保存候选稿</button></footer></div>
        </section>

        <section v-else-if="selectedStage==='audio'" class="workspace-panel audio-layout">
          <div class="audio-summary"><div class="audio-icon"><FileAudio :size="28" /></div><div><small>音频状态</small><h3>{{ current.tts_metadata?.provider || '等待生成' }}</h3><p>{{ current.tts_metadata?.segment_count || 0 }} 段 · {{ duration(current.tts_metadata?.duration_seconds) }}</p></div><audio v-if="current.audio_url" :src="current.audio_url" controls></audio></div>
          <div class="audio-segments"><header><h3>分段时长预估</h3><span>目标单段 {{ current.tts_plan?.target_segment_seconds || 26 }} 秒</span></header><div v-for="segment in current.tts_plan?.segments || []" :key="segment.index"><strong>音频片段 {{ segment.index }}</strong><span>{{ segment.estimated_seconds }} 秒</span><p>{{ segment.text }}</p></div></div>
        </section>

        <section v-else-if="selectedStage==='scene_images'" class="workspace-panel image-workspace">
          <div class="image-summary"><div><small>原生竖图</small><strong>{{ current.scene_manifest?.count || current.options.scene_count }} 张</strong></div><div><small>预览联系表</small><strong>{{ current.scene_manifest?.contact_sheet_count ?? current.scene_manifest?.grid_count ?? 0 }} 组</strong></div><div><small>自动质检</small><strong>{{ current.scene_manifest?.quality?.status === 'passed' ? '通过' : `${current.scene_manifest?.quality?.flagged_count || 0} 张待看` }}</strong></div><button class="primary-btn" @click="runFrom('scene_images')"><RefreshCw :size="15" />重新生成竖屏图</button></div>
          <div class="grid-section"><header><h3>竖图预览联系表</h3><p>联系表由真实 9:16 单图确定性拼接，只用于总览，不参与成片裁切。</p></header><div class="grid-gallery portrait-contacts"><img v-for="(url,index) in current.scene_manifest?.contact_sheets_urls || current.scene_manifest?.grids_urls || []" :key="url" :src="url" :alt="`联系表 ${index+1}`" /></div></div>
          <div class="scene-section"><header><h3>原生 9:16 场景图</h3><span>{{ current.scene_manifest?.scenes_urls?.length || 0 }}/{{ current.scene_manifest?.count || 0 }} 已生成</span></header><div class="scene-gallery"><article v-for="(url,index) in current.scene_manifest?.scenes_urls || []" :key="url"><img :src="url" :alt="`场景图 ${index+1}`" /><strong>{{ String(index+1).padStart(2,'0') }} · {{ current.scene_manifest?.briefs?.[index]?.shot_role || '镜头' }}</strong><p>{{ current.scene_manifest?.briefs?.[index]?.visual_purpose || current.scene_manifest?.briefs?.[index]?.script_text }}</p></article></div></div>
        </section>

        <section v-else-if="selectedStage==='book_info'" class="workspace-panel book-workspace">
          <div class="confidence" :class="{warn:current.book_info?.needs_review || current.book_info?.asset_warnings?.length}"><BookOpen :size="28" /><div><small>商品资料完整度</small><strong>{{ current.book_info?.product_ready ? '可二创' : `${Math.round((current.book_info?.confidence || 0)*100)}%` }}</strong><p>{{ current.book_info?.evidence || '等待识别书籍信息' }}</p><p v-for="warning in current.book_info?.asset_warnings || []" :key="warning">· {{ warning }}</p></div></div>
          <div class="book-form" @input="markDraftDirty('book')" @change="markDraftDirty('book')"><label>书籍名<input v-model="bookDraft.book_title" placeholder="必须对应实际挂车商品" /></label><label>作者 / 具体版本<input v-model="bookDraft.book_author" /></label><label>已确认卖点<textarea v-model="bookDraft.selling_points_text" rows="3" placeholder="每行一条，只填写可从商品页或实物确认的卖点"></textarea></label><label>真实封面本地路径<input v-model="bookDraft.book_cover" placeholder="可选；AI 不会虚构封面" /></label><label>二创强度<select v-model="bookDraft.rewrite_mode"><option value="light">轻度校改</option><option value="medium">中度二创</option><option value="deep">深度二创</option></select></label><label>目标口播时长（秒）<input v-model.number="bookDraft.target_seconds" type="number" min="10" max="1200" /></label><button class="primary-btn" @click="saveBook"><Save :size="15" />确认商品并使二创待重跑</button></div>
          <div class="title-preview"><h3>下游使用原则</h3><p>书籍信息现在先于二创和生图。真实封面由后期叠加；没有确认的卖点、版次或内页不会让模型猜。</p><div><span>《{{ bookDraft.book_title || '待确认书名' }}》</span><span>{{ bookDraft.book_author || '待确认作者 / 版本' }}</span><small>{{ bookDraft.selling_points_text || '尚无已确认商品卖点' }}</small></div></div>
        </section>

        <section v-else-if="selectedStage==='styles'" class="workspace-panel style-workspace">
          <div class="style-head"><div><h3>视频风格</h3><p>每种风格可以单独设置生成数量，总数用于矩阵号分发。</p></div><strong>共 {{ Object.values(styleDraft.counts).reduce((a:number,b:number)=>a+b,0) }} 条</strong></div>
          <div class="style-list"><article v-for="preset in stylePresets" :key="preset.id" :class="{selected:styleDraft.counts[preset.id]>0}"><span class="style-color" :style="{background:preset.color}"></span><div><strong>{{ preset.label }}</strong><p>{{ preset.note }}</p></div><div class="stepper"><button @click="changeStyleCount(preset.id,-1)">−</button><span>{{ styleDraft.counts[preset.id] || 0 }}</span><button @click="changeStyleCount(preset.id,1)">+</button></div></article></div>
          <div class="style-settings" @input="markDraftDirty('styles')" @change="markDraftDirty('styles')"><label>竖屏镜头数量<select v-model.number="styleDraft.scene_count"><option v-for="count in [6,9,12,15,18,24,30,36]" :key="count" :value="count">{{ count }} 张</option></select></label><label class="declaration-field">健康与内容声明<textarea v-model="styleDraft.declaration" rows="3"></textarea></label><button class="primary-btn" @click="saveStyles"><Save :size="15" />保存风格与数量</button></div>
        </section>

        <section v-else-if="selectedStage==='outputs'" class="workspace-panel outputs-workspace">
          <div v-if="current.output_index?.outputs?.length" class="output-grid"><article v-for="output in current.output_index.outputs" :key="output.video_url"><video :src="output.video_url" controls playsinline></video><div><strong>{{ output.style_label }} · 版本 {{ output.variant || 1 }}</strong><a :href="output.video_url" download><Download :size="15" />下载 MP4</a></div></article></div><div v-else class="stage-empty"><Film :size="34" /><h3>等待批量成片</h3><p>确认书籍信息、场景图和风格数量后，从本步运行。</p><button class="primary-btn" @click="runFrom('outputs')"><Play :size="15" />开始生成成片</button></div>
        </section>

        <section v-else-if="selectedStage==='review'" class="workspace-panel review-workspace">
          <div class="review-status"><ClipboardCheck :size="34" /><div><small>人工确认状态</small><h3>{{ current.review_report?.status === 'ready' ? '产物已就绪' : '需要人工复核' }}</h3><p>发布前检查书名作者、医疗暗示、极限词、字幕错字和素材一致性。</p></div></div>
          <div class="review-checks"><article v-for="(value,key) in current.review_report?.checks || {}" :key="key"><Check v-if="value==='passed' || typeof value==='number'" :size="17" /><AlertTriangle v-else :size="17" /><div><strong>{{ key }}</strong><span>{{ value }}</span></div></article></div>
          <div class="log-block"><h3>运行日志与产物</h3><div v-for="stage in stages" :key="stage.id"><span>{{ stage.label }}</span><strong>{{ humanStatus(current.stages[stage.id]?.status || 'not_started') }}</strong><small>{{ current.stages[stage.id]?.artifacts?.length || 0 }} 个产物</small></div></div>
        </section>
      </section>
      <div v-if="mobileSteps" class="mobile-scrim" @click="mobileSteps=false"></div>
    </main>

    <div v-if="showSettings" class="modal-scrim" @click.self="showSettings=false"><section class="settings-modal"><header><div><h2>服务设置</h2><p>LLM、图片生成和 TTS 分别配置；密钥只保存在本机。</p></div><button class="icon-btn" @click="showSettings=false"><X :size="18" /></button></header><div class="settings-section"><h3>大模型 · 校对 / 二创 / 商品识别</h3><label>API 地址<input v-model="settingsDraft.llm_base_url" /></label><div><label>模型<input v-model="settingsDraft.llm_model" /></label><label>API Key<input v-model="settingsDraft.llm_api_key" type="password" :placeholder="settingsData.llm_api_key_configured?'已配置，留空不修改':'未配置'" /></label></div></div><div class="settings-section"><h3>图片生成 · 原生 9:16 单图</h3><label>API 地址<input v-model="settingsDraft.image_base_url" /></label><div><label>模型<input v-model="settingsDraft.image_model" /></label><label>尺寸<input v-model="settingsDraft.image_size" /></label><label>API Key<input v-model="settingsDraft.image_api_key" type="password" :placeholder="settingsData.image_api_key_configured?'已配置，留空不修改':'未配置'" /></label></div></div><div class="settings-section"><h3>TTS 配音 · 豆包语音合成 2.0</h3><label>Endpoint<input v-model="settingsDraft.volc_tts_endpoint" /></label><div><label>API Key<input v-model="settingsDraft.volc_tts_api_key" type="password" :placeholder="settingsData.volc_tts_api_key_configured?'已配置，留空不修改':'未配置'" /></label><label>Resource ID<input v-model="settingsDraft.volc_tts_resource_id" /></label><label>音色 ID<input v-model="settingsDraft.volc_tts_voice_type" list="volc-tts-voices" /></label></div><datalist id="volc-tts-voices"><option value="zh_female_vv_uranus_bigtts">Vivi 2.0 · 通用女声</option><option value="zh_female_liuchangnv_uranus_bigtts">流畅女声 2.0 · 视频配音</option><option value="zh_female_cancan_uranus_bigtts">知性灿灿 2.0</option><option value="zh_female_jitangnv_uranus_bigtts">鸡汤女 2.0 · 视频配音</option><option value="zh_male_dayi_uranus_bigtts">大壹 2.0 · 视频配音</option><option value="zh_male_ruyayichen_uranus_bigtts">儒雅逸辰 2.0 · 视频配音</option><option value="zh_male_jieshuoxiaoming_uranus_bigtts">解说小明 2.0</option><option value="zh_male_ruyaqingnian_uranus_bigtts">儒雅青年 2.0</option></datalist></div><footer><button class="ghost-btn" @click="showSettings=false">取消</button><button class="primary-btn" @click="saveSettings"><Save :size="15" />保存设置</button></footer></section></div>
  </div>
</template>
