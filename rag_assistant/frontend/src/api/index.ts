import axios from 'axios'
import { useAuthStore } from '@/stores/auth'
import router from '@/router'

const service = axios.create({ baseURL: 'http://localhost:8000', timeout: 10000 })

service.interceptors.request.use(config => {
  const token = useAuthStore().token
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

service.interceptors.response.use(
  res => res.data,
  err => {
    if (err.response?.status === 401) {
      useAuthStore().logout()
      router.push('/login')
    }
    return Promise.reject(err)
  },
)

export default service
