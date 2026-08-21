import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'Login', component: () => import('@/views/login/index.vue') },
    {
      path: '/',
      component: () => import('@/views/Layout.vue'),
      children: [
        { path: '', name: 'Dashboard', component: () => import('@/views/dashboard/index.vue') },
        { path: 'users', name: 'Users', component: () => import('@/views/users/index.vue') },
        { path: 'roles', name: 'Roles', component: () => import('@/views/roles/index.vue') },
        { path: 'kb/groups', name: 'KbGroups', component: () => import('@/views/kb/groups.vue') },
        { path: 'kb/documents', name: 'KbDocs', component: () => import('@/views/kb/documents.vue') },
        { path: 'kb/audit', name: 'KbAudit', component: () => import('@/views/kb/audit.vue') },
    { path: 'profile', name: 'Profile', component: () => import('@/views/profile.vue') },
      ],
    },
  ],
})

router.beforeEach(async (to, _from, next) => {
  const auth = useAuthStore()
  if (!auth.token && to.path !== '/login') {
    return next('/login')
  }
  if (auth.token && !auth.initialized) {
    await auth.fetchPermissions()
  }
  next()
})

export default router
