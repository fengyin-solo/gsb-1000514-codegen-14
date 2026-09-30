<template>
  <section class="page compare-page" data-module="hydro_compare">
    <header class="page-head">
      <div>
        <h2>多孔水位对照台</h2>
        <p class="page-desc">
          按观测类型与所在钻孔对照各孔时间轴与偏离带：点选红色偏离段即可派发补测；
          偏离结论会同步水文观测台账、钻孔详情与补测待办，并驱动概览看板重算。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="simulateOutOfOrder">模拟乱序补数（SW-01）</button>
        <button class="btn ghost" type="button" @click="loadAll">重算对照台</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p v-if="message" class="tip-text">{{ message }}</p>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div v-for="group in boardGroups" :key="group.观测类型" class="type-group">
      <h3 class="type-title">{{ group.观测类型 }}</h3>
      <div class="well-grid">
        <article v-for="well in group.观测孔" :key="well.well_id" class="well-card">
          <div class="well-head">
            <div>
              <strong>{{ well.井号 }}</strong>
              <span class="well-sub">{{ well.所在钻孔 }} · {{ well.观测类型 }}</span>
            </div>
            <button class="link" type="button" @click="openSeries(well)">
              长序列{{ seriesOpen === well.well_id ? '▲' : '▼' }}
            </button>
          </div>

          <div class="well-metrics">
            <span :class="['metric', well.统计.偏离天数 ? 'metric-bad' : '']">
              偏离 {{ well.统计.偏离天数 }} 天
            </span>
            <span :class="['metric', well.统计.待补测数量 ? 'metric-warn' : '']">
              待补测 {{ well.统计.待补测数量 }}
            </span>
            <span class="metric metric-ok">序列 {{ well.统计.序列总数 }} 条</span>
          </div>

          <div class="chart-scroll">
            <svg
              :viewBox="`0 0 ${chartWidth(well)} 132`"
              :width="chartWidth(well)"
              height="132"
              class="timeline"
            >
              <!-- 偏离带 -->
              <rect
                :x="0" :y="yOf(well, well.偏离带.上限)"
                :width="chartWidth(well)"
                :height="yOf(well, well.偏离带.下限) - yOf(well, well.偏离带.上限)"
                class="band"
              />
              <line :x1="0" :x2="chartWidth(well)" :y1="yOf(well, well.基准水位)"
                    :y2="yOf(well, well.基准水位)" class="baseline" />

              <!-- 偏离段（点选派发） -->
              <rect
                v-for="seg in well.偏离段"
                :key="seg.id"
                :x="slotX(well, seg.开始时间) - STEP / 2"
                :y="6"
                :width="(seg.偏离天数) * STEP"
                :height="110"
                :class="['dev-seg', `dev-${segState(seg)}`, { selected: selectedIds.has(seg.id) }]"
                @click="toggleSegment(seg)"
              >
                <title>{{ seg.开始时间 }} ~ {{ seg.结束时间 }}，{{ seg.偏离天数 }} 天（{{ seg.状态 }}）</title>
              </rect>

              <!-- 时间轴数据点 -->
              <circle
                v-for="p in well.时间轴"
                :key="p.时隙"
                :cx="slotX(well, p.时隙)"
                :cy="p.水位值 === null ? 116 : yOf(well, p.水位值)"
                :r="p.水位值 === null ? 3 : 3.2"
                :class="['point', `point-${pointClass(p)}`]"
              >
                <title>{{ p.时隙 }} {{ p.状态 }}：{{ p.水位值 ?? '缺测' }}{{ p.已复核 ? '（已复核）' : '' }}</title>
              </circle>
            </svg>
          </div>

          <div class="seg-list">
            <template v-for="seg in well.偏离段" :key="'t' + seg.id">
              <span :class="['seg-tag', `tag-${segState(seg)}`]" @click="toggleSegment(seg)">
                {{ seg.开始时间.slice(5) }}~{{ seg.结束时间.slice(5) }}
                （{{ seg.偏离天数 }}天·{{ seg.状态 }}{{ seg.任务编号 ? ' ' + seg.任务编号 : '' }}）
              </span>
            </template>
            <em v-if="!well.偏离段.length" class="seg-empty">暂无偏离段，水位运行正常</em>
          </div>

          <div class="well-actions">
            <button
              class="btn primary small"
              type="button"
              :disabled="!selectableInWell(well).length"
              @click="dispatchSelected(well)"
            >
              派发补测（已选 {{ selectedInWell(well).length }} 段）
            </button>
          </div>

          <!-- 长序列分页 -->
          <div v-if="seriesOpen === well.well_id && seriesData" class="series-panel">
            <div class="series-head">
              <strong>长序列分页</strong>
              <span>总条数 <b>{{ seriesData.total }}</b>；最新序列
                <b>{{ seriesData.最新序列?.观测编号 }}
                  {{ seriesData.最新序列?.观测时间?.slice(0, 10) }}
                  （{{ seriesData.最新序列?.水位值 }}）</b>
              </span>
            </div>
            <table class="data-table mini">
              <thead>
                <tr><th>观测编号</th><th>观测时间</th><th>原始水位</th><th>生效值</th><th>复核</th><th>操作</th></tr>
              </thead>
              <tbody>
                <tr v-for="r in seriesData.items" :key="r.id">
                  <td>{{ r.观测编号 }}</td>
                  <td>{{ r.观测时间 }}</td>
                  <td>{{ r.原始水位 }}</td>
                  <td>{{ r.水位值 }}</td>
                  <td>{{ r.已复核 ? `${r.复核人} ${r.复核时间?.slice(0, 10)}` : '—' }}</td>
                  <td>
                    <button
                      v-if="isOutlier(well, r) && !r.已复核"
                      class="link" type="button" @click="openReview(r)"
                    >人工复核</button>
                    <span v-else>—</span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="pager">
              <button class="btn small" type="button" :disabled="seriesPage <= 1" @click="flipPage(-1)">更早一页</button>
              <span>第 {{ seriesData.page }} / {{ Math.max(1, Math.ceil(seriesData.total / seriesData.size)) }} 页</span>
              <button
                class="btn small" type="button"
                :disabled="seriesData.page * seriesData.size >= seriesData.total"
                @click="flipPage(1)"
              >更新一页</button>
            </div>
          </div>
        </article>
      </div>
    </div>

    <div class="lower-grid">
      <section class="panel">
        <h3 class="panel-title">补测待办清单</h3>
        <table class="data-table mini">
          <thead>
            <tr><th>任务编号</th><th>井号</th><th>偏离段</th><th>指派</th><th>要求完成</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in tasks" :key="t.id">
              <td>{{ t.任务编号 }}</td>
              <td>{{ t.井号 }}</td>
              <td>
                <span v-for="m in t.偏离段" :key="m.id" class="seg-tag tag-pending">
                  {{ m.开始时间.slice(5) }}~{{ m.结束时间.slice(5) }}
                </span>
              </td>
              <td>{{ t.指派人员 }}</td>
              <td>{{ t.要求完成时间 ?? '—' }}</td>
              <td>{{ t.状态 }}</td>
              <td>
                <button v-if="t.状态 === '待补测'" class="link" type="button" @click="finishTask(t.id)">
                  回填并完成
                </button>
                <span v-else>{{ t.补测时间?.slice(0, 10) }}</span>
              </td>
            </tr>
            <tr v-if="!tasks.length"><td colspan="7" class="empty-state">暂无补测任务</td></tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <h3 class="panel-title">水文观测台账（偏离结论同步）</h3>
        <table class="data-table mini">
          <thead>
            <tr><th>观测编号</th><th>钻孔</th><th>状态</th><th>偏离天数</th><th>待补测</th><th>最新偏离结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="r in ledger" :key="r.id">
              <td>{{ r['观测编号'] }}</td>
              <td>{{ r['所在钻孔'] }}</td>
              <td>{{ r['观测状态'] }}</td>
              <td>{{ r['偏离天数'] }}</td>
              <td>{{ r['待补测数量'] }}</td>
              <td class="conclusion">{{ r['最新偏离结论'] }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <!-- 人工复核弹窗 -->
    <div v-if="reviewTarget" class="modal-mask" @click.self="reviewTarget = null">
      <div class="modal">
        <h3>人工复核 · {{ reviewTarget.观测编号 }}</h3>
        <p class="page-desc">原始观测值 {{ reviewTarget.原始水位 }} 保留在历史曲线上，不追溯改写；
          复核值生效后重新判定偏离。</p>
        <label class="filter-item"><span>复核值</span>
          <input v-model.number="reviewValue" type="number" step="0.01" />
        </label>
        <label class="filter-item"><span>复核人</span>
          <input v-model="reviewReviewer" placeholder="值班管理员" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="reviewTarget = null">取消</button>
          <button class="btn primary" type="button" @click="submitReview">提交复核并重算</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Point = {
  时隙: string; 观测编号: string | null; 原始水位: number | null
  水位值: number | null; 已复核: boolean; 复核人: string; 状态: string
}
type Segment = {
  id: number; well_id: number; 开始时间: string; 结束时间: string
  偏离天数: number; 缺测天数: number; 超限天数: number
  状态: string; 任务编号: string | null; 可派发: boolean
}
type WellView = {
  well_id: number; 井号: string; 观测类型: string; 所在钻孔: string
  基准水位: number; 偏离带: { 下限: number; 上限: number }; 观测周期: number
  时间轴: Point[]; 偏离段: Segment[]
  统计: { 偏离天数: number; 待补测数量: number; 序列总数: number; 最新序列: Record<string, unknown> | null }
}
type SeriesRow = {
  id: number; 观测编号: string; 观测时间: string; 回传时间: string | null
  原始水位: number; 水位值: number; 已复核: boolean
  复核人: string | null; 复核时间: string | null
}
type TaskView = {
  id: number; 任务编号: string; 井号: string; 观测类型: string; 所在钻孔: string
  状态: string; 指派人员: string; 要求完成时间: string | null
  派发时间: string; 补测时间: string | null; 偏离段: Segment[]
}
type LedgerRow = {
  id: number; 观测编号: string; 所在钻孔: string; 观测状态: string
  偏离天数: number; 待补测数量: number; 最新偏离结论: string
}
type Board = {
  horizon: string
  观测类型分组: { 观测类型: string; 观测孔: WellView[] }[]
  汇总: Record<string, number>
}

const STEP = 14
const ENDPOINT = '/api/hydro-compare'

const board = ref<Board>({ horizon: '', 观测类型分组: [], 汇总: {} })
const tasks = ref<TaskView[]>([])
const ledger = ref<LedgerRow[]>([])
const selectedIds = ref<Set<number>>(new Set())
const message = ref('')
const errorMessage = ref('')

const seriesOpen = ref<number | null>(null)
const seriesData = ref<{
  items: SeriesRow[]; total: number; page: number; size: number
  最新序列: { 观测编号: string; 观测时间: string; 水位值: number } | null
} | null>(null)
const seriesPage = ref(1)
const seriesSize = 12

const reviewTarget = ref<Pick<SeriesRow, '观测编号' | '原始水位'> | null>(null)
const reviewValue = ref<number>(0)
const reviewReviewer = ref('李复核')

const boardGroups = computed(() => board.value.观测类型分组)
const statCards = computed(() => {
  const s = board.value.汇总
  return [
    { label: '对照观测孔', value: s['对照观测孔'] ?? 0 },
    { label: '水位偏离天数', value: s['水位偏离天数'] ?? 0 },
    { label: '待补测任务', value: s['待补测任务'] ?? 0 },
    { label: '已补测偏离段', value: s['已补测偏离段'] ?? 0 },
  ]
})

// ---------------------------------------------------------------- 图表
function chartWidth(well: WellView) {
  return Math.max(well.时间轴.length, 1) * STEP + 10
}
function slotX(well: WellView, day: string) {
  const idx = well.时间轴.findIndex((p) => p.时隙 === day)
  return (idx < 0 ? 0 : idx) * STEP + STEP / 2 + 5
}
function yOf(well: WellView, value: number) {
  const low = well.偏离带.下限
  const high = well.偏离带.上限
  const pad = Math.max(high - low, 0.2)
  const values = well.时间轴.map((p) => p.水位值).filter((v): v is number => v !== null)
  const min = Math.min(low - pad * 0.5, ...values)
  const max = Math.max(high + pad * 0.5, ...values)
  return 106 - ((value - min) / (max - min || 1)) * 96 + 8
}
function pointClass(p: Point) {
  return { 缺测: 'missing', 超限: 'outlier', 待观测: 'pending', 正常: 'ok' }[p.状态] ?? 'ok'
}
function segState(seg: Segment) {
  return { 偏离中: 'open', 待补测: 'pending', 已补测: 'filled', 已消除: 'cleared' }[seg.状态] ?? 'open'
}
function selectableInWell(well: WellView) {
  return well.偏离段.filter((s) => s.可派发 && selectedIds.value.has(s.id))
}
function selectedInWell(well: WellView) {
  return well.偏离段.filter((s) => selectedIds.value.has(s.id))
}
function toggleSegment(seg: Segment) {
  if (!seg.可派发) {
    message.value = seg.任务编号 ? `该段已派发任务 ${seg.任务编号}，不能重复派发` : `偏离段当前为「${seg.状态}」`
    return
  }
  const next = new Set(selectedIds.value)
  if (next.has(seg.id)) next.delete(seg.id)
  else next.add(seg.id)
  selectedIds.value = next
}
function isOutlier(well: WellView, row: { 原始水位: number; 已复核: boolean }) {
  return !row.已复核 && (row.原始水位 < well.偏离带.下限 || row.原始水位 > well.偏离带.上限)
}

// ---------------------------------------------------------------- 数据加载
async function loadAll() {
  errorMessage.value = ''
  try {
    const [b, t, l] = await Promise.all([
      request(`${ENDPOINT}/board`).then((r) => r.json()),
      request(`${ENDPOINT}/tasks`).then((r) => r.json()),
      request('/api/hydro?size=200').then((r) => r.json()),
    ])
    board.value = b
    tasks.value = t.items
    ledger.value = l.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对照台加载失败'
  }
}

async function openSeries(well: WellView) {
  if (seriesOpen.value === well.well_id) {
    seriesOpen.value = null
    seriesData.value = null
    return
  }
  seriesOpen.value = well.well_id
  seriesPage.value = 1
  await loadSeries(well.well_id)
}

async function loadSeries(wellId: number) {
  const r = await request(`${ENDPOINT}/wells/${wellId}/series?page=${seriesPage.value}&size=${seriesSize}&order=desc`)
  if (!r.ok) throw new Error('长序列读取失败')
  seriesData.value = await r.json()
}

async function flipPage(delta: number) {
  seriesPage.value = Math.max(1, seriesPage.value + delta)
  if (seriesOpen.value !== null) await loadSeries(seriesOpen.value)
}

// ---------------------------------------------------------------- 动作
async function postJson(path: string, body: unknown, okText: string) {
  errorMessage.value = ''
  const r = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await r.json()
  if (!r.ok || payload.ok === false) {
    throw new Error(payload.detail || payload.message || '操作未生效')
  }
  message.value = okText || payload.message
  await loadAll()
  if (seriesOpen.value !== null) await loadSeries(seriesOpen.value)
  return payload
}

async function dispatchSelected(well: WellView) {
  const ids = selectableInWell(well).map((s) => s.id)
  if (!ids.length) return
  try {
    await postJson(`${ENDPOINT}/retests`, {
      偏离段id: ids,
      指派人员: '水文补测组',
      要求完成时间: board.value.horizon,
    }, '')
    selectedIds.value = new Set([...selectedIds.value].filter((id) => !ids.includes(id)))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '派发失败'
  }
}

async function finishTask(id: number) {
  try {
    await postJson(`${ENDPOINT}/tasks/${id}/complete`, {}, '')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '完成失败'
  }
}

function openReview(row: { 观测编号: string; 原始水位: number }) {
  reviewTarget.value = row
  reviewValue.value = 12.0
  reviewReviewer.value = '李复核'
}

async function submitReview() {
  if (!reviewTarget.value) return
  try {
    await postJson(`${ENDPOINT}/reviews`, {
      观测编号: reviewTarget.value.观测编号,
      复核值: reviewValue.value,
      复核人: reviewReviewer.value,
    }, '')
    reviewTarget.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核失败'
  }
}

// 乱序回传：两条迟到补数（SW-01 的 09-11/09-12），另带一条已存在编号演示幂等跳过
async function simulateOutOfOrder() {
  try {
    const payload = await postJson(`${ENDPOINT}/observations`, {
      observations: [
        { 观测编号: 'OBS-01-0912', well_id: 1, 观测时间: '2026-09-12T10:00:00', 水位值: 12.05 },
        { 观测编号: 'OBS-01-0911', well_id: 1, 观测时间: '2026-09-11T10:00:00', 水位值: 12.02 },
        { 观测编号: 'OBS-01-0910', well_id: 1, 观测时间: '2026-09-10T08:00:00', 水位值: 12.0 },
      ],
    }, '')
    message.value = `${payload.message}（迟到数据已按观测时间归位，开放偏离段自动消除）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回传失败'
  }
}

onMounted(loadAll)
</script>
