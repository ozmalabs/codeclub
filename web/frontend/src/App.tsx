import { Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import Dashboard from './pages/Dashboard';
import Tasks from './pages/Tasks';
import TaskDetail from './pages/TaskDetail';
import Models from './pages/Models';
import SmashMaps from './pages/SmashMaps';
import Tournament from './pages/Tournament';
import Git from './pages/Git';
import Hardware from './pages/Hardware';
import Settings from './pages/Settings';

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="tasks" element={<Tasks />} />
        <Route path="tasks/:id" element={<TaskDetail />} />
        <Route path="models" element={<Models />} />
        <Route path="maps" element={<SmashMaps />} />
        <Route path="tournament" element={<Tournament />} />
        <Route path="git" element={<Git />} />
        <Route path="hardware" element={<Hardware />} />
        <Route path="settings" element={<Settings />} />
      </Route>
    </Routes>
  );
}
