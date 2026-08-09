import { api } from './client';
import type { AuthStatus } from '../types';

export const authApi = {
  status: () => api.get<AuthStatus>('/auth/status'),
  verifyOtp: (code: string) => api.post<AuthStatus>('/auth/otp', { code }),
  logout: () => api.post<AuthStatus>('/auth/logout'),
};
