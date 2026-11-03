import { CheckIcon, CopyIcon, LoaderCircleIcon, PlugIcon, Trash2Icon } from 'lucide-react';
import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { ConfirmDialog } from '@/features/clone/confirm-dialog';
import { apiFetch, apiJson, describeError } from '@/lib/api/client';
import { SettingsActionError } from './settings-action-error';
import { SettingsRow, SettingsSection } from './settings-layout';

export interface McpConnection {
  client_id: string;
  name: string;
  redirect_host: string;
  created_at: number | null;
  last_used_at: number | null;
  grants: number;
}

/** Claude / ChatGPT custom-connector card: the /mcp URL and approved connections. */
export function McpConnectorSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const info = useQuery({
    queryKey: ['mcp-oauth-info'],
    queryFn: ({ signal }) => apiJson<{ mcp_url: string }>('/api/mcp/oauth/info', { signal }),
  });
  const connections = useQuery({
    queryKey: ['mcp-oauth-connections'],
    queryFn: ({ signal }) => apiJson<McpConnection[]>('/api/mcp/oauth/connections', { signal }),
  });
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState('');
  const [confirm, setConfirm] = useState<McpConnection | null>(null);
  const [revokingId, setRevokingId] = useState<string | null>(null);

  const url = info.data?.mcp_url ?? '';

  const copy = async () => {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  const revoke = async (connection: McpConnection) => {
    setRevokingId(connection.client_id);
    setError('');
    try {
      await apiFetch(`/api/mcp/oauth/connections/${encodeURIComponent(connection.client_id)}`, {
        method: 'DELETE',
      });
      await queryClient.invalidateQueries({ queryKey: ['mcp-oauth-connections'] });
    } catch (err) {
      setError(describeError(err));
    } finally {
      setRevokingId(null);
    }
  };

  const loadError = info.isError ? info.error : connections.isError ? connections.error : null;

  return (
    <>
      <SettingsSection icon={PlugIcon} title={t('mcp_connector.title')}>
        <SettingsRow
          id="sharing-mcp-connector-url"
          title={t('mcp_connector.url_label')}
          description={t('mcp_connector.desc')}
        >
          <div className="flex w-full min-w-0 items-center gap-2 @xl:w-[28rem]">
            <Input
              readOnly
              value={url}
              aria-label={t('mcp_connector.url_label')}
              className="font-mono"
              onFocus={(event) => event.currentTarget.select()}
            />
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              disabled={!url}
              aria-label={copied ? t('mcp_connector.copied') : t('mcp_connector.copy')}
              onClick={() => void copy()}
            >
              {copied ? <CheckIcon /> : <CopyIcon />}
            </Button>
          </div>
        </SettingsRow>

        {(loadError || error) && (
          <div className="p-4">
            <SettingsActionError
              title={loadError ? t('mcp_connector.load_failed') : error}
              detail={loadError ? describeError(loadError) : undefined}
              onDismiss={error ? () => setError('') : undefined}
            />
          </div>
        )}

        {connections.isPending ? (
          <div className="flex min-h-16 items-center justify-center p-4 text-muted-foreground">
            <LoaderCircleIcon aria-label={t('common.loading')} className="size-4 animate-spin" />
          </div>
        ) : connections.data?.length ? (
          connections.data.map((connection) => (
            <SettingsRow
              key={connection.client_id}
              id={`mcp-connection-${connection.client_id}`}
              title={connection.name}
              description={connection.redirect_host}
            >
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                disabled={revokingId === connection.client_id}
                aria-label={t('mcp_connector.revoke', { name: connection.name })}
                onClick={() => setConfirm(connection)}
              >
                {revokingId === connection.client_id ? (
                  <LoaderCircleIcon className="animate-spin" />
                ) : (
                  <Trash2Icon />
                )}
              </Button>
            </SettingsRow>
          ))
        ) : (
          <p className="px-4 py-3 text-sm text-muted-foreground">{t('mcp_connector.empty')}</p>
        )}
      </SettingsSection>

      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
        title={t('mcp_connector.revoke', { name: confirm?.name ?? '' })}
        description={t('mcp_connector.revoke_confirm', { name: confirm?.name ?? '' })}
        confirmLabel={t('mcp_connector.revoke', { name: confirm?.name ?? '' })}
        onConfirm={async () => {
          const target = confirm;
          if (target) await revoke(target);
        }}
      />
    </>
  );
}
