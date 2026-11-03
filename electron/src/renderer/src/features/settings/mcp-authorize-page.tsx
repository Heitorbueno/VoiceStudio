import { LoaderCircleIcon, PlugIcon } from 'lucide-react';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useSearch } from '@tanstack/react-router';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { apiJson, describeError } from '@/lib/api/client';

interface PendingRequest {
  id: string;
  client_name: string;
  redirect_host: string;
  scope: string;
}

/** Only ever hand the browser to the connector host the backend validated. */
export function safeRedirect(url: string, expectedHost: string): string | null {
  try {
    const parsed = new URL(url);
    const local = parsed.hostname === 'localhost' || parsed.hostname === '127.0.0.1';
    if (parsed.hostname !== expectedHost) return null;
    if (parsed.protocol !== 'https:' && !(local && parsed.protocol === 'http:')) return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

/** Operator approval of a Claude / ChatGPT connector (OAuth consent). */
export function McpAuthorizePage() {
  const { t } = useTranslation();
  const search = useSearch({ strict: false }) as { req?: unknown };
  const requestId = typeof search.req === 'string' ? search.req : '';
  const pending = useQuery({
    queryKey: ['mcp-oauth-request', requestId],
    enabled: Boolean(requestId),
    retry: false,
    queryFn: ({ signal }) =>
      apiJson<PendingRequest>(`/api/mcp/oauth/requests/${encodeURIComponent(requestId)}`, {
        signal,
      }),
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const decide = async (approve: boolean) => {
    if (!pending.data || busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await apiJson<{ redirect_url: string }>(
        `/api/mcp/oauth/requests/${encodeURIComponent(requestId)}/decision`,
        { method: 'POST', body: JSON.stringify({ approve }) },
      );
      const target = safeRedirect(result.redirect_url, pending.data.redirect_host);
      if (!target) throw new Error(t('mcp_connector.expired'));
      window.location.assign(target);
    } catch (err) {
      setError(describeError(err));
      setBusy(false);
    }
  };

  const expired = !requestId || pending.isError;

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-md space-y-4 rounded-xl border bg-card p-6 shadow-sm">
        <div className="flex items-center gap-2">
          <PlugIcon className="size-5 text-muted-foreground" aria-hidden="true" />
          <h1 className="text-lg font-semibold">{t('mcp_connector.authorize_title')}</h1>
        </div>

        {pending.isPending && !expired ? (
          <div className="flex justify-center py-6 text-muted-foreground">
            <LoaderCircleIcon aria-label={t('common.loading')} className="size-5 animate-spin" />
          </div>
        ) : expired || !pending.data ? (
          <p role="alert" className="text-sm text-muted-foreground">
            {t('mcp_connector.expired')}
          </p>
        ) : (
          <>
            <p className="text-sm">
              {t('mcp_connector.authorize_desc', {
                name: pending.data.client_name,
                host: pending.data.redirect_host,
              })}
            </p>
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="outline" disabled={busy} onClick={() => void decide(false)}>
                {t('mcp_connector.deny')}
              </Button>
              <Button disabled={busy} onClick={() => void decide(true)}>
                {busy && <LoaderCircleIcon className="animate-spin" />}
                {t('mcp_connector.approve')}
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
