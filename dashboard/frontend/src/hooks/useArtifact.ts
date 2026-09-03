// FC-096 Phase E PR-2 (§D-3): one cell's detail artifact.
//
// Thin on purpose. Every decision that matters — when a fetch is allowed, what
// is memoised, what is evicted — lives in `artifactCache.ts` and is shared with
// `useBars`; this hook is the React binding over it and owns exactly one thing
// the cache cannot: cancelling the state write when the cell changes under a
// slow request.

import { useEffect, useState } from 'react';
import type { SimArtifact, SweepRow } from '../types/v2';
import { parseArtifact } from '../components/v2/console/normaliseArtifact';
import {
  failureMessage,
  fetchArtifact,
  isSessionExpired,
  resolveArtifactRun,
  type ArtifactResult,
} from './artifactCache';

/** Which cell. All four segments, exactly as the route spells them. */
export interface CellRef {
  scenario: string;
  symbol: string;
  split: string;
}

export interface ArtifactState<T> {
  data: T | null;
  loading: boolean;
  /** The endpoint's own words for an absent object. Not an error. */
  absent: string | null;
  /** A read that FAILED. Distinct from `absent` — different remedy. */
  error: string | null;
  sessionExpired: boolean;
  /** The run actually read, which differs from the row's id under dedup. */
  runId: string | null;
  /** `true` when `runId` came from `deduplicated_to`. */
  followedDedup: boolean;
}

const IDLE = {
  data: null,
  loading: false,
  absent: null,
  error: null,
  sessionExpired: false,
} as const;

export const artifactUrl = (runId: string, cell: CellRef): string =>
  `/api/v2/sweeps/${encodeURIComponent(runId)}/artifacts/` +
  `${encodeURIComponent(cell.scenario)}/${encodeURIComponent(cell.symbol)}/` +
  `${encodeURIComponent(cell.split)}`;

/**
 * Shared body of `useArtifact` and `useBars`.
 *
 * `sweep` is the ROW, not a status string, because the dedup pointer lives on
 * it and the two must be read together: a `deduplicated` row's status alone
 * would say "do not fetch" and lose the run that has the evidence.
 */
export function useStoredObject<T>(
  sweep: SweepRow | null | undefined,
  urlFor: ((runId: string) => string) | null,
  parse: (raw: unknown) => { value: T | null; reason: string | null },
): ArtifactState<T> {
  const target = resolveArtifactRun(sweep?.run_id, sweep?.status, sweep?.deduplicated_to);
  const url = target && urlFor ? urlFor(target.runId) : null;

  const [state, setState] = useState<Omit<ArtifactState<T>, 'runId' | 'followedDedup'>>({
    ...IDLE,
    loading: !!url,
  });

  useEffect(() => {
    if (!url) {
      setState({ ...IDLE });
      return;
    }
    let live = true;
    setState({ ...IDLE, loading: true });
    // The followed run completed by construction — dedup only ever points at a
    // run that finished — so the cache may memoise its 404s.
    fetchArtifact<T>(
      url,
      'done',
      (raw) => parse(raw).value,
      (raw) => parse(raw).reason ?? 'This object could not be read.',
    )
      .then((result: ArtifactResult<T>) => {
        if (!live) return;
        if (result.kind === 'ok') setState({ ...IDLE, data: result.value });
        else setState({ ...IDLE, absent: result.detail });
      })
      .catch((err: unknown) => {
        if (!live) return;
        if (isSessionExpired(err)) setState({ ...IDLE, sessionExpired: true });
        else setState({ ...IDLE, error: failureMessage(err) });
      });
    return () => {
      live = false;
    };
    // `parse` is a stable module-level function at every call site; depending
    // on it would refetch on every render for no gain.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);

  return { ...state, runId: target?.runId ?? null, followedDedup: target?.followed ?? false };
}

/** One cell's artifact, or the reason there is none. */
export function useArtifact(
  sweep: SweepRow | null | undefined,
  cell: CellRef | null,
): ArtifactState<SimArtifact> {
  return useStoredObject<SimArtifact>(
    sweep,
    cell ? (runId) => artifactUrl(runId, cell) : null,
    parseArtifact,
  );
}
