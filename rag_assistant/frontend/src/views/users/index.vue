<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import api from '@/api'

const users = ref<any[]>([])
const allGroups = ref<any[]>([])
const dialogVisible = ref(false)
const editMode = ref(false)
const editId = ref(0)
const form = ref({ username: '', password: '', department: '' })
const selectedGroups = ref<number[]>([])

async function loadUsers() {
  const res = await api.get('/api/users')
  users.value = res as any[]
}

async function loadGroups() {
  try { allGroups.value = (await api.get('/api/kb/groups')) as any[] }
  catch { allGroups.value = [] }
}

async function createUser() {
  await api.post('/api/users', form.value)
  dialogVisible.value = false
  resetForm()
  loadUsers()
}

async function updateUser() {
  await api.put(`/api/users/${editId.value}`, {
    username: form.value.username,
    department: form.value.department,
  })
  // 同步更新知识库分组
  await api.put(`/api/users/${editId.value}/groups`, selectedGroups.value)
  dialogVisible.value = false
  resetForm()
  loadUsers()
}

async function deleteUser(id: number, name: string) {
  try {
    await ElMessageBox.confirm(`确定删除用户 "${name}"？`, '确认', { type: 'warning' })
    await api.delete(`/api/users/${id}`)
    loadUsers()
    ElMessage.success('已删除')
  } catch { /* cancelled */ }
}

async function toggleActive(row: any) {
  await api.put(`/api/users/${row.id}`, { is_active: !row.is_active })
  loadUsers()
}

async function openEdit(row: any) {
  editMode.value = true
  editId.value = row.id
  form.value = { username: row.username, password: '', department: row.department || '' }
  // 加载该用户当前的分组
  try {
    const gs = (await api.get(`/api/users/${row.id}/groups`)) as any[]
    selectedGroups.value = gs.map((g: any) => g.id)
  } catch { selectedGroups.value = [] }
  await loadGroups()
  dialogVisible.value = true
}

function openCreate() {
  editMode.value = false
  resetForm()
  loadGroups()
  dialogVisible.value = true
}

function resetForm() {
  form.value = { username: '', password: '', department: '' }
  selectedGroups.value = []
  editId.value = 0
}

onMounted(() => { loadUsers(); loadGroups() })
</script>

<template>
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h2>👥 用户管理</h2>
    <el-button type="primary" @click="openCreate">新建用户</el-button>
  </div>
  <el-table :data="users" style="margin-top:16px" border>
    <el-table-column prop="id" label="ID" width="60" />
    <el-table-column prop="username" label="用户名" />
    <el-table-column prop="department" label="部门" />
    <el-table-column prop="is_active" label="状态" width="80">
      <template #default="{ row }">
        <el-switch :model-value="!!row.is_active" @change="toggleActive(row)" />
      </template>
    </el-table-column>
    <el-table-column prop="created_at" label="创建时间" width="180" />
    <el-table-column label="操作" width="160">
      <template #default="{ row }">
        <el-button size="small" @click="openEdit(row)">编辑</el-button>
        <el-button size="small" type="danger" @click="deleteUser(row.id, row.username)">删除</el-button>
      </template>
    </el-table-column>
  </el-table>

  <el-dialog v-model="dialogVisible" :title="editMode ? '编辑用户' : '新建用户'" width="480px">
    <el-form>
      <el-form-item label="用户名"><el-input v-model="form.username" /></el-form-item>
      <el-form-item v-if="!editMode" label="密码"><el-input v-model="form.password" type="password" show-password /></el-form-item>
      <el-form-item label="部门"><el-input v-model="form.department" /></el-form-item>
      <el-form-item v-if="editMode" label="知识库分组">
        <el-checkbox-group v-model="selectedGroups">
          <el-checkbox v-for="g in allGroups" :key="g.id" :label="g.id" :value="g.id">
            {{ g.name }}
            <el-tag size="small" :type="g.visibility==='public'?'success':'warning'" style="margin-left:4px">
              {{ g.visibility === 'public' ? '公开' : '内部' }}
            </el-tag>
          </el-checkbox>
        </el-checkbox-group>
        <div v-if="allGroups.length === 0" style="color:#999;font-size:13px;margin-top:4px">
          暂无分组，请先到「知识库分组」页面创建
        </div>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="editMode ? updateUser() : createUser()">确定</el-button>
    </template>
  </el-dialog>
</template>
