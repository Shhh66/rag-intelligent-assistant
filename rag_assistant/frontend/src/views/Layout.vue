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
  <el-container style="height:100vh">
    <el-aside width="220px" style="background:#304156">
      <div style="padding:16px;color:white;font-size:18px;text-align:center">🔐 RAG 权限管理</div>
      <el-menu
        router
        :default-active="$route.path"
        background-color="#304156"
        text-color="#bfcbd9"
        active-text-color="#409EFF"
      >
        <el-menu-item index="/">
          <el-icon><DataAnalysis /></el-icon> 仪表盘
        </el-menu-item>
        <el-menu-item index="/users" v-if="auth.hasPermission('manage_users')">
          <el-icon><User /></el-icon> 用户管理
        </el-menu-item>
        <el-menu-item index="/roles" v-if="auth.hasPermission('manage_users')">
          <el-icon><Lock /></el-icon> 角色管理
        </el-menu-item>
        <el-menu-item index="/kb/groups">
          <el-icon><Folder /></el-icon> 知识库分组
        </el-menu-item>
        <el-menu-item index="/kb/documents">
          <el-icon><Document /></el-icon> 文档管理
        </el-menu-item>
        <el-menu-item index="/kb/audit" v-if="auth.hasPermission('view_audit')">
          <el-icon><Clock /></el-icon> 审计日志
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="display:flex;align-items:center;justify-content:flex-end;border-bottom:1px solid #eee">
        <span v-if="auth.user" style="margin-right:16px;cursor:pointer" @click="$router.push('/profile')">
          👤 {{ auth.user.username }}
        </span>
        <el-button @click="logout" type="danger" size="small">退出</el-button>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
