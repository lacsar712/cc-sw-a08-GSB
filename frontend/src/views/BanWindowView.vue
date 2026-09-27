<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../api.js'

const role = ref(localStorage.getItem('role') || '')
const win = ref(null)
const traces = ref([])
const err = ref('')
const ok = ref('')
const form = ref({ start: '00:00', end: '00:00' })
const prefilled = ref(false)
let timer

const isWriter = computed(() => role.value === 'writer')

async function refresh() {
  if (!localStorage.getItem('tok')) return
  try {
    const [w, t] = await Promise.all([api('/api/ban-window'), api('/api/ban-traces')])
    win.value = w
    traces.value = t
    if (w.configured && !prefilled.value) {
      form.value = { start: w.start, end: w.end }
      prefilled.value = true
    }
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function save() {
  err.value = ''
  ok.value = ''
  try {
    await api('/api/ban-window', { method: 'PUT', body: JSON.stringify(form.value) })
    ok.value = `已保存每日禁写闭区间 ${form.value.start}–${form.value.end}`
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
}

function fmtTs(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return isNaN(d) ? iso : d.toLocaleString()
}

onMounted(() => {
  role.value = localStorage.getItem('role') || ''
  refresh()
  timer = setInterval(refresh, 1000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <h2>钟点闸 · 班次禁写窗</h2>
    <p v-if="err" style="color:#b00020">{{ err }}</p>
    <p v-if="ok" style="color:#1a7f37">{{ ok }}</p>

    <section style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>此刻是否禁写</h3>
      <p v-if="win">
        服务器时间：<b>{{ win.server_time }}</b>
        <span
          :style="{
            marginLeft: '12px',
            padding: '2px 10px',
            borderRadius: '4px',
            color: '#fff',
            background: win.banned_now ? '#b00020' : '#1a7f37',
          }"
        >{{ win.banned_now ? '禁写中' : '可提交' }}</span>
      </p>
      <p v-if="win && win.configured" class="hint">
        当前每日禁写闭区间：{{ win.start }}–{{ win.end }}（由 {{ win.updated_by }} 设置于 {{ fmtTs(win.updated_at) }}）
      </p>
      <p v-else-if="win" class="hint">尚未配置禁写区间，全天可提交。</p>
    </section>

    <section style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>区间配置（每日闭区间）</h3>
      <template v-if="isWriter">
        <label>起 <input type="time" v-model="form.start" /></label>
        <label>止 <input type="time" v-model="form.end" /></label>
        <button type="button" @click="save">保存区间</button>
        <p class="hint">闭区间含起止两端；起晚于止表示跨零点窗口。落在窗内的提交一律拒收并留痕。</p>
      </template>
      <template v-else>
        <p v-if="win && win.configured">每日禁写闭区间：{{ win.start }}–{{ win.end }}</p>
        <p v-else>尚未配置禁写区间。</p>
        <p class="hint">观察账号只许查看钟点配置与痕迹，不许改区间。</p>
      </template>
    </section>

    <section style="margin:16px 0; padding:12px; border:1px solid #ccc;">
      <h3>禁写痕迹</h3>
      <table border="1" cellpadding="6" style="border-collapse:collapse; width:100%;">
        <thead>
          <tr>
            <th>编号</th><th>时间</th><th>账号</th><th>灯种</th><th>标称</th><th>实测</th><th>命中禁写窗</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in traces" :key="t.id">
            <td>{{ t.id }}</td>
            <td>{{ fmtTs(t.attempted_at) }}</td>
            <td>{{ t.username }}</td>
            <td>{{ t.lamp }}</td>
            <td>{{ t.nominal_nm }}</td>
            <td>{{ t.measured_nm }}</td>
            <td>{{ t.window_start }}–{{ t.window_end }}</td>
          </tr>
          <tr v-if="!traces.length">
            <td colspan="7" style="text-align:center; color:#666;">暂无禁写痕迹</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.hint {
  color: #666;
  font-size: 13px;
}
label {
  margin-right: 12px;
}
</style>
