<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'
import { useRouter } from 'vue-router'

const auth = useAuthStore()
const router = useRouter()

function logout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <h2>👤 个人中心</h2>
  <el-card style="max-width:500px;margin-top:16px">
    <p><b>用户名：</b>{{ auth.user?.username }}</p>
    <p><b>用户ID：</b>{{ auth.user?.user_id }}</p>
    <p><b>权限：</b>
      <el-tag v-for="p in auth.permissions" :key="p" size="small" style="margin-right:4px">{{ p }}</el-tag>
    </p>
    <p><b>可访问知识库：</b>
      <el-tag v-for="g in auth.kbGroups" :key="g" size="small" type="success" style="margin-right:4px">{{ g }}</el-tag>
    </p>
    <el-divider />
    <el-button type="danger" @click="logout">退出登录</el-button>
  </el-card>
</template>
