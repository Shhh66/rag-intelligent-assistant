// axios 类型增强
//
// src/api/index.ts 的响应拦截器做了 `res => res.data` 解包，
// 因此实例方法运行时返回的是业务数据本身，而非 AxiosResponse。
// 这里同步类型签名，否则 vue-tsc 会把每一处 res.xxx 都报成 TS2339。
import 'axios'

declare module 'axios' {
  interface AxiosInstance {
    get<T = any>(url: string, config?: AxiosRequestConfig): Promise<T>
    delete<T = any>(url: string, config?: AxiosRequestConfig): Promise<T>
    head<T = any>(url: string, config?: AxiosRequestConfig): Promise<T>
    options<T = any>(url: string, config?: AxiosRequestConfig): Promise<T>
    post<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T>
    put<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T>
    patch<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T>
  }
}
