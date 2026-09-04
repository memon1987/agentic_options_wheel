// FC-096 Phase E PR-3: the equal-weight portfolio index.
//
// The single most misreadable panel in the console, so it is built to refuse
// the misreading:
//
//   * MEASURED symbols only. An `insufficient` cell is a flat line at its own
//     starting cash; averaged in, it reads as stability rather than as absence.
//   * Everything excluded is NAMED, with its state — the reader can see the
//     constituency instead of inferring it from a count.
//   * **No aggregate number anywhere.** No portfolio return, no index level in
//     text, no "up X%". These are N independent single-symbol replays, each on
//     its OWN full capital; summing them claims a diversification the engine
//     never modelled, and one number is all it takes for that claim to travel.
//   * "k of N loaded" while the fetches land, so a half-drawn index is legible
//     as half-drawn.
//
// Fetching is capped at four in flight. Every request goes through the shared
// module-level cache, so switching tabs and coming back costs nothing, and the
// base overlay's second N is only paid when the operator asks for it.

import { useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { SimArtifact, SweepReport, SweepRow } from '../../../types/v2';
import { failureMessage, fetchArtifact, resolveArtifactRun } from '../../../hooks/artifactCache';
import { artifactUrl, type CellRef } from '../../../hooks/useArtifact';
import { fmtDateShort } from '../../../utils/format';
import { indexRows, lookupCell, renderCell } from '../sims/resultCells';
import { parseArtifact } from './normaliseArtifact';
import { overlayConstituency, portfolioIndex, type PortfolioCell } from './series';
import StrategyBanner from './StrategyBanner';

export const PORTFOLIO_CONCURRENCY = 4;

const cellKey = (cell: CellRef): string => `${cell.scenario}/${cell.symbol}/${cell.split}`;

interface LoadedCell {
  artifact: SimArtifact | null;
  absence: string | null;
}

/**
 * Fetch a SET of cell artifacts, at most four at a time, reporting each as it
 * lands so the index draws progressively.
 */
function useArtifactSet(
  sweep: SweepRow | null,
  cells: CellRef[],
): Record<string, LoadedCell> {
  const [loaded, setLoaded] = useState<Record<string, LoadedCell>>({});
  // The cell LIST is the dependency, not the array identity: a parent that
  // rebuilds the array every render must not restart every fetch.
  const signature = cells.map(cellKey).join('|');

  useEffect(() => {
    const target = resolveArtifactRun(sweep?.run_id, sweep?.status, sweep?.deduplicated_to);
    setLoaded({});
    if (!target || cells.length === 0) return;
    let live = true;
    const queue = [...cells];
    const worker = async (): Promise<void> => {
      for (;;) {
        const cell = queue.shift();
        if (!cell || !live) return;
        const url = artifactUrl(target.runId, cell);
        let result: LoadedCell;
        try {
          const settled = await fetchArtifact<SimArtifact>(
            url,
            target.status,
            (raw) => parseArtifact(raw).value,
            (raw) => parseArtifact(raw).reason ?? 'This object could not be read.',
          );
          result =
            settled.kind === 'ok'
              ? { artifact: settled.value, absence: null }
              : { artifact: null, absence: settled.detail };
        } catch (err) {
          result = { artifact: null, absence: failureMessage(err) };
        }
        if (!live) return;
        setLoaded((prev) => ({ ...prev, [cellKey(cell)]: result }));
      }
    };
    void Promise.all(
      Array.from({ length: PORTFOLIO_CONCURRENCY }, () => worker()),
    ).catch(() => undefined);
    return () => {
      live = false;
    };
    // `cells` is rebuilt on every render by the caller; `signature` is its value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sweep, signature]);

  return loaded;
}

export interface PortfolioEquityViewProps {
  sweep: SweepRow;
  report: SweepReport;
  scenario: string;
  split: string;
  specStrategy: string | null;
}

export default function PortfolioEquityView({
  sweep,
  report,
  scenario,
  split,
  specStrategy,
}: PortfolioEquityViewProps) {
  const [withBase, setWithBase] = useState(false);
  const isBase = scenario === 'base';
  const index = useMemo(() => indexRows(report.rows), [report.rows]);

  /** The cell-state partition decides membership — `renderCell`, not a re-read. */
  const measuredFor = (arm: string): string[] =>
    report.symbols.filter(
      (symbol) => renderCell(lookupCell(index, arm, symbol, split)).kind === 'return',
    );
  const armSymbols = useMemo(
    () => measuredFor(scenario),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [index, report.symbols, scenario, split],
  );

  /**
   * The symbols BASE measured in this window (review round 1, R2).
   *
   * The overlay used to be built over the ARM's measured set, which quietly
   * averaged base cells base never measured: an `insufficient` base cell is a
   * flat line at its own starting cash, so it pulls the base index toward 100
   * and the arm reads as beating a benchmark that was never computed. The two
   * indices must be over the SAME constituency or there is no overlay.
   */
  const baseSymbols = useMemo(
    () => (isBase ? [] : measuredFor('base')),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [index, report.symbols, split, isBase],
  );

  const cells = useMemo(() => {
    const wanted: CellRef[] = armSymbols.map((symbol) => ({ scenario, symbol, split }));
    if (withBase && !isBase) {
      // Only base cells base MEASURED are fetched: an unmeasured one cannot
      // enter the index, so requesting it is a round trip for a 404 or for an
      // artifact nothing may read.
      for (const symbol of armSymbols) {
        if (baseSymbols.includes(symbol)) wanted.push({ scenario: 'base', symbol, split });
      }
    }
    return wanted;
  }, [armSymbols, baseSymbols, scenario, split, withBase, isBase]);

  const loaded = useArtifactSet(sweep, cells);

  const armCells: Record<string, PortfolioCell> = {};
  for (const symbol of armSymbols) {
    const entry = loaded[cellKey({ scenario, symbol, split })];
    if (entry) armCells[symbol] = { ...entry, specStrategy };
  }
  const arm = portfolioIndex(armCells, armSymbols);

  // The overlay's target constituency: the arm's index members that base ALSO
  // measured. Anything the arm measured and base did not is named below rather
  // than averaged in at its starting cash.
  const { target: overlayTarget, missing: missingFromBase } = overlayConstituency(
    arm.included,
    baseSymbols,
  );

  const baseCells: Record<string, PortfolioCell> = {};
  for (const symbol of overlayTarget) {
    const entry = loaded[cellKey({ scenario: 'base', symbol, split })];
    if (entry) baseCells[symbol] = { ...entry, specStrategy };
  }
  const baseIndex = withBase && !isBase ? portfolioIndex(baseCells, overlayTarget) : null;
  // Drawn ONLY when base measured every symbol in the arm's index and every one
  // of those artifacts is in hand. Two lines over different constituencies are
  // not comparable, and a chart is the last place that difference is noticed.
  const baseComparable =
    !!baseIndex &&
    missingFromBase.length === 0 &&
    baseIndex.included.length === arm.included.length &&
    arm.included.length > 0;

  const rows = useMemo(() => {
    const baseBy = new Map((baseComparable ? baseIndex!.rows : []).map((r) => [r.date, r.index]));
    return arm.rows.map((r) => ({ date: r.date, arm: r.index, base: baseBy.get(r.date) ?? null }));
  }, [arm.rows, baseIndex, baseComparable]);

  const excluded = useMemo(() => {
    const named = [...arm.excluded];
    for (const symbol of report.symbols) {
      if (armSymbols.includes(symbol)) continue;
      named.push({ symbol, reason: renderCell(lookupCell(index, scenario, symbol, split)).title });
    }
    return named.sort((a, b) => a.symbol.localeCompare(b.symbol));
  }, [arm.excluded, report.symbols, armSymbols, index, scenario, split]);

  return (
    <section
      data-testid="portfolio-equity-view"
      className="rounded-lg border border-gray-700 bg-gray-800 p-5"
    >
      <div className="flex items-start justify-between gap-4">
        <h3 className="text-base font-semibold text-white">
          Portfolio — equal-weight index, {scenario} / {split}
        </h3>
        {!isBase && (
          <label className="text-xs text-gray-400 flex items-center gap-2">
            <input
              type="checkbox"
              data-testid="portfolio-base-toggle"
              checked={withBase}
              onChange={(e) => setWithBase(e.target.checked)}
            />
            Overlay base ({arm.total} more fetches)
          </label>
        )}
      </div>

      <StrategyBanner strategy={specStrategy} />

      <p data-testid="portfolio-caption" className="text-xs text-gray-500 mt-1 mb-3">
        Equal-weight index of {arm.included.length} independent single-symbol replays, each on its
        own capital — not a portfolio simulation. 100 = capital base: the mean of each measured
        symbol&rsquo;s equity ÷ its own capital base, never rebased to its first day.{' '}
        <span data-testid="portfolio-loaded">
          {arm.loaded} of {arm.total} loaded
        </span>
        .
        {arm.droppedDates > 0 && (
          <> {arm.droppedDates} dates are omitted where a member had no state.</>
        )}
      </p>

      {arm.total === 0 ? (
        <p data-testid="portfolio-empty" className="text-sm text-gray-400">
          No symbol in this arm and window is measured, so there is no index to build.
        </p>
      ) : rows.length === 0 ? (
        <p data-testid="portfolio-loading" className="text-sm text-gray-400">
          Loading {arm.total} symbol artifacts…
        </p>
      ) : (
        <div className="h-64">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
              <XAxis
                dataKey="date"
                tick={{ fill: '#9ca3af', fontSize: 11 }}
                tickFormatter={fmtDateShort}
                minTickGap={28}
              />
              <YAxis tick={{ fill: '#9ca3af', fontSize: 11 }} domain={['auto', 'auto']} />
              <Tooltip
                contentStyle={{
                  background: '#1f2937',
                  border: '1px solid #374151',
                  color: '#f3f4f6',
                }}
                labelFormatter={(label) => fmtDateShort(label as string)}
                formatter={(value: number, name: string) => [value.toFixed(1), name]}
              />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              <Line
                type="monotone"
                dataKey="arm"
                name={`${scenario} (${arm.included.length} symbols)`}
                stroke="#60a5fa"
                strokeWidth={2}
                dot={false}
                connectNulls
              />
              {baseComparable && (
                <Line
                  type="monotone"
                  dataKey="base"
                  name={`base (same ${arm.included.length} symbols)`}
                  stroke="#a78bfa"
                  strokeWidth={1.5}
                  strokeDasharray="5 3"
                  dot={false}
                  connectNulls
                />
              )}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      <p data-testid="portfolio-members" className="text-xs text-gray-500 mt-2">
        In the index: {arm.included.length ? arm.included.join(', ') : 'none yet'}.
      </p>
      {excluded.length > 0 && (
        <ul data-testid="portfolio-excluded" className="text-xs text-gray-500 mt-1 space-y-0.5">
          {excluded.map((entry) => (
            <li key={entry.symbol}>
              <span className="font-mono text-gray-400">{entry.symbol}</span> excluded —{' '}
              {entry.reason}
            </li>
          ))}
        </ul>
      )}
      {withBase && !isBase && !baseComparable && (
        <p data-testid="portfolio-base-omitted" className="text-xs text-amber-400 mt-1">
          Base overlay omitted:{' '}
          {missingFromBase.length > 0 ? (
            <>
              base did not measure {missingFromBase.join(', ')} in this window (
              {missingFromBase
                .map(
                  (symbol) =>
                    `${symbol}: ${renderCell(lookupCell(index, 'base', symbol, split)).text}`,
                )
                .join(', ')}
              ). Averaging an unmeasured base cell in would put its flat starting-cash line into
              the benchmark and make this arm look like it beat one.
            </>
          ) : baseIndex && baseIndex.excluded.length > 0 ? (
            <>
              {/* A base cell that 404s is PERMANENT (§D-5), not "not loaded
                  yet": the confirmer showed guard 3 is what stops a 2-symbol
                  base line being drawn against a 3-symbol arm, and the notice
                  beside it has to say which symbol and why. */}
              base&rsquo;s cell for{' '}
              {baseIndex.excluded.map((entry) => entry.symbol).join(', ')} could not be read —{' '}
              {baseIndex.excluded.map((entry) => `${entry.symbol}: ${entry.reason}`).join('; ')}. An
              index over fewer symbols than the arm&rsquo;s is not the same benchmark, so it is not
              drawn.
            </>
          ) : (
            <>base&rsquo;s artifacts for these symbols are not all loaded yet.</>
          )}
        </p>
      )}
    </section>
  );
}
