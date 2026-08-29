import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import LayoutV2 from './components/v2/LayoutV2';
import ErrorBoundary from './components/ErrorBoundary';
import OverviewV2 from './pages/v2/Overview';
import SymbolDeepDiveV2 from './pages/v2/SymbolDeepDive';
import BotHealthV2 from './pages/v2/BotHealth';
import SimulationsV2 from './pages/v2/Simulations';

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <Routes>
          {/* FC-018 canonical routes (PR G dropped the /v2/ prefix). */}
          <Route element={<LayoutV2 />}>
            <Route path="/"                    element={<OverviewV2 />} />
            <Route path="/overview"            element={<OverviewV2 />} />
            <Route path="/symbol"              element={<SymbolDeepDiveV2 />} />
            <Route path="/symbol/:underlying"  element={<SymbolDeepDiveV2 />} />
            <Route path="/bot-health"          element={<BotHealthV2 />} />
            {/* FC-060 Layer 4: scenario sweeps. Simulations, not the live book. */}
            <Route path="/sims"                element={<SimulationsV2 />} />
          </Route>

          {/* Bookmark redirects: every old path lands on the new equivalent. */}
          <Route path="/v2/overview"           element={<Navigate to="/overview"   replace />} />
          <Route path="/v2/symbol"             element={<Navigate to="/symbol"     replace />} />
          <Route path="/v2/symbol/:underlying" element={<RedirectV2Symbol />} />
          <Route path="/v2/bot-health"         element={<Navigate to="/bot-health" replace />} />

          {/* Legacy v1 paths from before PR F. */}
          <Route path="/positions"   element={<Navigate to="/overview"  replace />} />
          <Route path="/trades"      element={<Navigate to="/overview"  replace />} />
          <Route path="/performance" element={<Navigate to="/overview"  replace />} />
          <Route path="/cycles"      element={<Navigate to="/symbol"    replace />} />

          {/* Catch-all. */}
          <Route path="*" element={<Navigate to="/overview" replace />} />
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}

// Lazy import for the redirect helper so we keep <Routes> readable.
import { useParams } from 'react-router-dom';
function RedirectV2Symbol() {
  const { underlying } = useParams<{ underlying?: string }>();
  return <Navigate to={underlying ? `/symbol/${underlying}` : '/symbol'} replace />;
}
