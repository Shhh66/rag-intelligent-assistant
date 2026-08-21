<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessageBox, ElMessage } from 'element-plus'
import api from '@/api'

const route = useRoute()
const filterGroup = computed(() => (route.query.group as string) || '')
const docs = ref<any[]>([])
const filteredDocs = computed(() => {
  if (!filterGroup.value) return docs.value
  return docs.value.filter((d: any) => d.kb_group === filterGroup.value)
})
const allGroups = ref<any[]>([])
const loading = ref(false)
const permDialog = ref(false)
const currentDoc = ref<any>(null)
const permForm = ref({ kb_group: '', visibility: 'internal' })
// 批量选择
const selectedDocs = ref<string[]>([])
const batchForm = ref({ kb_group: '', visibility: 'internal' })
const batchDialog = ref(false)
const addDocsDialog = ref(false)
const addSelected = ref<string[]>([])

// 未加入当前分组的文档
const notInGroup = computed(() => {
  if (!filterGroup.value) return []
  return docs.value.filter((d: any) => d.kb_group !== filterGroup.value)
})

async function addToGroup() {
  if (addSelected.value.length === 0) { ElMessage.warning('请选择文档'); return }
  for (const fp of addSelected.value) {
    await api.put(`/api/kb/documents/${encodeURIComponent(fp)}/permission`,
      { kb_group: filterGroup.value, visibility: 'internal' })
    // 立即更新本地数据，不等待 API 重新查询
    const doc = docs.value.find((d: any) => d.file_path === fp)
    if (doc) { doc.kb_group = filterGroup.value; doc.visibility = 'internal' }
  }
  ElMessage.success(`已将 ${addSelected.value.length} 个文档加入「${filterGroup.value}」`)
  addSelected.value = []
  addDocsDialog.value = false
  loadDocs()
}

async function loadDocs() {
  loading.value = true
  try {
    const res = await api.get('/api/kb/documents')
    docs.value = Array.isArray(res) ? res : []
  } catch { docs.value = [] }
  loading.value = false
}

async function loadGroups() {
  try { allGroups.value = (await api.get('/api/kb/groups')) as any[] }
  catch { allGroups.value = [] }
}

function openPerm(doc: any) {
  currentDoc.value = doc
  permForm.value = {
    kb_group: doc.kb_group || '',
    visibility: doc.visibility || 'internal',
  }
  permDialog.value = true
}

async function savePerm() {
  const fp = currentDoc.value.file_path
  await api.put(`/api/kb/documents/${encodeURIComponent(fp)}/permission`, permForm.value)
  // 立即更新本地
  const doc = docs.value.find((d: any) => d.file_path === fp)
  if (doc) { doc.kb_group = permForm.value.kb_group; doc.visibility = permForm.value.visibility }
  permDialog.value = false
  ElMessage.success('权限已更新')
  loadDocs()
}

async function batchAssign() {
  if (selectedDocs.value.length === 0) { ElMessage.warning('请先选择文档'); return }
  try {
    await ElMessageBox.confirm(
      `将 ${selectedDocs.value.length} 个文档批量设为「${batchForm.value.kb_group || '(清空)'}」/「${batchForm.value.visibility}」？`,
      '批量分配', { type: 'warning' },
    )
    for (const fp of selectedDocs.value) {
      await api.put(`/api/kb/documents/${encodeURIComponent(fp)}/permission`, batchForm.value)
    }
    ElMessage.success(`已批量更新 ${selectedDocs.value.length} 个文档`)
    selectedDocs.value = []
    loadDocs()
  } catch { /* cancelled */ }
}

async function deleteDoc(fp: string) {
  try {
    await ElMessageBox.confirm(`确定删除 "${fp}" 的所有 chunk？`, '确认', { type: 'warning' })
    await api.delete(`/api/kb/documents/${encodeURIComponent(fp)}`)
    ElMessage.success('已删除')
    loadDocs()
  } catch { /* cancelled */ }
}

onMounted(() => { loadDocs(); loadGroups() })
</script>

