import { Routes, Route } from 'react-router-dom';
import AppLayout from './components/Layout';
import Dashboard from './pages/Dashboard';
import Channels from './pages/Channels';
import ChannelForm from './pages/ChannelForm';
import ChannelDetail from './pages/ChannelDetail';
import Downloaders from './pages/Downloaders';
import DownloaderForm from './pages/DownloaderForm';
import DownloaderDetail from './pages/DownloaderDetail';
import Agents from './pages/Agents';
import AgentForm from './pages/AgentForm';
import AgentDetail from './pages/AgentDetail';
import Series from './pages/Series';
import SeriesDetail from './pages/SeriesDetail';
import Movies from './pages/Movies';
import MovieDetail from './pages/MovieDetail';
import WorksPage from './pages/WorksPage';
import PageErrorBoundary from './components/PageErrorBoundary';

function App() {
  return (
    <PageErrorBoundary>
      <Routes>
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
          <Route path="agents" element={<Agents />} />
          <Route path="agents/new" element={<AgentForm />} />
          <Route path="agents/:id/edit" element={<AgentForm />} />
          <Route path="agents/:id" element={<AgentDetail />} />
          <Route path="series" element={<Series />} />
          <Route path="series/:id" element={<SeriesDetail />} />
          <Route path="movies" element={<Movies />} />
          <Route path="movies/:id" element={<MovieDetail />} />
        </Route>
      </Routes>
    </PageErrorBoundary>
  );
}

export default App;
