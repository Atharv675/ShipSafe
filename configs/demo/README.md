# Demo overlay (`make demo` only)

With `SENTINEL_CONFIG_OVERLAY=demo`, each file here is deep-merged over the file of the same
name in `configs/` (nested keys merge, lists are replaced). Only what the demo changes is
listed. Nothing here applies to the real Win64/Win32 stack.

- PDF instead of Win64/Win32: its cleaned splits are small (52,000 train / 12,000 test files)
  and already local, so the demo needs no download and no Kaggle run.
- Fewer LightGBM trees: the demo must finish in under 10 minutes on a laptop.
- Gate operating point 1% FPR: a PDF test week has ~500 benign files, too few to measure
  0.1% (Stage 0, DATASET_REPORT.md). The real policy stays at 0.1%.
