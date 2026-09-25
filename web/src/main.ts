import ElementPlus from 'element-plus'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import './styles/main.css'

const app = createApp(App)

app.use(createPinia())
// 注意这里**不传 locale**，而且这是刻意的。
//
// `app.use(ElementPlus, { locale: zhCn })` 在 vue-tsc 严格模式下会报 TS2769：
// ConfigProviderProps 把 locale 声明成了 PropType 包装对象而不是值本身。
// 绕过去需要一次类型断言，而断言会把真实的类型错误一起掩盖掉 —— 不划算。
//
// 官方的正确做法是在模板里用 <el-config-provider :locale="zhCn"> 包裹根节点。
// 当前这个页面用的都是卡片/标签/骨架屏，没有依赖 locale 的文案；
// M8 重写成真正的仪表盘（会有 el-table 空状态、el-pagination、日期选择器）
// 时，把 provider 加在 App.vue 的根节点上。
app.use(ElementPlus)

app.mount('#app')
