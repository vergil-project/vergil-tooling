// Vergil shareable ESLint flat config for TypeScript (epic
// vergil-project/.github#284, T4). Type-aware linting via typescript-eslint's
// recommendedTypeChecked, plus the no-standing-suppression rule
// (@typescript-eslint/ban-ts-comment) that bans bare `@ts-ignore`/`@ts-nocheck`
// and requires a description on `@ts-expect-error` (spec §3.2).
//
// Referenced by the registry LINT command as `eslint . --config
// {configs}/typescript/eslint.config.mjs`; the packages it imports
// (@eslint/js, typescript-eslint) ship in the `prod-ts-node:*` images (T1).
// `projectService: true` auto-discovers the consumer's nearest tsconfig, so the
// type-aware rules resolve type information without a hard-coded project path.

import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  {
    ignores: ["dist/", "coverage/", "node_modules/"],
  },
  {
    files: ["**/*.ts", "**/*.tsx", "**/*.mts", "**/*.cts"],
    extends: [js.configs.recommended, ...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parserOptions: {
        projectService: true,
      },
    },
    rules: {
      "@typescript-eslint/ban-ts-comment": [
        "error",
        {
          "ts-expect-error": "allow-with-description",
          "ts-ignore": true,
          "ts-nocheck": true,
          "ts-check": false,
        },
      ],
    },
  },
);
