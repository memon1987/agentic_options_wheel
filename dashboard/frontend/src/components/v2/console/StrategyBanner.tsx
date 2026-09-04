// FC-096 Phase E PR-3 (§Degrading for CC), review round 1 R8: the premise
// banner that must appear on EVERY chart and table of a covered-call cell.
//
// One component, one string, keyed on the resolved strategy — not nine copies
// of a sentence that would drift apart. The premise is not decoration: a CC
// replay's benchmark, capital base and every P&L on screen are relative to a lot
// the engine INVENTED at the window-start close (D2). A reader who scrolls past
// the one card that says so reads the rest as if real shares had been bought,
// and nothing in the numbers would tell them otherwise.
//
// A wheel cell renders nothing at all: a banner that appears everywhere, always,
// stops being read.

export const SYNTHETIC_LOT_PREMISE =
  'Synthetic lot — 100 shares at the window-start close (D2). Every figure on this card is ' +
  'relative to a lot the engine created, not to a position anyone held.';

export interface StrategyBannerProps {
  /** `provenance.strategy`, resolved through `artifactStrategy`. */
  strategy: string | null | undefined;
}

export default function StrategyBanner({ strategy }: StrategyBannerProps) {
  if (strategy !== 'covered_call') return null;
  return (
    <p data-testid="cc-lot-banner" className="text-xs text-amber-300 mb-2">
      {SYNTHETIC_LOT_PREMISE}
    </p>
  );
}
