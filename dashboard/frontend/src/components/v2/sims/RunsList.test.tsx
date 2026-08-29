// FC-060 Layer 4 (PR-B): the runs list.
//
// The failure this file exists to prevent is a run that is going nowhere looking
// exactly like a run that is progressing. Status is read from BigQuery, not from
// the Cloud Run execution (plan D3), so a Job that dies before its first write
// leaves a `submitted` row forever — the "stuck" hint is the only thing that
// distinguishes that from a container still starting.

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import RunsList from './RunsList';
import type { SweepRow } from '../../../types/v2';

const row = (over: Partial<SweepRow> = {}): SweepRow => ({
  run_id: 'run1',
  sweep_key: 'key1',
  status: 'running',
  deduplicated_to: null,
  submitted_at: new Date().toISOString(),
  started_at: null,
  finished_at: null,
  submitted_via: 'dashboard',
  execution_name: null,
  git_commit: null,
  engine_version: null,
  base_config_hash: null,
  base_config_json: null,
  spec_json: null,
  symbols: ['AAPL', 'NVDA'],
  window_start: '2025-08-28',
  window_end: '2026-08-28',
  holdout_start: '2026-05-30',
  in_sample_only: false,
  scenario_count: 3,
  cell_count: 16,
  wall_seconds: null,
  materialise_seconds: null,
  replay_seconds: null,
  provider_fetches: null,
  bar_cache_hits: null,
  lake_summary_json: null,
  error: null,
  ...over,
});

const view = (rows: SweepRow[], onSelect = vi.fn()) => {
  render(
    <RunsList sweeps={rows} selectedRunId={null} onSelect={onSelect} loading={false} error={null} />,
  );
  return onSelect;
};

describe('RunsList', () => {
  it('renders a status pill per run', () => {
    view([row({ run_id: 'a', status: 'done' }), row({ run_id: 'b', status: 'failed' })]);
    expect(screen.getByTestId('status-done')).toBeInTheDocument();
    expect(screen.getByTestId('status-failed')).toBeInTheDocument();
  });

  it('hints "stuck" on a submitted row older than 10 minutes, and not before', () => {
    const old = new Date(Date.now() - 11 * 60_000).toISOString();
    const fresh = new Date(Date.now() - 2 * 60_000).toISOString();
    const { unmount } = render(
      <RunsList
        sweeps={[row({ status: 'submitted', submitted_at: fresh })]}
        selectedRunId={null}
        onSelect={vi.fn()}
        loading={false}
        error={null}
      />,
    );
    expect(screen.queryByText(/stuck/)).toBeNull();
    unmount();
    view([row({ status: 'submitted', submitted_at: old, execution_name: 'exec-xyz' })]);
    expect(screen.getByText(/stuck — check the execution/)).toBeInTheDocument();
    expect(screen.getByText('exec-xyz')).toBeInTheDocument();
  });

  it('links a deduplicated run at the run it points to', () => {
    const onSelect = view([row({ status: 'deduplicated', deduplicated_to: 'older-run' })]);
    fireEvent.click(screen.getByRole('button', { name: /older-run/ }));
    expect(onSelect).toHaveBeenCalledWith('older-run');
  });

  it('shows a failed run’s error rather than an empty row', () => {
    view([row({ status: 'failed', error: 'UnadjustedCorporateAction on IWM' })]);
    expect(screen.getByText('UnadjustedCorporateAction on IWM')).toBeInTheDocument();
  });

  it('flags an in-sample-only run in the list, before its results are opened', () => {
    view([row({ in_sample_only: true })]);
    expect(screen.getByText(/in-sample only/)).toBeInTheDocument();
  });

  it('says the list is empty rather than showing a blank table', () => {
    view([]);
    expect(screen.getByText(/No sweeps yet/)).toBeInTheDocument();
  });

  it('keeps rendered rows and warns when a refresh fails', () => {
    render(
      <RunsList
        sweeps={[row()]}
        selectedRunId={null}
        onSelect={vi.fn()}
        loading={false}
        error="BigQuery timeout"
      />,
    );
    expect(screen.getByText(/Could not refresh the runs list/)).toBeInTheDocument();
    expect(screen.getByText('run1')).toBeInTheDocument();
  });
});
