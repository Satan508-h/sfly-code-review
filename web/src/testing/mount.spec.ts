/**
 * 「警告闸」本身的测试。
 *
 * 这道闸的作用是让「未知组件 / props 写错 / v-model 用反」这类**在浏览器里
 * 只留一行警告、然后默默降级**的问题变成测试失败。而它自己一旦失效，
 * 表现是**所有渲染测试照样全绿** —— 一个不会被发现的失效。
 *
 * 所以它必须自己有一条测试。这条测试红掉的时候，别改它，去修 `mount.ts`。
 */
import { defineComponent, h, resolveComponent } from 'vue'
import { describe, expect, it } from 'vitest'

import { mountWithUi } from './mount'

describe('mountWithUi 的警告闸', () => {
  it('引用了不存在的组件时，挂载必须失败', () => {
    // 用渲染函数而不是内联 `template` 字符串：后者的行为取决于测试环境
    // 解析到的是不是带编译器的 Vue 构建 —— 那样这条测试就变成了在测
    // 「运行时的 Vue 是哪个构建」，而不是在测这道闸。
    const Broken = defineComponent({
      render: () => h(resolveComponent('el-this-does-not-exist')),
    })

    expect(() => mountWithUi(Broken)).toThrow(/Vue 警告/)
  })

  it('正常的组件不会误报', () => {
    const Fine = defineComponent({
      render: () => h(resolveComponent('el-tag'), null, () => '正常'),
    })

    expect(mountWithUi(Fine).text()).toContain('正常')
  })
})
