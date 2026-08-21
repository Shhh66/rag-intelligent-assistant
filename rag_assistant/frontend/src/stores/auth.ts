import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '@/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user = ref<any>(null)
  const permissions = ref<string[]>([])
  const kbGroups = ref<string[]>([])

  const initialized = computed(() => permissions.value.length > 0)

  async function login(username: string, password: string) {
    const res = await api.post('/api/auth/login', { username, password })
    token.value = res.token
    user.value = res.user
    permissions.value = res.user.permissions
    kbGroups.value = res.user.kb_groups
    localStorage.setItem('token', res.token)
  }

  async function fetchPermissions() {
    const res = await api.get('/api/auth/me')
    user.value = res
    permissions.value = res.permissions
    kbGroups.value = res.kb_groups
  }

  function hasPermission(perm: string) {
    return permissions.value.includes(perm)
  }

  function logout() {
    token.value = ''
    user.value = null
    permissions.value = []
    kbGroups.value = []
    localStorage.removeItem('token')
  }

  return { token, user, permissions, kbGroups, initialized, login, fetchPermissions, hasPermission, logout }
})
