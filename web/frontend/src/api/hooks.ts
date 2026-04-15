import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from './client';
import type { TaskCreate, TournamentStartOpts } from '../types';

function invalidateTaskQueries(qc: ReturnType<typeof useQueryClient>, id?: string) {
  void qc.invalidateQueries({ queryKey: ['tasks'] });
  void qc.invalidateQueries({ queryKey: ['dashboard'] });
  void qc.invalidateQueries({ queryKey: ['pipeline-status'] });
  if (id) {
    void qc.invalidateQueries({ queryKey: ['task', id] });
  }
}

export function useDashboard() {
  return useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard.get, refetchInterval: 5000 });
}

export function useTasks(status?: string) {
  return useQuery({ queryKey: ['tasks', status], queryFn: () => api.tasks.list(status), refetchInterval: 5000 });
}

export function useTask(id: string) {
  return useQuery({
    queryKey: ['task', id],
    queryFn: () => api.tasks.get(id),
    enabled: Boolean(id),
    refetchInterval: 3000,
  });
}

export function useCreateTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: TaskCreate) => api.tasks.create(data),
    onSuccess: (task) => invalidateTaskQueries(qc, task.id),
  });
}

export function useRunTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.tasks.run(id),
    onSuccess: (task) => invalidateTaskQueries(qc, task.id),
  });
}

export function useCancelTask() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.tasks.cancel(id),
    onSuccess: (task) => invalidateTaskQueries(qc, task.id),
  });
}

export function usePipelineStatus() {
  return useQuery({
    queryKey: ['pipeline-status'],
    queryFn: api.tasks.pipeline.status,
    refetchInterval: 5000,
  });
}

export function usePausePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.tasks.pipeline.pause,
    onSuccess: () => invalidateTaskQueries(qc),
  });
}

export function useResumePipeline() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: api.tasks.pipeline.resume,
    onSuccess: () => invalidateTaskQueries(qc),
  });
}

export function useBulkRunTasks() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) => api.tasks.bulk.run(ids),
    onSuccess: () => invalidateTaskQueries(qc),
  });
}

export function useBulkCancelTasks() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) => api.tasks.bulk.cancel(ids),
    onSuccess: () => invalidateTaskQueries(qc),
  });
}

export function useBulkDeleteTasks() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) => api.tasks.bulk.delete(ids),
    onSuccess: () => invalidateTaskQueries(qc),
  });
}

export function useModels() {
  return useQuery({ queryKey: ['models'], queryFn: api.models.list });
}

export function useHardware() {
  return useQuery({ queryKey: ['hardware'], queryFn: api.hardware.get, refetchInterval: 30000 });
}

export function useSmashModels() {
  return useQuery({ queryKey: ['smash-models'], queryFn: api.smash.models });
}

export function useSettings() {
  return useQuery({ queryKey: ['settings'], queryFn: api.settings.list });
}

export function useUpdateSettings() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (settings: Record<string, string>) => api.settings.update(settings),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['settings'] }),
  });
}

export function useTournamentResults() {
  return useQuery({ queryKey: ['tournament-results'], queryFn: api.tournament.results });
}

export function useTournamentTasks() {
  return useQuery({ queryKey: ['tournament-tasks'], queryFn: api.tournament.tasks });
}

export function useTournamentLeaderboard() {
  return useQuery({ queryKey: ['tournament-leaderboard'], queryFn: api.tournament.leaderboard });
}

export function useStartTournament() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (opts: TournamentStartOpts) => api.tournament.start(opts),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['tournament-results'] }),
  });
}

export function useRuns(taskId?: string) {
  return useQuery({ queryKey: ['runs', taskId], queryFn: () => api.runs.list(taskId) });
}
