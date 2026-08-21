<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import api from '@/api'

const roles = ref<any[]>([])
const editDialog = ref(false)
const editRole = ref<any>(null)
const editPerms = ref<string[]>([])
const allPerms = ['upload', 'delete_doc', 'search_all', 'search_group', 'manage_users', 'manage_kb', 'view_audit', 'export']

onMounted(loadRoles)

async function loadRoles() {
  const res = await api.get('/api/roles')
  roles.value = (res as any[]).map(r => ({
    ...r,
    perms: JSON.parse(r.permissions),
  }))
}

function openEdit(role: any) {
  editRole.value = role
  editPerms.value = [...role.perms]
  editDialog.value = true
}

async function saveRole() {
  await api.put(`/api/roles/${editRole.value.id}`, { permissions: editPerms.value })
  editDialog.value = false
  loadRoles()
}

async function createRole() {
  await api.post('/api/roles')
  loadRoles()
}

async function deleteRole(id: number) {
  try {
    await ElMessageBox.confirm('确定删除此角色？', '确认', { type: 'warning' })
    await api.delete(`/api/roles/${id}`)
    loadRoles()
  } catch { /* cancelled */ }
}
</script>

<template>
  <div style="display:flex;justify-content:space-between;align-items:center">
    <h2>🔐 角色权限配置</h2>
    <el-button type="primary" @click="createRole">新建角色</el-button>
  </div>

  <el-table :data="roles" style="margin-top:16px" border>
    <el-table-column prop="id" label="ID" width="60" />
    <el-table-column prop="name" label="角色名" width="120" />
    <el-table-column label="权限">
      <template #default="{ row }">
        <el-tag v-for="p in row.perms" :key="p" size="small" style="margin-right:4px;margin-bottom:2px">{{ p }}</el-tag>
      </template>
    </el-table-column>
    <el-table-column label="操作" width="160">
      <template #default="{ row }">
        <el-button size="small" @click="openEdit(row)" :disabled="row.id <= 3">编辑权限</el-button>
        <el-button size="small" type="danger" @click="deleteRole(row.id)" :disabled="row.id <= 3">删除</el-button>
      </template>
    </el-table-column>
  </el-table>

  <!-- 权限编辑弹窗 -->
  <el-dialog v-model="editDialog" :title="'编辑权限 - ' + (editRole?.name || '')" width="500px">
    <el-checkbox-group v-model="editPerms">
      <el-checkbox v-for="p in allPerms" :key="p" :label="p" :value="p" style="margin-right:16px;margin-bottom:8px">
        {{ p }}
      </el-checkbox>
    </el-checkbox-group>
    <template #footer>
      <el-button @click="editDialog = false">取消</el-button>
      <el-button type="primary" @click="saveRole">保存</el-button>
    </template>
  </el-dialog>
</template>
