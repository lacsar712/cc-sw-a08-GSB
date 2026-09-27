<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'

const role = ref(localStorage.getItem('role') || '')
const status = ref(null)
const windows = ref([])
const traces = ref([])
const err = ref('')
const form = ref({ start_minute: '', end_minute: '', note: '' })
const drafts = ref({})
let timer

const isWriter = computed(() => role.value === 'writer')

function hhmm(v) {
  return v ? v.slice(11, 19) : ''
}
function ymd(v) {
  return v ? v.slice(0, 10) : ''
}

function draftOf(w) {
  if (!drafts.value[w.id]) {
    drafts.value[w.id] = {
      start_minute: w.start_minute,
      end_minute: w.end_minute,
      note: w.note || '',
    }
  }
  return drafts.value[w.id]
}

async function refresh() {
  if (!localStorage.getItem('tok')) return
  try {
    const [s, ws, ts] = await Promise.all([
      api('/api/blackout/status'),
      api('/api/blackout/windows'),
      api('/api/blackout/traces'),
    ])
    status.value = s
    windows.value = ws
    traces.value = ts
    role.value = s.role || localStorage.getItem('role') || ''
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function addWindow() {
  err.value = ''
  try {
    await api('/api/blackout/windows', { method: 'POST', body: JSON.stringify(form.value) })
    form.value = { start_minute: '', end_minute: '', note: '' }
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function saveWindow(w) {
  err.value = ''
  try {
    await api(`/api/blackout/windows/${w.id}`, {
      method: 'POST',
      body: JSON.stringify(draftOf(w)),
    })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function removeWindow(w) {
  err.value = ''
  try {
    await api(`/api/blackout/windows/${w.id}/delete`, { method: 'POST' })
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 2000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <h2>钟点禁写窗</h2>
    <p v-if="err" style="color:#b00020">{{ err }}</p>

    <section style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>此刻是否禁写（服务端对表）</h3>
      <template v-if="status">
        <p>
          服务器钟点：<strong>{{ status.server_minute }}</strong>
          <span class="hint">（{{ status.server_time }}，每日闭区间，端点也命中）</span>
        </p>
        <p v-if="status.blocked" style="color:#b00020; font-weight:700;">
          禁写中：命中区间 {{ status.window.start_minute }}–{{ status.window.end_minute }}
          <template v-if="status.window.note">（{{ status.window.note }}）</template>
          ，此刻提交一律拒收，请把区间挪到窗外再提交。
        </p>
        <p v-else style="color:#0a7a2f; font-weight:700;">
          当前不在任何禁写窗内，提交放行。
        </p>
      </template>
    </section>

    <section style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>区间配置</h3>
      <p v-if="!isWriter" class="hint">
        观察账号只读：可查看钟点配置与禁写痕迹，不能修改区间。
      </p>
      <table v-if="windows.length" border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">
        <thead>
          <tr>
            <th>编号</th><th>起</th><th>止</th><th>说明</th><th>设立人</th><th v-if="isWriter">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="w in windows" :key="w.id">
            <td>{{ w.id }}</td>
            <template v-if="isWriter">
              <td><input v-model="draftOf(w).start_minute" size="6" placeholder="HH:MM" /></td>
              <td><input v-model="draftOf(w).end_minute" size="6" placeholder="HH:MM" /></td>
              <td><input v-model="draftOf(w).note" size="14" /></td>
            </template>
            <template v-else>
              <td>{{ w.start_minute }}</td>
              <td>{{ w.end_minute }}</td>
              <td>{{ w.note }}</td>
            </template>
            <td>{{ w.created_by }}</td>
            <td v-if="isWriter">
              <button type="button" @click="saveWindow(w)">保存</button>
              <button type="button" @click="removeWindow(w)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">尚无禁写区间。</p>

      <div v-if="isWriter" style="margin-top:10px;">
        <strong>新增每日禁写闭区间：</strong>
        <label>起 <input v-model="form.start_minute" size="6" placeholder="HH:MM" /></label>
        <label>止 <input v-model="form.end_minute" size="6" placeholder="HH:MM" /></label>
        <label>说明 <input v-model="form.note" size="14" /></label>
        <button type="button" @click="addWindow">设立</button>
        <p class="hint">起可晚于止，表示跨午夜区间（如 23:00–02:00）。</p>
      </div>
    </section>

    <section style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>禁写痕迹</h3>
      <table v-if="traces.length" border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">
        <thead>
          <tr>
            <th>编号</th><th>服务器日期</th><th>钟点</th><th>灯种</th>
            <th>标称</th><th>实测</th><th>提交人</th><th>命中区间</th><th>拒收说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in traces" :key="t.id">
            <td>{{ t.id }}</td>
            <td>{{ ymd(t.attempted_at) }}</td>
            <td>{{ hhmm(t.attempted_at) }}</td>
            <td>{{ t.lamp }}</td>
            <td>{{ t.nominal_nm }}</td>
            <td>{{ t.measured_nm }}</td>
            <td>{{ t.attempted_by }}</td>
            <td>{{ t.start_minute }}–{{ t.end_minute }}</td>
            <td>{{ t.reason }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="hint">暂无拒收痕迹。</p>
    </section>
  </div>
</template>

<style scoped>
.hint {
  color: #666;
  font-size: 13px;
}
</style>
