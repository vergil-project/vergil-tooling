// Vergil shareable ESLint flat config for TypeScript (epic
// vergil-project/.github#284, T4). Type-aware linting via typescript-eslint's
// recommendedTypeChecked, plus the no-standing-suppression rule
// (@typescript-eslint/ban-ts-comment) that bans bare `@ts-ignore`/`@ts-nocheck`
// and requires a description on `@ts-expect-error` (spec §3.2).
//
// This is Vergil's base, a managed file the fleet sync writes into every
// TypeScript repo's root (epic vergil-project/.github#1174). Do not edit a
// repo's copy: the sync rewrites it. The default export is a flat-config
// array, so the repo's own `eslint.config.mjs` imports it and spreads it,
// adding ignores and rules of its own. Its bare imports (@eslint/js,
// typescript-eslint) resolve from the repo's node_modules, which hold the
// declared plugins. `projectService: true` auto-discovers the repo's nearest
// tsconfig, so the type-aware rules resolve type information without a
// hard-coded project path.

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
