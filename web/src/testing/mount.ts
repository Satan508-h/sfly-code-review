/**
 * 挂载助手：把测试环境摆成和线上一样，并且**让 Vue 的警告直接判失败**。
 *
 * ### 为什么要装 Element Plus
 *
 * 不装的话 `el-tooltip` 这类组件解析不了，Vue 会把它们当**未知元素**渲染 ——
 * 插槽里的文字照样进 DOM，于是 `wrapper.text()` 的断言**全部照常通过**，
 * 但组件其实根本没正常工作。实测就是这样：写完 6 条渲染测试全绿，而日志里
 * 躺着一行 `Failed to resolve component: el-tooltip`。
 *
 * ### 为什么警告要判失败
 *
 * 「未知组件」「props 类型不对」「v-model 用错」这些在浏览器里都是**一行警告 +
 * 默默降级**。渲染测试的价值恰恰在于抓住这类问题，而警告只要不判失败，
 * 就一定会被忽略 —— 谁会去读一屏绿色输出中间那几行黄字？
 *
 * 代价是**升级依赖时可能会红**：新版本 Element Plus 多一条弃用警告就会挂。
 * 那时该做的是把那一条显式加进 `IGNORE` 并写清理由，而不是把这道闸拆掉。
 */
import { mount, type ComponentMountingOptions } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import type { App, Component, Plugin } from 'vue'

/** 已知且无害的警告。**每加一条都要写清楚为什么它无害** —— 不写理由的白名单
 *  会自己长大，长大到最后就等于把闸拆了。 */
const IGNORE: RegExp[] = []

/**
 * jsdom 没有实现 `ResizeObserver`，而 Element Plus 的弹出层（tooltip / select）
 * 会去 new 一个。不补这个桩的话，测试会在一个和业务逻辑毫无关系的地方抛
 * `ResizeObserver is not defined` —— 一个会把人引向错误方向的错误。
 */
class NoopResizeObserver implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

function installPolyfills(): void {
  globalThis.ResizeObserver ??= NoopResizeObserver
}

/**
 * 挂载一个组件，环境与线上一致，且**任何未被白名单放行的 Vue 警告都会让调用
 * 直接抛错**。
 *
 * 用插件而不是 `global.config.warnHandler` 来装那个钩子：插件的 `install(app)`
 * 一定在首次渲染之前被调用，而警告是在渲染**过程中**产生的 —— 晚一步装上
 * 就等于漏掉首屏那一批，而首屏恰恰是最容易出问题的地方。
 */
export function mountWithUi<T extends Component>(
  component: T,
  options: ComponentMountingOptions<T> = {} as ComponentMountingOptions<T>,
) {
  installPolyfills()

  const warnings: string[] = []
  const collectWarnings: Plugin = {
    install(app: App) {
      app.config.warnHandler = (msg: string) => {
        if (IGNORE.some((re) => re.test(msg))) return
        warnings.push(msg)
      }
    },
  }

  const { global: globalOptions, ...rest } = options
  const wrapper = mount(component, {
    ...rest,
    global: {
      ...globalOptions,
      plugins: [collectWarnings, ElementPlus, ...(globalOptions?.plugins ?? [])],
    },
  })

  // 挂载是同步的，所以首个渲染周期的警告在这里已经收齐了。抛错而不是返回
  // 一个「有没有警告」的布尔：调用方不需要多写一行断言，忘了写也不会漏掉。
  if (warnings.length > 0) {
    throw new Error(`Vue 警告（共 ${warnings.length} 条）：\n- ${warnings.join('\n- ')}`)
  }
  return wrapper
}
