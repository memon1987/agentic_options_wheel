// FC-096 Phase E PR-3, component 2: the price chart with trade-event markers.
//
// Two modes, and the chart says which one it is in every time:
//
//   1. **The replay's own bars.** The sidecar PR-1 writes carries the closes
//      the engine actually decided against, so the markers sit on the prices
//      that produced them. Roll days get a reference line.
//   2. **The BQ fallback.** Every run replayed before PR-1 deployed has no
//      sidecar. For a LIVE symbol the dashboard's own stock history can draw the
//      shape of the window — but it is a different series (unadjusted, ingested
//      daily, no settlement clamp), so it carries NO markers and says what it
//      is. A candidate symbol has no rows at all and gets the absent state.
//
// Markers never move to mode 2. A marker is a claim that a decision was made at
// that price, and on a series the replay never saw it is a claim we cannot make.

import { useMemo } from 'react';
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { SimArtifact, SimBar, SimBars } from '../../../types/v2';
import { useApi } from '../../../hooks/useApi';
import { fmtCurrency, fmtDateShort, parseOcc } from '../../../utils/format';
import { chartRows, clipBars, type ChartRow, type PlacedEvent } from './chartRows';

export interface PriceChartProps {
  symbol: string;
  bars: SimBars | null;
  /** The bars route's own words when there is no sidecar. */
  barsAbsence: string | null;
  artifact: SimArtifact | null;
  artifactAbsence: string | null;
  /** The run's window for this split — the fallback's clip. */
  window: { start: string; end: string } | null;
  strategy: string;
}

/**
 * One BigQuery history row as a bar, or `null` when it cannot be one.
 *
 * The BQ route is a DIFFERENT shape from the sidecar — every numeric column is
 * nullable and a failed query answers `[]` — so the payload is coerced and
 * filtered rather than trusted. An unusable row is dropped and an unusable
 * payload renders the absent state; a chart of NaN closes is worse than no
 * chart, because it looks like a chart.
 */
export function asBar(raw: unknown): SimBar | null {
  if (!raw || typeof raw !== 'object') return null;
  const row = raw as Record<string, unknown>;
  const date = typeof row.date === 'string' ? row.date.slice(0, 10) : null;
  const close = Number(row.close);
  if (!date || !Number.isFinite(close)) return null;
  const num = (value: unknown): number => (Number.isFinite(Number(value)) ? Number(value) : close);
  return {
    date,
    open: num(row.open),
    high: num(row.high),
    low: num(row.low),
    close,
    volume: Number.isFinite(Number(row.volume)) ? Number(row.volume) : 0,
  };
}

/** Whole days from `start` to today, the BQ route's only window control. */
export function fallbackDays(start: string | null, now: Date = new Date()): number {
  if (!start) return 365;
  const from = Date.parse(`${start}T00:00:00Z`);
  if (Number.isNaN(from)) return 365;
  const days = Math.round((now.getTime() - from) / 86_400_000) + 1;
  return Math.min(3650, Math.max(1, days));
}

/** One session's events, as the tooltip lists them. */
function EventLines({ events }: { events: PlacedEvent[] }) {
  return (
    <>
      {events.map(({ event, offSession }, i) => {
        const occ = parseOcc(event.symbol);
        const contract = occ.optionType
          ? `${occ.optionType === 'P' ? 'put' : 'call'} ${occ.strike?.toFixed(2)} exp ${occ.expiration}`
          : event.symbol;
        return (
          <div key={i} className="mt-1">
            <span className="text-gray-200">{event.kind}</span>{' '}
            <span className="text-gray-400">{contract}</span>
            <div className="text-gray-400">
              {event.contracts ? `${event.contracts}x · ` : ''}
              {event.price === null ? 'no price' : fmtCurrency(event.price)} · cash{' '}
              {fmtCurrency(event.cash_delta)}
              {offSession && (
                <span className="text-amber-400"> · dated {event.date} (not a session)</span>
              )}
            </div>
          </div>
        );
      })}
    </>
  );
}

function Shell({ children, caption }: { children: React.ReactNode; caption?: React.ReactNode }) {
  return (
    <section data-testid="price-chart" className="rounded-lg border border-gray-700 bg-gray-800 p-5">
      {children}
      {caption}
    </section>
  );
}

