import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './styles/main.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
// 这里**不传 locale**，中文文案由 App.vue 根节点的 `<el-config-provider>` 提供。
//
// `app.use(ElementPlus, { locale: zhCn })` 在 vue-tsc 严格模式下会报 TS2769：
// ConfigProviderProps 把 locale 声明成了 PropType 包装对象而不是值本身，
// 绕过去需要一次类型断言，而断言会把真实的类型错误一起掩盖掉。
app.use(ElementPlus)

app.mount('#app')
