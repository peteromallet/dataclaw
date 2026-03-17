type BadgeKind = 'outcome' | 'value' | 'risk' | 'status';

const COLORS: Record<BadgeKind, Record<string, { bg: string; fg: string }>> = {
  outcome: {
    tests_passed: { bg: '#dcfce7', fg: '#166534' },
    tests_failed: { bg: '#fee2e2', fg: '#991b1b' },
    build_failed: { bg: '#fee2e2', fg: '#991b1b' },
    analysis_only: { bg: '#e0f2fe', fg: '#075985' },
    unknown: { bg: '#f3f4f6', fg: '#6b7280' },
  },
  value: {
    novel_domain: { bg: '#faf5ff', fg: '#7e22ce' },
    long_horizon: { bg: '#fff7ed', fg: '#c2410c' },
    tool_rich: { bg: '#f0fdf4', fg: '#15803d' },
    scientific_workflow: { bg: '#eff6ff', fg: '#1d4ed8' },
    debugging: { bg: '#fefce8', fg: '#a16207' },
  },
  risk: {
    secrets_detected: { bg: '#fee2e2', fg: '#991b1b' },
    names_detected: { bg: '#fef3c7', fg: '#92400e' },
    private_url: { bg: '#fef3c7', fg: '#92400e' },
    manual_review: { bg: '#fce7f3', fg: '#9d174d' },
  },
  status: {
    new: { bg: '#e0f2fe', fg: '#075985' },
    shortlisted: { bg: '#fef3c7', fg: '#92400e' },
    approved: { bg: '#dcfce7', fg: '#166534' },
    blocked: { bg: '#fee2e2', fg: '#991b1b' },
    uploaded: { bg: '#f3e8ff', fg: '#7e22ce' },
  },
};

const LABELS: Record<string, string> = {
  tests_passed: 'Tests Passed',
  tests_failed: 'Tests Failed',
  build_failed: 'Build Failed',
  analysis_only: 'Analysis',
  unknown: 'Unknown',
  novel_domain: 'Novel Domain',
  long_horizon: 'Long Horizon',
  tool_rich: 'Tool Rich',
  scientific_workflow: 'Scientific',
  debugging: 'Debugging',
  secrets_detected: 'Secrets',
  names_detected: 'Names',
  private_url: 'Private URL',
  manual_review: 'Review Needed',
  new: 'New',
  shortlisted: 'Shortlisted',
  approved: 'Approved',
  blocked: 'Blocked',
  uploaded: 'Uploaded',
};

export function BadgeChip({ kind, value }: { kind: BadgeKind; value: string }) {
  const palette = COLORS[kind]?.[value] ?? { bg: '#f3f4f6', fg: '#6b7280' };
  const label = LABELS[value] ?? value.replace(/_/g, ' ');

  return (
    <span
      style={{
        display: 'inline-block',
        padding: '1px 8px',
        borderRadius: '9999px',
        fontSize: '11px',
        fontWeight: 500,
        lineHeight: '20px',
        background: palette.bg,
        color: palette.fg,
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  );
}
