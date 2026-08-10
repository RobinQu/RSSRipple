import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // react-hooks v6 (compiler-powered) flags the codebase's standard
      // data-loading idiom — `useEffect(() => { load(); }, [deps])` where the
      // async loader calls setLoading(true) synchronously before awaiting —
      // as set-state-in-effect. These are ~20 idiomatic async loaders across
      // pages/modals; restructuring them would not change runtime behavior,
      // so this single rule is disabled instead of rewriting the pattern.
      'react-hooks/set-state-in-effect': 'off',
    },
  },
])