<template>
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h2>
      📄 文档管理
      <el-tag v-if="filterGroup" type="warning" size="large" style="margin-left:12px;vertical-align:middle">
        📁 {{ filterGroup }}
        <el-button text size="small" @click="$router.push('/kb/documents')" style="margin-left:4px">✕</el-button>
      </el-tag>
      <el-button v-if="filterGroup" size="small" type="success" @click="addDocsDialog = true" style="margin-left:8px">
        ＋ 添加文档到此分组
      </el-button>
    </h2>
    <div style="display:flex;gap:8px">
      <el-button @click="loadDocs" :loading="loading">🔄 刷新</el-button>
      <el-button type="primary" @click="batchDialog = true" :disabled="selectedDocs.length === 0">
        📦 批量分配 ({{ selectedDocs.length }})
      </el-button>
    </div>
  </div>

  <el-table
    :data="filteredDocs" style="margin-top:16px" border v-loading="loading"
    @selection-change="(rows: any[]) => selectedDocs = rows.map((r: any) => r.file_path)"
  >
    <el-table-column type="selection" width="40" />
    <el-table-column prop="file_path" label="文件路径" min-width="280" />
    <el-table-column prop="chunks" label="Chunks" width="70" />
    <el-table-column label="知识库分组" width="130">
      <template #default="{ row }">
        <el-tag v-if="row.kb_group" size="small" type="primary">{{ row.kb_group }}</el-tag>
        <span v-else style="color:#c0c4cc">未分配</span>
      </template>
    </el-table-column>
    <el-table-column label="可见性" width="90">
      <template #default="{ row }">
        <el-tag v-if="row.visibility" size="small"
          :type="row.visibility==='public'?'success':'warning'">
          {{ row.visibility === 'public' ? '公开' : '内部' }}
        </el-tag>
      </template>
    </el-table-column>
    <el-table-column prop="added_at" label="入库时间" width="170" />
    <el-table-column label="操作" width="140">
      <template #default="{ row }">
        <el-button size="small" @click="openPerm(row)">分配分组</el-button>
        <el-button size="small" type="danger" @click="deleteDoc(row.file_path)">删除</el-button>
      </template>
    </el-table-column>
  </el-table>
  <el-empty v-if="!loading && filteredDocs.length === 0"
    :description="filterGroup ? `「${filterGroup}」分组下暂无文档` : '暂无文档，请通过 Streamlit 上传'" />

  <!-- 单个文档权限编辑 -->
  <el-dialog v-model="permDialog" :title="'分配分组 - ' + (currentDoc?.file_path || '')" width="450px">
    <el-form>
      <el-form-item label="知识库分组">
        <el-select v-model="permForm.kb_group" placeholder="选择分组（留空=清空）" clearable style="width:100%">
          <el-option v-for="g in allGroups" :key="g.id" :label="g.name" :value="g.name" />
        </el-select>
      </el-form-item>
      <el-form-item label="可见性">
        <el-radio-group v-model="permForm.visibility">
          <el-radio value="public">公开 — 全员可检索</el-radio>
          <el-radio value="internal">内部 — 仅分组成员可检索</el-radio>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="permDialog = false">取消</el-button>
      <el-button type="primary" @click="savePerm">保存</el-button>
    </template>
  </el-dialog>

  <!-- 加入当前分组（筛选模式下显示） -->
  <el-dialog v-model="addDocsDialog" :title="'添加文档到「' + filterGroup + '」'" width="600px">
    <el-table
      :data="notInGroup" border max-height="400"
      @selection-change="(rows: any[]) => addSelected = rows.map((r: any) => r.file_path)"
    >
      <el-table-column type="selection" width="40" />
      <el-table-column prop="file_path" label="文件路径" min-width="250" />
      <el-table-column label="当前分组" width="120">
        <template #default="{ row }">
          {{ row.kb_group || '未分配' }}
        </template>
      </el-table-column>
      <el-table-column prop="chunks" label="Chunks" width="70" />
    </el-table>
    <el-empty v-if="notInGroup.length === 0" description="所有文档已加入此分组" />
    <template #footer>
      <el-button @click="addDocsDialog = false">取消</el-button>
      <el-button type="primary" @click="addToGroup" :disabled="addSelected.length === 0">
        加入「{{ filterGroup }}」({{ addSelected.length }})
      </el-button>
    </template>
  </el-dialog>

  <!-- 批量分配 -->
  <el-dialog v-model="batchDialog" title="批量分配分组" width="450px">
    <p style="margin-bottom:12px;color:#666">已选 <b>{{ selectedDocs.length }}</b> 个文档</p>
    <el-form>
      <el-form-item label="知识库分组">
        <el-select v-model="batchForm.kb_group" placeholder="选择分组（留空=清空）" clearable style="width:100%">
          <el-option v-for="g in allGroups" :key="g.id" :label="g.name" :value="g.name" />
        </el-select>
      </el-form-item>
      <el-form-item label="可见性">
        <el-radio-group v-model="batchForm.visibility">
          <el-radio value="public">公开</el-radio>
          <el-radio value="internal">内部</el-radio>
        </el-radio-group>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="batchDialog = false">取消</el-button>
      <el-button type="primary" @click="batchAssign">批量应用</el-button>
    </template>
  </el-dialog>
</template>
