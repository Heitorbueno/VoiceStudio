import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mock = vi.hoisted(() => ({ api: vi.fn(), search: { req: 'req-1' } as { req?: string } }));

vi.mock('@/lib/api/client', async (load) => {
  const actual = await load<typeof import('@/lib/api/client')>();
  return { ...actual, apiJson: mock.api };
});
vi.mock('@tanstack/react-router', () => ({ useSearch: () => mock.search }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import { McpAuthorizePage, safeRedirect } from './mcp-authorize-page';

const assign = vi.fn();

beforeEach(() => {
  mock.search = { req: 'req-1' };
  vi.stubGlobal('location', { ...window.location, assign });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <McpAuthorizePage />
    </QueryClientProvider>,
  );
}

it('only follows redirects to the validated connector host over https', () => {
  expect(safeRedirect('https://claude.ai/cb?code=x', 'claude.ai')).toBe(
    'https://claude.ai/cb?code=x',
  );
  expect(safeRedirect('https://evil.example/cb', 'claude.ai')).toBeNull();
  expect(safeRedirect('http://claude.ai/cb', 'claude.ai')).toBeNull();
  expect(safeRedirect('javascript:alert(1)', 'claude.ai')).toBeNull();
});

it('approves the pending request and returns to the connector', async () => {
  mock.api.mockImplementation((path: string) => {
    if (path === '/api/mcp/oauth/requests/req-1')
      return Promise.resolve({
        id: 'req-1',
        client_name: 'Claude',
        redirect_host: 'claude.ai',
        scope: 'mcp',
      });
    return Promise.resolve({
      redirect_url: 'https://claude.ai/api/mcp/auth_callback?code=c&state=s',
    });
  });
  renderPage();

  fireEvent.click(await screen.findByRole('button', { name: 'mcp_connector.approve' }));

  await waitFor(() =>
    expect(assign).toHaveBeenCalledWith('https://claude.ai/api/mcp/auth_callback?code=c&state=s'),
  );
  expect(mock.api).toHaveBeenCalledWith('/api/mcp/oauth/requests/req-1/decision', {
    method: 'POST',
    body: JSON.stringify({ approve: true }),
  });
});

it('refuses a decision that would send the browser to another host', async () => {
  mock.api.mockImplementation((path: string) => {
    if (path === '/api/mcp/oauth/requests/req-1')
      return Promise.resolve({
        id: 'req-1',
        client_name: 'Claude',
        redirect_host: 'claude.ai',
        scope: 'mcp',
      });
    return Promise.resolve({ redirect_url: 'https://evil.example/steal?code=c' });
  });
  renderPage();

  fireEvent.click(await screen.findByRole('button', { name: 'mcp_connector.deny' }));

  expect(await screen.findByRole('alert')).toHaveTextContent('mcp_connector.expired');
  expect(assign).not.toHaveBeenCalled();
});

it('shows the expired notice without a request id', () => {
  mock.search = {};
  renderPage();
  expect(screen.getByRole('alert')).toHaveTextContent('mcp_connector.expired');
  expect(mock.api).not.toHaveBeenCalled();
});
