<script setup lang="ts">
import { ref, onMounted } from 'vue'
import api from '@/api'

const stats = ref({ groups: 0, documents: 0, users: 0, searches: 0 })

onMounted(async () => {
  try {
    const [groups, users, docs, audit] = await Promise.all([
      api.get('/api/kb/groups'),
      api.get('/api/users'),
      api.get('/api/kb/documents'),
      api.get('/api/kb/audit'),
    ])
    stats.value.groups = (groups as any[]).length
    stats.value.users = (users as any[]).length
    stats.value.documents = Array.isArray(docs) ? docs.length : 0
    stats.value.searches = Array.isArray(audit) ? audit.length : 0
  } catch {}
})
</script>

<template>
  <h2>📊 知识库概览</h2>
  <el-row :gutter="20" style="margin-top:20px">
    <el-col :span="6"><el-card><div style="text-align:center"><h1 style="color:#409EFF">{{ stats.groups }}</h1>知识库分组</div></el-card></el-col>
    <el-col :span="6"><el-card><div style="text-align:center"><h1 style="color:#67C23A">{{ stats.documents }}</h1>文档数</div></el-card></el-col>
    <el-col :span="6"><el-card><div style="text-align:center"><h1 style="color:#E6A23C">{{ stats.users }}</h1>用户数</div></el-card></el-col>
    <el-col :span="6"><el-card><div style="text-align:center"><h1 style="color:#F56C6C">{{ stats.searches }}</h1>本月检索量</div></el-card></el-col>
  </el-row>
</template>