export default function PriceChart({
  symbol,
  bars,
  barsAbsence,
  artifact,
  artifactAbsence,
  window: windowRange,
  strategy,
}: PriceChartProps) {
  const hasSidecar = !!bars && bars.bars.length > 0;
  // Only when there is no sidecar, and only for the window's own span. `useApi`
  // treats a null URL as "do not fetch", so the live path costs no request.
  const fallbackUrl = hasSidecar
    ? null
    : `/api/v2/symbol/${encodeURIComponent(symbol)}/stock-history?days=${fallbackDays(
        windowRange?.start ?? null,
      )}`;
  const { data: bqBars, loading: bqLoading } = useApi<SimBar[]>(fallbackUrl);

  const built = useMemo(() => {
    if (hasSidecar) {
      return {
        mode: 'replay' as const,
        ...chartRows(bars!.bars, artifact?.ledger ?? [], artifact?.roll_records ?? []),
      };
    }
    // The BQ route is a different shape from the sidecar: `date` arrives as a
    // string but the numbers can be nulls, and a failed query answers `[]`. It
    // is coerced and filtered here rather than trusted — an unusable payload
    // renders the absent state, never a chart with NaN closes.
    const clipped = clipBars(
      Array.isArray(bqBars) ? bqBars.map(asBar).filter((bar): bar is SimBar => bar !== null) : [],
      windowRange?.start ?? null,
      windowRange?.end ?? null,
    );
    return { mode: 'fallback' as const, ...chartRows(clipped, [], []) };
  }, [hasSidecar, bars, artifact, bqBars, windowRange]);

  const { rows, markers, rollLines, offSessionEvents, droppedEvents, mode } = built;

  if (rows.length === 0) {
    return (
      <Shell>
        <h3 className="text-base font-semibold text-white">{symbol} — price and trade events</h3>
        <p data-testid="price-chart-absent" className="text-sm text-gray-400 mt-2">
          {bqLoading
            ? 'Loading price history…'
            : (barsAbsence ??
              'No price series is stored for this run and the dashboard has no history for this symbol.')}
        </p>
        <p className="text-xs text-gray-500 mt-2">
          Runs replayed before the bars sidecar shipped store no price series. Re-submitting the
          same spec replays it and writes one; a candidate symbol has no dashboard history to fall
          back to either.
        </p>
      </Shell>
    );
  }

  return (
    <Shell>
      <h3 className="text-base font-semibold text-white">{symbol} — price and trade events</h3>
      <p className="text-xs text-gray-500 mt-1 mb-3">
        {mode === 'replay' ? (
          <span data-testid="price-caption-replay">
            {rows.length} sessions the replay decided against, from this run&rsquo;s bars sidecar.{' '}
            {built.placedEvents} ledger events, {rollLines.length} roll
            {rollLines.length === 1 ? '' : 's'}.
          </span>
        ) : (
          <span data-testid="price-caption-fallback" className="text-amber-400">
            Dashboard stock history, not the replay&rsquo;s bars — this run stored no price series,
            so the closes below are the ingested daily series and carry NO trade markers.
          </span>
        )}
      </p>

      {strategy === 'covered_call' && (
        <p data-testid="price-cc-banner" className="text-xs text-amber-300 mb-2">
          Synthetic lot — 100 shares at the window-start close (D2).
        </p>
      )}

      {mode === 'replay' && artifactAbsence && (
        <p data-testid="price-no-markers" className="text-xs text-amber-400 mb-2">
          No detail artifact for this cell — {artifactAbsence} The closes are drawn; the markers
          are not, because the events that would place them are in the artifact.
        </p>
      )}

      <div className="h-72">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={rows} margin={{ top: 5, right: 5, left: 0, bottom: 5 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
            <XAxis
              dataKey="date"
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              tickFormatter={fmtDateShort}
              minTickGap={28}
            />
            <YAxis
              tick={{ fill: '#9ca3af', fontSize: 11 }}
              domain={['auto', 'auto']}
              tickFormatter={(n: number) => fmtCurrency(n, { compact: true })}
            />
            <Tooltip
              content={({ active, payload }) => {
                if (!active || !payload || payload.length === 0) return null;
                const row = payload[0].payload as ChartRow;
                return (
                  <div className="bg-gray-800 border border-gray-600 rounded px-3 py-2 text-xs text-gray-200 max-w-xs">
                    <div className="font-semibold">{fmtDateShort(row.date)}</div>
                    <div>Close {fmtCurrency(row.close)}</div>
                    <EventLines events={row.events} />
                  </div>
                );
              }}
            />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Line
              type="monotone"
              dataKey="close"
              name="Close"
              stroke="#e5e7eb"
              strokeWidth={1.5}
              dot={false}
            />
            {rollLines.map((line, i) => (
              <ReferenceLine
                key={`${line.day}-${i}`}
                x={line.date}
                stroke="#fbbf24"
                strokeDasharray="3 3"
                label={{ value: 'roll', fill: '#fbbf24', fontSize: 9, position: 'top' }}
              />
            ))}
            {markers.map((marker) => (
              <Scatter
                key={marker.key}
                dataKey={marker.key}
                name={`${marker.glyph} ${marker.label} (${marker.count})`}
                fill={marker.color}
                shape={marker.shape}
                isAnimationActive={false}
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {rollLines.length > 0 && (
        <p data-testid="price-roll-note" className="text-xs text-gray-500 mt-2">
          {rollLines.length} roll{rollLines.length === 1 ? '' : 's'}, dashed:{' '}
          {rollLines.map((l) => l.label).join(' · ')}
        </p>
      )}
      {offSessionEvents > 0 && (
        <p data-testid="price-off-session" className="text-xs text-amber-400 mt-1">
          {offSessionEvents} event{offSessionEvents === 1 ? '' : 's'} fell on a non-session date
          (a weekend expiry, say) and {offSessionEvents === 1 ? 'is' : 'are'} drawn on the previous
          session&rsquo;s close.
        </p>
      )}
      {droppedEvents.length > 0 && (
        <p data-testid="price-dropped" className="text-xs text-red-400 mt-1">
          {droppedEvents.length} event{droppedEvents.length === 1 ? '' : 's'} predate the first
          session in this window and could not be placed on the chart. They are still in the ledger
          table below.
        </p>
      )}
    </Shell>
  );
}
