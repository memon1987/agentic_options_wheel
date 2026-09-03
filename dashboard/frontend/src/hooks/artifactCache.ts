// FC-096 Phase E PR-2 (§D-3): the module-level artifact/bars cache.
//
// A cell's artifact is an IMMUTABLE object in GCS: the engine writes it once as
// it replays and never rewrites it, so re-fetching it on every mount would buy
// nothing but latency and GCS reads. The console mounts and unmounts these on
// every cell change, every split flip and every Back, which is why the cache
// is module-level rather than component-level.
//
// Two rules earn their own paragraphs, because both encode a distinction that
// looks like a detail and is not:
//
//   1. **A 404 is memoised only when the run's status is `done`.** 404 is the
//      NORMAL answer for an errored cell, for a run replayed before artifacts
//      existed, and for a CLI run without `--persist` — memoising it saves a
//      pointless round trip per mount. But on a run that is still `running` the
//      same 404 means "not written YET", and caching that would leave a
//      permanently empty panel on a run that finished thirty seconds later,
//      recoverable only by a reload. `done` is the only status under which a
//      cell is fetched at all, so in practice this is belt and braces — and it
//      is exactly the kind of belt that stops a future caller re-introducing
//      the bug.
//   2. **A rejected promise is evicted immediately.** A 502 (missing bucket
//      grant), a network drop or an expired session are all transient in the
//      sense that matters: the next mount must try again. Leaving the rejected
//      promise in the map would turn one bad minute into a dead panel for the
//      life of the tab.
//
// The map holds PROMISES, not values, so two components mounting in the same
// tick share one in-flight request rather than racing two.

import { getJson, HttpError } from './useSweeps';
import { isSessionExpired } from './iapSession';

/** 48 entries ~ four full 12-symbol runs' worth of cells. Bounded, not tuned. */
export const ARTIFACT_CACHE_MAX = 48;

/**
 * A settled fetch: the object, or the endpoint's own words for why there is
 * none.
 *
 * `absent` is a RESULT, not an error. The console renders the `detail`
 * verbatim — `routers/v2.py` writes it for an operator ("Cells that errored
 * have none, and neither do runs replayed before artifacts existed…") and any
 * paraphrase here would drop the half that says what to do next.
 */
export type ArtifactResult<T> =
  | { kind: 'ok'; value: T }
  | { kind: 'absent'; detail: string };

/**
 * The one status whose 404 is a DURABLE answer, and so the only one under which
 * a 404 may be memoised (§D-3, review B5). Never inferred here: the caller
 * passes the status it resolved (`resolveArtifactRun`), because inferring it is
 * how a cache ends up memoising a 404 against a run that had not finished.
 */
export const FETCHABLE_STATUS = 'done';

const cache = new Map<string, Promise<ArtifactResult<unknown>>>();

/** Cheapest possible LRU: `Map` iterates in insertion order. */
function remember(url: string, promise: Promise<ArtifactResult<unknown>>): void {
  cache.delete(url);
  cache.set(url, promise);
  while (cache.size > ARTIFACT_CACHE_MAX) {
    const oldest = cache.keys().next();
    if (oldest.done) break;
    cache.delete(oldest.value);
  }
}

/** Mirrors `resetSessionExpiredSignal`. Module state needs a test seam. */
export function resetArtifactCacheForTests(): void {
  cache.clear();
}

/** Entry count. Test-only observability — never branched on by the console. */
export const artifactCacheSize = (): number => cache.size;

/**
 * Fetch `url` once per tab, or return the in-flight/settled promise.
 *
 * `status` is the RUN's status, passed down by the caller from the sweep row
 * (`useSweepDetail`) — this module never infers it, because inferring it is how
 * a cache ends up memoising a 404 against a run that had not finished writing.
 */
export function fetchArtifact<T>(
  url: string,
  status: string,
  parse: (raw: unknown) => T | null,
  parseFailureDetail: (raw: unknown) => string,
): Promise<ArtifactResult<T>> {
  const hit = cache.get(url);
  if (hit) return hit as Promise<ArtifactResult<T>>;

  const promise = (async (): Promise<ArtifactResult<T>> => {
    const controller = new AbortController();
    let raw: unknown;
    try {
      raw = await getJson<unknown>(url, controller.signal);
    } catch (err) {
      if (err instanceof HttpError && err.status === 404) {
        if (status !== FETCHABLE_STATUS) {
          // Rule 1: not a durable answer on an unfinished run. Evict so the
          // next mount asks again, and report it as absence for THIS render.
          cache.delete(url);
        }
        return { kind: 'absent', detail: err.detail };
      }
      // Rule 2: everything else — 502, 503, 400, a network failure, an expired
      // session — is retried on the next mount.
      cache.delete(url);
      throw err;
    }
    const value = parse(raw);
    if (value === null) {
      // A schema this build cannot read is a DURABLE answer about a stored
      // object, so it stays cached: re-fetching would produce the same bytes
      // and the same refusal. The reason reaches the screen as absence.
      return { kind: 'absent', detail: parseFailureDetail(raw) };
    }
    return { kind: 'ok', value };
  })();

  remember(url, promise as Promise<ArtifactResult<unknown>>);
  // A rejection that nobody has awaited yet must not become an unhandled
  // rejection just because the mount that started it unmounted first.
  promise.catch(() => undefined);
  return promise;
}

/**
 * The message shown for a read that FAILED (as opposed to one that was
 * absent). `isSessionExpired` is re-exported through here so the hooks do not
 * each import the classifier separately.
 */
export const failureMessage = (err: unknown): string =>
  err instanceof Error ? err.message : String(err);

export { isSessionExpired };

/**
 * WHICH run's objects a cell of this run should be read from, and whether it
 * may be read at all (§D-3, review item B5).
 *
 * `deduplicated` is the interesting case. Such a row has a `run_id` of its own
 * and NO objects under it: the whole point of dedup is that nothing was
 * replayed, so the evidence lives under `deduplicated_to`. Fetching under the
 * row's own id would 404 on every cell of a run that has a complete artifact
 * set one pointer away. So the hooks follow the pointer, and the console says
 * "answered by run X".
 *
 * `submitted` / `running` / `failed` return `null`: there is nothing stored
 * yet, or there never will be, and a fetch would only manufacture 404s.
 */
export function resolveArtifactRun(
  runId: string | null | undefined,
  status: string | null | undefined,
  deduplicatedTo: string | null | undefined,
): { runId: string; followed: boolean; status: string } | null {
  if (!runId) return null;
  if (status === 'done') return { runId, followed: false, status };
  if (status === 'deduplicated' && deduplicatedTo) {
    // The ROW's status travels with the target, not a `'done'` this module
    // invented (review round 1, F6). All this row proves is that a pointer
    // exists; whether the run it points at finished writing is a fact about
    // THAT row, which nobody here has read. So a 404 under a followed pointer
    // is reported and retried rather than memoised -- one extra request per
    // mount on a dedup'd run's missing cells, against never showing a cell
    // that appeared a moment later.
    return { runId: deduplicatedTo, followed: true, status };
  }
  return null;
}
