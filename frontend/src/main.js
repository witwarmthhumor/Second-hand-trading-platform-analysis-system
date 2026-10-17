import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import './styles/global.css'
// 中文语言包（随 element-plus 一起提供，不是新依赖）：
// 不配置时组件内置文案是英文（确认框按钮显示 Cancel/OK、分页显示 Total 等）
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import App from './App.vue'

createApp(App).use(ElementPlus, { locale: zhCn }).mount('#app')
