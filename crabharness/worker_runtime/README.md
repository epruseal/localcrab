# LocalCrab Worker Runtime

This directory pins the Node runtime used by CrabHarness workers.

Install dependencies from `crabharness/worker_runtime`:

```bash
npm install
npx playwright install chromium
```

The SOEAK worker command registered in `codex_workers/soeak/worker.manifest.json` resolves to:

```bash
npm --prefix ./worker_runtime run worker:soeak -- [worker args]
```

CrabHarness runs worker commands with `crabharness/` as cwd, so the relative
`./worker_runtime` path is stable.
