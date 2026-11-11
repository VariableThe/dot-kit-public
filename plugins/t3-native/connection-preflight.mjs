/** Read-only preflight through the caller's existing native T3 connector. */
import { payload } from './completion-guard.mjs';

const MODES = new Set(['approval-required', 'auto-accept-edits', 'auto', 'full-access']);
const ERROR_CODES = new Set(['auth_error', 'unauthorized', 'forbidden', 'permission_denied', 'connection_error', 'timeout', 'deadline_exceeded', 'ECONNRESET', 'ETIMEDOUT']);
const nonempty = value => typeof value === 'string' && value.trim().length > 0;

/**
 * callTool must invoke the existing authorized native connector, not a local
 * model session. No URLs, tokens, credential repair, writes, retries, or launches.
 * Advertised runtime mode is a ceiling, not user authorization or a write test.
 */
export async function checkNativeConnection({ expectedEnvironmentId, callTool }) {
  if (!nonempty(expectedEnvironmentId) || typeof callTool !== 'function')
    throw new TypeError('A verified environment ID and existing connector callback are required');

  const checks = [];
  const base = { environmentId: expectedEnvironmentId, checks, writeExecutionVerified: false };
  let activeCheck;
  const read = async (tool, extra = {}) => {
    activeCheck = tool;
    const response = await callTool(tool, { environmentId: expectedEnvironmentId, ...extra });
    if (response?.isError === true) {
      const code = response.structuredContent?.error?.code ?? response.structuredContent?.code;
      throw Object.assign(new Error('Connector read failed'), { code });
    }
    const data = payload(response);
    if (!data) throw Object.assign(new Error('Invalid response'), { code: 'invalid_response' });
    return data;
  };
  try {
    const environment = await read('t3_environment_read');
    if (!nonempty(environment.environmentId) || !nonempty(environment.serverVersion))
      return { ...base, state: 'invalid_response', failedCheck: activeCheck };
    if (environment.environmentId !== expectedEnvironmentId)
      return { ...base, state: 'environment_mismatch', failedCheck: activeCheck };
    checks.push(activeCheck);

    const projects = await read('t3_project_list', { limit: 1 });
    if (!Array.isArray(projects.projects) || !projects.projects.every(project => nonempty(project?.id)) ||
        !(projects.nextCursor === null || Number.isInteger(projects.nextCursor) && projects.nextCursor >= 0))
      return { ...base, state: 'invalid_response', failedCheck: activeCheck };
    checks.push(activeCheck);

    const capabilities = await read('orchestrator_capabilities');
    if (!MODES.has(capabilities.runtimeMode) || !['default', 'plan'].includes(capabilities.interactionMode))
      return { ...base, state: 'invalid_response', failedCheck: activeCheck };
    checks.push(activeCheck);

    return {
      ...base, state: 'read_ready', serverVersion: environment.serverVersion,
      runtimeCeiling: capabilities.runtimeMode, interactionMode: capabilities.interactionMode,
      projectPageReadable: true,
    };
  } catch (error) {
    const errorCode = ERROR_CODES.has(error?.code) || error?.code === 'invalid_response'
      ? error.code : 'connector_read_failed';
    return { ...base, state: 'blocked', failedCheck: activeCheck, errorCode };
  }
}
