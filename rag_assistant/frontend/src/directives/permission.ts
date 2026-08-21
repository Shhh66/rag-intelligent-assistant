import { useAuthStore } from '@/stores/auth'
import type { DirectiveBinding } from 'vue'

export const vPermission = {
  mounted(el: HTMLElement, binding: DirectiveBinding) {
    const { value } = binding
    if (!value) return
    const auth = useAuthStore()
    if (!auth.hasPermission(value)) {
      el.parentNode?.removeChild(el)
    }
  },
}
