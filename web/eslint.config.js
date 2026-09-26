// ESLint flat config（ESLint 9 的格式）。
//
// ### 为什么是 `flat/essential` 而不是 `flat/recommended`
//
// `flat/recommended` 里塞了几十条**格式**规则（属性换行、自闭合标签、
// 单行元素的内容要不要换行……）。这个项目没打算让 lint 管格式 ——
// 格式交给 Prettier，lint 只管**对错**。两件事分开的直接好处是：
// lint 报出来的每一条都是「这里可能错了」，而不是「这里和我不一样」。
// 混在一起的下场是所有人学会了一眼扫过就算 —— 而那正是 lint 失效的方式。
//
// 和 Python 那边的分工是一致的：`ruff check`（对错）+ `ruff format`（格式）。
//
// 这个文件本身是 JS 而不是 TS：ESLint 要在加载 TypeScript 之前先读它，
// 让配置文件本身需要编译会多出一层先有鸡还是先有蛋。
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    // 构建产物和缓存不是源码。**`dist/` 必须排除** —— 它是压缩过的 JS，
    // 让 lint 去读它只会报出一堆看不懂的错，而真正的源码错误被淹掉。
    ignores: ['dist/**', 'node_modules/**', '*.tsbuildinfo'],
  },

  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/essential'],

  {
    // `.vue` 文件里的 `<script lang="ts">` 要交给 TypeScript 解析器 ——
    // `vue-eslint-parser` 负责拆出模板和脚本，脚本部分再转交。
    // 不配这一段的话，`lang="ts"` 的代码会被当成普通 JS 解析，
    // 报出「Unexpected token」这种指错方向的错。
    files: ['**/*.vue'],
    languageOptions: { parserOptions: { parser: tseslint.parser } },
  },

  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      globals: { ...globals.browser },
    },
    rules: {
      // `App.vue` 是 Vue 的约定名，这条规则对它没有意义。
      // （规则本身的用意是防止组件名和 HTML 标签冲突，而 App 不是标签。）
      'vue/multi-word-component-names': 'off',

      // 未使用的变量：允许 `_` 开头的占位（比如解构里刻意丢掉的字段）。
      // 项目里的 `const { global: globalOptions, ...rest } = options` 就是这个形状。
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],

      // `console` 在浏览器控制台里是调试工具，在这个项目里没有留着它的场景：
      // 日志一律走 structlog（后端），前端该显示的东西就显示在页面上。
      // 报错而不是警告：警告会被忽略。
      'no-console': 'error',
    },
  },

  // 配置文件自己跑在 Node 里
  {
    files: ['*.config.{js,ts}', 'vite.config.ts'],
    languageOptions: { globals: { ...globals.node } },
  },
)
