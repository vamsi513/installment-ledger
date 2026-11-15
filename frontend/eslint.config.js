import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "coverage/**", "node_modules/**"] },
  js.configs.recommended,
  tseslint.configs.recommendedTypeChecked,
  // In eslint-plugin-react-hooks 7 the top level `configs.recommended*` entries
  // are still the eslintrc shape; the flat config lives under `configs.flat`.
  reactHooks.configs.flat["recommended-latest"],
  {
    languageOptions: {
      globals: { ...globals.browser },
      parserOptions: {
        projectService: {
          // This file is not in tsconfig.json's `include`, so the project
          // service needs to be told it may type it on its own.
          allowDefaultProject: ["eslint.config.js"],
        },
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      // Amounts are integer cents. Float arithmetic on money is the bug this
      // project exists to avoid, so the rules that catch sloppy numeric and
      // nullish handling are errors rather than warnings.
      "@typescript-eslint/no-unnecessary-condition": "error",
      "@typescript-eslint/strict-boolean-expressions": [
        "error",
        { allowNumber: false, allowString: false, allowNullableObject: true },
      ],
      eqeqeq: ["error", "always"],
      "no-implicit-coercion": "error",
    },
  },
  {
    files: ["**/*.test.ts", "**/*.test.tsx", "src/test/**"],
    rules: {
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/unbound-method": "off",
    },
  },
  {
    files: ["vite.config.ts", "eslint.config.js"],
    languageOptions: { globals: { ...globals.node } },
    rules: { "@typescript-eslint/no-unsafe-assignment": "off" },
  },
);
