<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import { useRouter } from 'vue-router'
import api from '@/api'

const router = useRouter()
const groups = ref<any[]>([])
const dialogVisible = ref(false)
const memberDialog = ref(false)
const permDialog = ref(false)
const renameDialog = ref(false)
const currentGroup = ref<any>(null)
const form = ref({ name: '', visibility: 'internal' })
const permForm = ref({ visibility: 'internal' })
const renameForm = ref({ name: '' })
const allUsers = ref<any[]>([])
const selectedUser = ref<number | null>(null)
const members = ref<any[]>([])

async function load() {
  const res = await api.get('/api/kb/groups')
  groups.value = res as any[]
}

async function create() {
  await api.post('/api/kb/groups', form.value)
  dialogVisible.value = false
  form.value = { name: '', visibility: 'internal' }
  load()
}

function showDocs(group: any) {
  router.push({ path: '/kb/documents', query: { group: group.name } })
}

function showPerm(group: any) {
  currentGroup.value = group
  permForm.value.visibility = group.visibility
  permDialog.value = true
}

async function savePerm() {
  await api.put(`/api/kb/groups/${currentGroup.value.id}`, { visibility: permForm.value.visibility })
  permDialog.value = false
  ElMessage.success('权限已更新')
  load()
}

async function showMembers(group: any) {
  currentGroup.value = group
  memberDialog.value = true
  try {
    allUsers.value = (await api.get('/api/users')) as any[]
    members.value = (await api.get(`/api/kb/groups/${group.id}/members`)) as any[]
  } catch { allUsers.value = []; members.value = [] }
}

async function addMember() {
  if (!selectedUser.value) return
  await api.put(`/api/kb/groups/${currentGroup.value.id}/members?action=add`,
    [selectedUser.value])
  selectedUser.value = null
  ElMessage.success('成员已添加')
  // 刷新成员列表
  try { members.value = (await api.get(`/api/kb/groups/${currentGroup.value.id}/members`)) as any[] } catch {}
  load()
}

async function removeMember(userId: number) {
  await api.put(`/api/kb/groups/${currentGroup.value.id}/members?action=remove`,
    [userId])
  ElMessage.success('成员已移除')
  try { members.value = (await api.get(`/api/kb/groups/${currentGroup.value.id}/members`)) as any[] } catch {}
  load()
}

function openRename(group: any) {
  currentGroup.value = group
  renameForm.value.name = group.name
  renameDialog.value = true
}

async function saveRename() {
  if (!renameForm.value.name.trim()) { ElMessage.warning('名称不能为空'); return }
  await api.put(`/api/kb/groups/${currentGroup.value.id}`, { name: renameForm.value.name })
  renameDialog.value = false
  ElMessage.success('已改名')
  load()
}

async function deleteGroup(id: number) {
  try {
    await ElMessageBox.confirm('确定删除此分组？', '确认', { type: 'warning' })
    await api.delete(`/api/kb/groups/${id}`)
    ElMessage.success('已删除')
    load()
  } catch { /* cancelled */ }
}

onMounted(load)
</script>

<template>
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h2>📁 知识库分组</h2>
    <el-button type="primary" @click="dialogVisible = true">＋ 新建分组</el-button>
  </div>

  <div v-for="g in groups" :key="g.id" style="margin-top:12px">
    <el-card>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <b style="font-size:16px">{{ g.name }}</b>
          <el-button size="small" text @click="openRename(g)" style="margin-left:4px;padding:0 4px" title="改名">✏️</el-button>
          <el-tag
            :type="g.visibility==='public'?'success':'warning'"
            style="margin-left:12px"
          >
            {{ g.visibility === 'public' ? '公开' : '内部' }}
          </el-tag>
        </div>
        <div style="display:flex;gap:8px">
          <el-button size="small" @click="showDocs(g)">📄 文档列表</el-button>
          <el-button size="small" @click="showMembers(g)">👥 成员管理</el-button>
          <el-button size="small" @click="showPerm(g)">🔒 权限设置</el-button>
          <el-button size="small" type="danger" @click="deleteGroup(g.id)">🗑 删除</el-button>
        </div>
      </div>

      <div style="margin-top:12px;color:#909399;font-size:13px">
        <span style="margin-right:24px">文档: {{ g.doc_count || 0 }}</span>
        <span style="margin-right:24px">成员: {{ g.member_count || 0 }}</span>
        <span>创建: {{ g.created_at }}</span>
      </div>
    </el-card>
  </div>

  <el-empty v-if="groups.length === 0" description="暂无分组，点击上方按钮新建" />

  <!-- 新建分组 -->
  <el-dialog v-model="dialogVisible" title="新建分组" width="400px">
    <el-form>
      <el-form-item label="名称"><el-input v-model="form.name" placeholder="如：研发部知识库" /></el-form-item>
      <el-form-item label="可见性">
        <el-select v-model="form.visibility">
          <el-option label="公开 (public)" value="public" />
          <el-option label="内部 (internal)" value="internal" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="create">确定</el-button>
    </template>
  </el-dialog>

  <!-- 权限设置 -->
  <el-dialog v-model="permDialog" :title="'权限设置 - ' + (currentGroup?.name || '')" width="400px">
    <el-form>
      <el-form-item label="可见性等级">
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

  <!-- 改名 -->
  <el-dialog v-model="renameDialog" :title="'改名 - ' + (currentGroup?.name || '')" width="400px">
    <el-form>
      <el-form-item label="新名称">
        <el-input v-model="renameForm.name" placeholder="输入新的分组名称" @keyup.enter="saveRename" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="renameDialog = false">取消</el-button>
      <el-button type="primary" @click="saveRename">确定</el-button>
    </template>
  </el-dialog>

  <!-- 成员管理 -->
  <el-dialog v-model="memberDialog" :title="'成员管理 - ' + (currentGroup?.name || '')">
    <div style="display:flex;gap:8px;margin-bottom:16px">
      <el-select v-model="selectedUser" placeholder="选择用户" style="flex:1">
        <el-option v-for="u in allUsers" :key="u.id" :label="u.username" :value="u.id" />
      </el-select>
      <el-button type="primary" @click="addMember">添加</el-button>
    </div>
    <el-table :data="members" empty-text="暂无成员，点击上方添加" style="margin-top:12px">
      <el-table-column prop="username" label="用户名" />
      <el-table-column label="操作" width="80">
        <template #default="{ row }">
          <el-button size="small" type="danger" @click="removeMember(row.id)">移除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>
</template>
