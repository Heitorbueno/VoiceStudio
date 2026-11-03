import { expect, it } from 'vitest';
import { isSetupExemptHash } from './setup-gate';

it('lets only the connector approval route skip the first-run wizard', () => {
  expect(isSetupExemptHash('#/mcp-authorize?req=abc')).toBe(true);
  expect(isSetupExemptHash('#/mcp-authorize')).toBe(true);
  expect(isSetupExemptHash('#/mcp-authorizex')).toBe(false);
  expect(isSetupExemptHash('#/settings/sharing')).toBe(false);
  expect(isSetupExemptHash('#/')).toBe(false);
  expect(isSetupExemptHash('')).toBe(false);
});
