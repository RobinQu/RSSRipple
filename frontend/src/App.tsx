import { Routes, Route } from 'react-router-dom';
import AppLayout from './components/Layout';
import Dashboard from './pages/Dashboard';
import Channels from './pages/Channels';
import ChannelForm from './pages/ChannelForm';
import ChannelDetail from './pages/ChannelDetail';
import Downloaders from './pages/Downloaders';
import DownloaderForm from './pages/DownloaderForm';
import Agents from './pages/Agents';
import AgentForm from './pages/AgentForm';
import AgentDetail from './pages/AgentDetail';

function App() {
  return (
    <Routes>
      <Route path="/" element={<AppLayout />}>
        <Route index element={<Dashboard />} />
        <Route path="channels" element={<Channels />} />
        <Route path="channels/new" element={<ChannelForm />} />
        <Route path="channels/:id" element={<ChannelDetail />} />
        <Route path="downloaders" element={<Downloaders />} />
        <Route path="downloaders/new" element={<DownloaderForm />} />
        <Route path="agents" element={<Agents />} />
        <Route path="agents/new" element={<AgentForm />} />
        <Route path="agents/:id" element={<AgentDetail />} />
      </Route>
    </Routes>
  );
}

export default App;
