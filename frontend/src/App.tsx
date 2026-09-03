import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import AppLayout from './components/Layout';
import PageErrorBoundary from './components/PageErrorBoundary';

const Dashboard = lazy(() => import('./pages/Dashboard'));
const Channels = lazy(() => import('./pages/Channels'));
const ChannelForm = lazy(() => import('./pages/ChannelForm'));
const ChannelDetail = lazy(() => import('./pages/ChannelDetail'));
const Downloaders = lazy(() => import('./pages/Downloaders'));
const DownloaderForm = lazy(() => import('./pages/DownloaderForm'));
const DownloaderDetail = lazy(() => import('./pages/DownloaderDetail'));
const Agents = lazy(() => import('./pages/Agents'));
const AgentForm = lazy(() => import('./pages/AgentForm'));
const AgentDetail = lazy(() => import('./pages/AgentDetail'));
const SeriesDetail = lazy(() => import('./pages/SeriesDetail'));
const MovieDetail = lazy(() => import('./pages/MovieDetail'));
const WorkEditPage = lazy(() => import('./pages/WorkEditPage'));
const AudioWorkDetail = lazy(() => import('./pages/AudioWorkDetail'));
const WorksPage = lazy(() => import('./pages/WorksPage'));
const CollectionDetail = lazy(() => import('./pages/CollectionDetail'));
const MediaLibrary = lazy(() => import('./pages/MediaLibrary'));
const SettingsPage = lazy(() => import('./pages/Settings'));
const Login = lazy(() => import('./pages/Login'));

function App() {
  return (
    <PageErrorBoundary>
      <Routes>
        {/* Login sits outside AppLayout — no sidebar until authenticated. */}
        <Route
          path="/login"
          element={(
            <Suspense fallback={<div className="route-loading" aria-label="Loading" />}>
              <Login />
            </Suspense>
          )}
        />
        <Route path="/" element={<AppLayout />}>
          <Route index element={<Dashboard />} />
          <Route path="works" element={<WorksPage />} />
          <Route path="channels" element={<Channels />} />
          <Route path="channels/new" element={<ChannelForm />} />
          <Route path="channels/:id/edit" element={<ChannelForm />} />
          <Route path="channels/:id" element={<ChannelDetail />} />
          <Route path="downloaders" element={<Downloaders />} />
          <Route path="downloaders/new" element={<DownloaderForm />} />
          <Route path="downloaders/:id/edit" element={<DownloaderForm />} />
          <Route path="downloaders/:id" element={<DownloaderDetail />} />
          {/* Storage volumes now live in System Settings (/settings). */}
          <Route path="volumes" element={<Navigate to="/settings" replace />} />
          <Route path="agents" element={<Agents />} />
          <Route path="agents/new" element={<AgentForm />} />
          <Route path="agents/:id/edit" element={<AgentForm />} />
          <Route path="agents/:id" element={<AgentDetail />} />
          {/* The works library (/works) is the single work list page; the old
              /series and /movies list pages were removed. Detail routes stay,
              and stale list links redirect to /works. */}
          <Route path="series" element={<Navigate to="/works" replace />} />
          <Route path="series/:id" element={<SeriesDetail />} />
          <Route path="series/:id/edit" element={<WorkEditPage contentType="tv" />} />
          <Route path="movies" element={<Navigate to="/works" replace />} />
          <Route path="movies/:id" element={<MovieDetail />} />
          <Route path="movies/:id/edit" element={<WorkEditPage contentType="movie" />} />
          <Route path="audio-works/:id" element={<AudioWorkDetail />} />
          {/* Collection detail is a full page; the list stays inside the
              /works 合集 browse mode (CollectionsPanel). */}
          <Route path="collections/:id" element={<CollectionDetail />} />
          {/* Media library file organization consolidates the former
              /media-servers and /organize pages into one tabbed module. */}
          <Route path="media-library" element={<MediaLibrary />} />
          <Route path="media-servers" element={<Navigate to="/media-library" replace />} />
          <Route path="organize" element={<Navigate to="/media-library" replace />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </PageErrorBoundary>
  );
}

export default App;
