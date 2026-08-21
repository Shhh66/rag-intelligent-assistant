<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '@/api'

const logs = ref<any[]>([])

onMounted(async () => {
  try {
    const res = await api.get('/api/kb/audit')
    logs.value = res as any[]
  } catch { logs.value = [] }
})
</script>

<template>
  <h2>📊 审计日志</h2>
  <el-table :data="logs" style="margin-top:16px" border>
    <el-table-column prop="id" label="ID" width="60" />
    <el-table-column prop="user_id" label="用户ID" width="80" />
    <el-table-column prop="query" label="操作" min-width="200" />
    <el-table-column prop="result_count" label="结果数" width="80" />
    <el-table-column prop="timestamp" label="时间" width="180" />
  </el-table>
  <el-empty v-if="logs.length === 0" description="暂无审计记录" />
</template>
