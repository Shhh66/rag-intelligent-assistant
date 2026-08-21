<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useRouter } from 'vue-router'

const auth = useAuthStore()
const router = useRouter()
const username = ref('')
const password = ref('')
const loading = ref(false)

async function doLogin() {
  loading.value = true
  try {
    await auth.login(username.value, password.value)
    router.push('/')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div style="display:flex;justify-content:center;align-items:center;height:100vh;background:#f0f2f5">
    <el-card style="width:400px">
      <h2 style="text-align:center">RAG 权限管理系统</h2>
      <el-form @submit.prevent="doLogin" style="margin-top:24px">
        <el-form-item><el-input v-model="username" placeholder="用户名" prefix-icon="User" /></el-form-item>
        <el-form-item><el-input v-model="password" type="password" placeholder="密码" prefix-icon="Lock" show-password /></el-form-item>
        <el-form-item><el-button type="primary" @click="doLogin" :loading="loading" style="width:100%">登 录</el-button></el-form-item>
      </el-form>
    </el-card>
  </div>
</template>
