import { describe, expect, test } from 'bun:test'
import { businessRunUrl, parseBusinessLinks, safeDownloadUrl } from '../app/utils/operations'
import type { IntegrationRun } from '../app/types/operations'

describe('operations links stay within their intended trust boundaries', () => {
  test('download accepts only API links on the current origin', () => {
    expect(safeDownloadUrl('/api/v1/integration/analysis-runs/1/outputs/download?key=x', 'http://bwm.test')).toStartWith('/api/v1/')
    expect(safeDownloadUrl('https://untrusted.test/file', 'http://bwm.test')).toBe('')
    expect(safeDownloadUrl('javascript:alert(1)', 'http://bwm.test')).toBe('')
  })
  test('business navigation uses configured URLs and encoded identifiers', () => {
    const links = parseBusinessLinks('[{"clientId":"a","name":"A","analysisUrlTemplate":"https://a.test/runs/{analysis_id}"}]')
    const run = { external_ref: { client_id: 'a', external_analysis_id: 'one/two' } } as IntegrationRun
    expect(businessRunUrl(run, links)).toBe('https://a.test/runs/one%2Ftwo')
    expect(businessRunUrl({ external_ref: { client_id: 'b' } } as IntegrationRun, links)).toBe('')
    expect(parseBusinessLinks('[{"clientId":"a","name":"A","analysisUrlTemplate":"javascript:alert(1)"}]')).toEqual([])
  })
})
