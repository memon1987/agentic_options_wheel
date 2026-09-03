// FC-096 Phase E PR-2 (§D-3): one window's bars sidecar.
//
// Per (run, symbol, split) — NOT per cell. The bars are the window: every arm
// of it replayed against the same materialisation, and the engine writes the
// sidecar from the BASE arm only. So the same object serves the base cell and
// every tweaked arm beside it, and the cache keyed on the URL gives that
// sharing for free.
//
// A 404 here is the normal answer for every run replayed before PR-1 deployed,
// and the console degrades rather than erroring: the price chart falls back to
// BQ history (captioned, no markers) or shows the absent state, the deployment
// tile switches to its at-cost basis, and the row's `benchmark_return` scalar
// still shows.

import type { SimBars, SweepRow } from '../types/v2';
import { parseBars } from '../components/v2/console/normaliseArtifact';
import { useStoredObject, type ArtifactState } from './useArtifact';

export const barsUrl = (runId: string, symbol: string, split: string): string =>
  `/api/v2/sweeps/${encodeURIComponent(runId)}/bars/` +
  `${encodeURIComponent(symbol)}/${encodeURIComponent(split)}`;

export function useBars(
  sweep: SweepRow | null | undefined,
  symbol: string | null,
  split: string | null,
): ArtifactState<SimBars> {
  return useStoredObject<SimBars>(
    sweep,
    symbol && split ? (runId) => barsUrl(runId, symbol, split) : null,
    parseBars,
  );
}
