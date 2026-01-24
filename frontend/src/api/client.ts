import axios, { type AxiosInstance, type AxiosError } from 'axios'

declare global {
  interface Window {
    __G2API_CONFIG__?: {
      // Path prefix where the app is mounted, e.g. "/gemini2api". Empty means "/".
      basePath?: string
      // Base URL for API calls. Defaults to basePath if omitted.
      apiBase?: string
    }
  }
}

function normalizeBaseUrl(url: string): string {
  // Keep it simple: allow "", "/x", "/x/y", "https://host/x".
  // Remove trailing slashes to make axios URL joining consistent.
  return String(url || '').replace(/\/+$/, '')
}

const runtimeApiBase =
  window.__G2API_CONFIG__?.apiBase ??
  window.__G2API_CONFIG__?.basePath ??
  ''

// 创建 axios 实例
export const apiClient: AxiosInstance = axios.create({
  // Runtime config > build-time config.
  baseURL: normalizeBaseUrl(runtimeApiBase || import.meta.env.VITE_API_URL || ''),
  timeout: 30000,
  withCredentials: true, // 支持 cookie 认证
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    // 可以在这里添加 token 等认证信息
    return config
  },
  (error) => {
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    return response.data
  },
  async (error: AxiosError) => {
    // 统一错误处理
    if (error.response?.status === 401) {
      const { useAuthStore } = await import('@/stores/auth')
      const authStore = useAuthStore()
      authStore.isLoggedIn = false

      const router = await import('@/router')
      router.default.push('/login')
    }

    const errorMessage = error.response?.data
      ? (error.response.data as any).detail || (error.response.data as any).message
      : error.message

    return Promise.reject(new Error(errorMessage || '请求失败'))
  }
)

export default apiClient
