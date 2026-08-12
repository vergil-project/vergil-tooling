// Vergil shareable ESLint flat config for TypeScript (epic
// vergil-project/.github#284, T4). Type-aware linting via typescript-eslint's
// recommendedTypeChecked, plus the no-standing-suppression rule
// (@typescript-eslint/ban-ts-comment) that bans bare `@ts-ignore`/`@ts-nocheck`
// and requires a description on `@ts-expect-error` (spec §3.2).
//
// The registry LINT command stages this file into the consumer repo root at
// lint time and runs `eslint . --config ./.vergil-eslint.config.mjs` there
// (see the LINT wiring in languages.py, #2771). Staging is required because
// this is an ESM config: Node resolves its bare imports (@eslint/js,
// typescript-eslint) relative to *this file's own directory*, so it must live
// inside the repo tree for those imports to resolve against the consumer's
// repo-local node_modules (populated by `npm ci`) — the packaged path inside
// the vergil-tooling Python package has no adjacent node_modules.
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
