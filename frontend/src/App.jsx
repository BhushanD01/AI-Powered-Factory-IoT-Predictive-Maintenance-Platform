import { useEffect, useMemo, useState } from 'react';
import { getMaintenanceRecommendation, healthCheck, uploadAnalyticsFile } from './api/client';
import PanelCard from './components/PanelCard';

const GAUGE_META = {
  Temperature_C: { label: 'Temperature', unit: '°C', max: 110 },
  Vibration_mms: { label: 'Vibration', unit: 'mm/s', max: 25 },
  Sound_dB: { label: 'Sound Level', unit: 'dB', max: 105 },
  Oil_Level_pct: { label: 'Oil Level', unit: '%', max: 100 },
  Coolant_Level_pct: { label: 'Coolant Level', unit: '%', max: 100 },
  Power_Consumption_kW: { label: 'Power Draw', unit: 'kW', max: 320 },
  Laser_Intensity: { label: 'Laser Intensity', unit: '%', max: 110 },
  Hydraulic_Pressure_bar: { label: 'Hydraulic Pressure', unit: 'bar', max: 170 },
  Coolant_Flow_L_min: { label: 'Coolant Flow', unit: 'L/min', max: 75 },
  Heat_Index: { label: 'Heat Index', unit: '', max: 650 },
  Error_Codes_Last_30_Days: { label: 'Error Codes (30d)', unit: '', max: 10 },
  AI_Override_Events: { label: 'AI Overrides', unit: '', max: 8 },
};

const PRIMARY_GAUGES = [
  'Temperature_C',
  'Vibration_mms',
  'Sound_dB',
  'Oil_Level_pct',
  'Coolant_Level_pct',
  'Power_Consumption_kW',
];
const SECONDARY_GAUGES = [
  'Laser_Intensity',
  'Hydraulic_Pressure_bar',
  'Coolant_Flow_L_min',
  'Heat_Index',
  'Error_Codes_Last_30_Days',
  'AI_Override_Events',
];

function App() {
  const [csvFile, setCsvFile] = useState(null);
  const [manualFile, setManualFile] = useState(null);
  const [machineIdInput, setMachineIdInput] = useState('');
  const [peerScope, setPeerScope] = useState('machine_type');
  const [status, setStatus] = useState('Checking backend connection...');
  const [isLoading, setIsLoading] = useState(false);
  const [loadingPhase, setLoadingPhase] = useState('');
  const [analyticsResult, setAnalyticsResult] = useState(null);
  const [recommendation, setRecommendation] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    const pingBackend = async () => {
      try {
        const result = await healthCheck();
        setStatus(`Backend online · ${result.service || 'API ready'}`);
      } catch (err) {
        setStatus('Backend offline. Start FastAPI on port 8000.');
        setError(err.message);
      }
    };

    pingBackend();
  }, []);

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (!csvFile) {
      setError('Please upload a factory sensor CSV file first.');
      return;
    }

    setIsLoading(true);
    setLoadingPhase('analytics');
    setError('');
    setRecommendation(null);

    try {
      const response = await uploadAnalyticsFile(csvFile, {
        machineId: machineIdInput.trim() || undefined,
        peerScope,
      });
      setAnalyticsResult(response);
      setStatus(`Analytics ready for ${response.machine_id || 'selected machine'}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setIsLoading(false);
      setLoadingPhase('');
    }
  };

  const handleRecommendation = async () => {
    if (!analyticsResult) {
      setError('Generate analytics first.');
      return;
    }

    setIsLoading(true);
    setLoadingPhase('ai');
    setError('');

    try {
      const response = await getMaintenanceRecommendation(analyticsResult);
      setRecommendation(response);
      setStatus('AI maintenance recommendation generated');
    } catch (err) {
      setError(err.message);
    } finally {
      setIsLoading(false);
      setLoadingPhase('');
    }
  };

  const summary = analyticsResult?.summary;
  const currentRecord = summary?.current_record;
  const peerAnalysis = summary?.peer_analysis || [];
  const bandAssessment = summary?.band_assessment || [];
  const derivedMetrics = summary?.derived_metrics || {};
  const lifeStage = summary?.life_stage_context || {};
  const dataQualityFlags = summary?.data_quality_flags || [];

  const findBand = (column) => bandAssessment.find((b) => b.column === column)?.band;
  const findPeer = (column) => peerAnalysis.find((p) => p.column === column);

  const metrics = useMemo(() => {
    if (!summary) return [];

    const vibration = findPeer('Vibration_mms');
    const riskScore = derivedMetrics.risk_score_pct;
    const rulEst = derivedMetrics.rul_est_days;

    const riskAccent = riskScore >= 85 ? 'red' : riskScore >= 60 ? 'orange' : riskScore >= 35 ? 'amber' : 'green';
    const rulAccent = rulEst < 45 ? 'red' : rulEst < 120 ? 'amber' : 'green';
    const vibrationBand = findBand('Vibration_mms');
    const vibrationAccent = vibrationBand === 'CRITICAL' ? 'red' : vibrationBand === 'WARNING' ? 'amber' : 'teal';

    return [
      {
        icon: '⚙',
        label: 'Machine',
        value: summary.machine_id || 'N/A',
        sub: summary.machine_type || 'Unknown type',
        accent: 'amber',
      },
      {
        icon: '🏭',
        label: 'Family',
        value: summary.machine_family || 'N/A',
        sub: `${summary.peer_group_size || 0} machines in comparison group`,
        accent: 'steel',
      },
      {
        icon: '⏱',
        label: 'Operational Hours',
        value: currentRecord?.Operational_Hours != null ? currentRecord.Operational_Hours.toLocaleString() : 'N/A',
        sub: lifeStage.operational_hours_band ? `Band: ${lifeStage.operational_hours_band} h` : '',
        accent: 'sky',
      },
      {
        icon: '🛠',
        label: 'Last Maintenance',
        value: currentRecord?.Last_Maintenance_Days_Ago != null ? `${currentRecord.Last_Maintenance_Days_Ago} d ago` : 'N/A',
        sub: `${currentRecord?.Maintenance_History_Count ?? 0} services logged`,
        accent: 'teal',
      },
      {
        icon: '⚠',
        label: 'Risk Score',
        value: riskScore != null ? `${riskScore}%` : 'N/A',
        sub: lifeStage.fleet_observed_7_day_failure_rate_in_band_pct != null
          ? `${lifeStage.fleet_observed_7_day_failure_rate_in_band_pct}% fleet failure rate in this band`
          : '',
        accent: riskAccent,
      },
      {
        icon: '🛡',
        label: 'Estimated Life',
        value: rulEst != null ? `${rulEst} d est.` : 'N/A',
        sub: derivedMetrics.recorded_rul_days != null ? `Recorded: ${derivedMetrics.recorded_rul_days} d` : '',
        accent: rulAccent,
      },
      {
        icon: '📳',
        label: 'Vibration',
        value: vibration ? `${vibration.latest_value} mm/s` : 'N/A',
        sub: vibration ? vibration.position_vs_peers.replace(/_/g, ' ').toLowerCase() : '',
        accent: vibrationAccent,
      },
      {
        icon: '📊',
        label: 'Peers Compared',
        value: peerAnalysis.length,
        sub: `${summary.peer_scope === 'fleet' ? 'Whole fleet' : 'Same type'} · ${summary.peer_group_size || 0} machines`,
        accent: 'violet',
      },
      {
        icon: '🧪',
        label: 'Data Quality',
        value: dataQualityFlags.length,
        sub: dataQualityFlags.length ? 'Sensor readings need verification' : 'No plausibility issues found',
        accent: dataQualityFlags.length ? 'orange' : 'green',
      },
    ];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary, currentRecord, derivedMetrics, lifeStage, peerAnalysis, dataQualityFlags]);

  const sensorGauges = useMemo(() => {
    if (!currentRecord) return [];

    const columns = [...PRIMARY_GAUGES];
    for (const column of SECONDARY_GAUGES) {
      if (columns.length >= 8) break;
      if (currentRecord[column] != null) columns.push(column);
    }

    return columns
      .filter((column) => currentRecord[column] != null)
      .map((column) => {
        const meta = GAUGE_META[column];
        const value = currentRecord[column];
        const pct = Math.min((value / meta.max) * 100, 100);
        return { column, value, pct, band: findBand(column), ...meta };
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentRecord, bandAssessment]);

  const recommendationSummary = recommendation?.report || null;
  const validationWarnings = recommendationSummary?.validation_warnings || [];

  const workflowSteps = [
    {
      number: '1',
      title: 'Sensor intake',
      description: 'Upload the factory sensor export and select a machine to review.',
    },
    {
      number: '2',
      title: 'Peer comparison',
      description: "Compare the machine's readings against its type and the manual's bands.",
    },
    {
      number: '3',
      title: 'Action guidance',
      description: 'Receive AI-backed maintenance direction grounded in the manual.',
    },
  ];

  const getGaugeLevel = (band) => {
    if (band === 'CRITICAL') return 'level-danger';
    if (band === 'WARNING') return 'level-warn';
    return 'level-ok';
  };

  const getTrendArrow = (position) => {
    switch (position) {
      case 'ABOVE_PEER_AVERAGE': return '↑';
      case 'BELOW_PEER_AVERAGE': return '↓';
      case 'TYPICAL': return '→';
      default: return '·';
    }
  };

  const getTrendClass = (position) => (position || '').split('_')[0].toLowerCase() || 'typical';

  const getHealthBadgeClass = (healthStatus) => {
    switch (healthStatus?.toUpperCase()) {
      case 'HEALTHY': return 'ok';
      case 'MONITOR': return 'monitor';
      case 'MAINTENANCE_DUE': return 'maintenance';
      case 'CRITICAL': return 'critical';
      default: return 'monitor';
    }
  };

  const getRiskBadgeClass = (level) => {
    switch (level?.toUpperCase()) {
      case 'LOW': return 'risk-low';
      case 'MEDIUM': return 'risk-medium';
      case 'HIGH': return 'risk-high';
      case 'CRITICAL': return 'risk-critical';
      default: return 'risk-medium';
    }
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-glow" aria-hidden="true" />
        <div className="hero-copy">
          <p className="eyebrow">
            <span className="eyebrow-line" />
            Condition-based maintenance intelligence
            <span className="eyebrow-line" />
          </p>
          <h1>
            <span className="logo-mark" aria-hidden="true">
              <svg viewBox="0 0 36 36" width="36" height="36">
                <circle cx="18" cy="18" r="17" fill="none" stroke="url(#logoGrad)" strokeWidth="1.5" opacity="0.5" />
                <circle cx="18" cy="18" r="12" fill="none" stroke="url(#logoGrad)" strokeWidth="0.8" opacity="0.3" />
                <defs>
                  <linearGradient id="logoGrad" x1="0" y1="0" x2="36" y2="36">
                    <stop offset="0%" stopColor="#f2a93b" />
                    <stop offset="100%" stopColor="#a78bfa" />
                  </linearGradient>
                </defs>
                <text x="18" y="22" textAnchor="middle" fontSize="15" fill="url(#logoGrad)">⚙</text>
              </svg>
            </span>
            <span className="title-text">
              <span className="title-helix">Helix</span>
              <span className="title-control">Control</span>
            </span>
            <span className="title-divider" />
            <span className="title-suffix">Maintenance</span>
          </h1>
          <p className="hero-subtitle">
            From sensor reading to work order — every machine is reviewed with clarity and precision.
          </p>
          <div className="hero-tags">
            <span className="hero-tag">AI-Powered</span>
            <span className="hero-tag">Peer-Benchmarked</span>
            <span className="hero-tag">Manual-Grounded</span>
          </div>
        </div>
        <div className="status-pill">
          <span className="status-dot" />
          {status}
        </div>
      </header>

      <section className="workflow-strip" aria-label="Dashboard workflow">
        {workflowSteps.map((step) => (
          <div key={step.title} className="workflow-step">
            <div className="step-number">{step.number}</div>
            <h4>{step.title}</h4>
            <p>{step.description}</p>
          </div>
        ))}
      </section>

      <section className="line-status-card">
        <div className="track-copy">
          <p className="eyebrow">Live condition monitoring</p>
          <h3>
            {analyticsResult
              ? `Machine ${analyticsResult.machine_id} is queued for condition review.`
              : 'A machine is ready for condition review.'}
          </h3>
          <p>
            The sensor and peer-comparison loop is visualized here so the transition from raw readings to
            maintenance guidance feels immediate and operational.
          </p>
        </div>
        <div className="track-visual" aria-hidden="true">
          <svg viewBox="0 0 340 120" role="presentation">
            <defs>
              <linearGradient id="trackGradient" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" stopColor="#f2a93b" stopOpacity="0.3" />
                <stop offset="50%" stopColor="#e0692c" stopOpacity="0.7" />
                <stop offset="100%" stopColor="#7fb069" stopOpacity="0.3" />
              </linearGradient>
            </defs>

            {/* Assessment path */}
            <path d="M24 82 C70 20, 130 20, 170 55 S250 108, 316 32" className="track-arc" />

            <circle cx="24" cy="82" r="5" className="track-node feed" />
            <circle cx="170" cy="55" r="5" className="track-node active" />
            <circle cx="316" cy="32" r="5" className="track-node assessed" />

            <g className="gear-mount">
              <g className="gear-group">
                <circle r="14" className="gear-body" />
                <circle r="5" className="gear-hub" />
                {[0, 45, 90, 135, 180, 225, 270, 315].map((deg) => (
                  <rect key={deg} x="-2.4" y="-18" width="4.8" height="7" className="gear-tooth" transform={`rotate(${deg})`} />
                ))}
              </g>
            </g>
          </svg>
          <div className="track-labels">
            <span>Sensor Feed</span>
            <span>Peer Baseline</span>
            <span>Manual Assessment</span>
          </div>
        </div>
      </section>

      <div className="dashboard-grid">
        <aside className="sidebar">
          <PanelCard title="Sensor Data Upload" subtitle="Upload the factory sensor export and begin the engineering review.">
            <form onSubmit={handleSubmit} className="upload-form">
              <label className="file-field">
                <span>Sensor Data (.csv)</span>
                <input
                  type="file"
                  accept=".csv"
                  onChange={(event) => setCsvFile(event.target.files?.[0] || null)}
                />
              </label>

              <label className="text-field">
                <span>Machine ID (optional)</span>
                <input
                  type="text"
                  placeholder="e.g. MC_000123 — defaults to the first machine in the file"
                  value={machineIdInput}
                  onChange={(event) => setMachineIdInput(event.target.value)}
                />
              </label>

              <label className="text-field">
                <span>Compare against</span>
                <select value={peerScope} onChange={(event) => setPeerScope(event.target.value)}>
                  <option value="machine_type">Same machine type</option>
                  <option value="fleet">Whole fleet</option>
                </select>
              </label>

              <p className="helper-text compact">
                This stage compares the selected machine against its peers to reveal its current condition.
              </p>

              <button type="submit" className="primary-btn" disabled={isLoading}>
                {isLoading && loadingPhase === 'analytics' ? 'Analyzing sensor data...' : 'Generate Engineering Analytics'}
              </button>
            </form>
          </PanelCard>

          <PanelCard
            title="Maintenance Guidance"
            subtitle="Use the analytics output and the SF-2040 manual for action guidance."
            accent="ai"
          >
            <label className="file-field">
              <span>Maintenance Manual (.pdf)</span>
              <input
                type="file"
                accept=".pdf"
                onChange={(event) => setManualFile(event.target.files?.[0] || null)}
              />
            </label>

            <p className="helper-text compact">
              {manualFile ? `Manual loaded: ${manualFile.name}` : 'The manual is used to ground the AI recommendation in documented thresholds and procedures.'}
            </p>

            <button className="primary-btn wide ai-btn" onClick={handleRecommendation} disabled={isLoading || !analyticsResult}>
              {isLoading && loadingPhase === 'ai' ? '✨ AI is analyzing...' : '✨ Generate AI Recommendation'}
            </button>
          </PanelCard>
        </aside>

        <main className="main-content">
          <div className="metric-row">
            {metrics.map((metric) => (
              <div className={`metric-card ${metric.accent}`} key={metric.label}>
                <span className="metric-icon">{metric.icon}</span>
                <span className="metric-label">{metric.label}</span>
                <strong className="metric-value">{metric.value}</strong>
                <small className="metric-sub">{metric.sub}</small>
              </div>
            ))}
          </div>

          {error ? <div className="error-box">⚠ {error}</div> : null}

          <div className="split-view">
            {/* ─── Engineering Analytics ─── */}
            <PanelCard title="Engineering Analytics" subtitle="Machine condition derived from the uploaded sensor dataset">
              {isLoading && loadingPhase === 'analytics' ? (
                <div className="loading-overlay">
                  <div className="loading-spinner" />
                  <span className="loading-text">Processing sensor data...</span>
                </div>
              ) : summary ? (
                <div className="analytics-panel">
                  {/* Machine overview header */}
                  <div className="machine-header">
                    <div className="machine-avatar">⚙</div>
                    <div className="machine-info">
                      <h4>{summary.machine_id} — {summary.machine_type}</h4>
                      <div className="machine-meta">
                        <span>🏭 {summary.machine_family || 'N/A'}</span>
                        <span>⏱ {currentRecord?.Operational_Hours?.toLocaleString() ?? 'N/A'} h</span>
                        {currentRecord?.AI_Supervision ? <span>🤖 AI-supervised</span> : null}
                      </div>
                    </div>
                  </div>

                  {/* Sensor Gauges */}
                  <div className="gauge-grid">
                    {sensorGauges.map((gauge) => (
                      <div className="gauge-card" key={gauge.column}>
                        <span className="gauge-label">{gauge.label}</span>
                        <div className="gauge-value-row">
                          <span className="gauge-value">{gauge.value?.toLocaleString?.() ?? gauge.value}</span>
                          <span className="gauge-unit">{gauge.unit}</span>
                        </div>
                        <div className="gauge-bar-track">
                          <div className={`gauge-bar-fill ${getGaugeLevel(gauge.band)}`} style={{ width: `${gauge.pct}%` }} />
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* Peer Comparison */}
                  <div className="signal-list">
                    <h4>
                      Peer Comparison
                      <span className="signal-count">{peerAnalysis.length} parameters</span>
                    </h4>
                    <div className="signal-row" style={{ color: 'var(--text-dim)', fontSize: '0.72rem', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                      <span>Parameter</span>
                      <span style={{ textAlign: 'right' }}>Current</span>
                      <span style={{ textAlign: 'right' }}>vs Peers</span>
                      <span>Position</span>
                    </div>
                    {peerAnalysis.map((item) => (
                      <div key={item.column} className="signal-row">
                        <span className="signal-name">{item.column.replace(/_/g, ' ')}</span>
                        <span className="signal-value">{item.latest_value.toLocaleString()}</span>
                        <span className={`signal-change ${item.change_percent >= 0 ? 'positive' : 'negative'}`}>
                          {item.change_percent > 0 ? '+' : ''}{item.change_percent.toFixed(1)}%
                        </span>
                        <span className={`trend-badge ${getTrendClass(item.position_vs_peers)}`}>
                          <span className="trend-arrow">{getTrendArrow(item.position_vs_peers)}</span>
                          {item.position_vs_peers.replace(/_/g, ' ')}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <p className="empty-state">
                  <span className="empty-state-icon">📊</span>
                  Upload the sensor CSV and generate engineering analytics to view the machine health dashboard.
                </p>
              )}
            </PanelCard>

            {/* ─── AI Maintenance Recommendation ─── */}
            <PanelCard
              title="AI Maintenance Recommendation"
              subtitle="Intelligent guidance generated from analytics & the SF-2040 manual"
              accent="ai"
              badge={
                <span className="panel-badge ai-badge">
                  <span className="ai-sparkle">✨</span>
                  AI Powered
                </span>
              }
            >
              {isLoading && loadingPhase === 'ai' ? (
                <div className="loading-overlay">
                  <div className="loading-spinner" />
                  <span className="loading-text">AI is analyzing machine health...</span>
                </div>
              ) : recommendationSummary ? (
                <div className="recommendation-panel">
                  {/* AI Header */}
                  <div className="ai-header">
                    <div className="ai-brain-icon">🧠</div>
                    <div>
                      <span className="ai-label">AI Analysis</span>
                      <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)' }}>
                        {recommendationSummary.machine_id} · {recommendationSummary.machine_type}
                      </div>
                    </div>
                  </div>

                  {/* Health & Risk Status */}
                  <div className="status-banner">
                    <span className={`status-badge ${getHealthBadgeClass(recommendationSummary.health_status)}`}>
                      <span className="badge-dot" />
                      {recommendationSummary.health_status?.replace(/_/g, ' ')}
                    </span>
                    <span className={`status-badge ${getRiskBadgeClass(recommendationSummary.risk_level)}`}>
                      <span className="badge-dot" />
                      {recommendationSummary.risk_level} RISK
                    </span>
                    <span className="status-badge priority">
                      <span className="badge-dot" />
                      {recommendationSummary.priority}
                    </span>
                    <span className={`status-badge ${recommendationSummary.safe_to_continue_operation ? 'ok' : 'critical'}`}>
                      <span className="badge-dot" />
                      {recommendationSummary.safe_to_continue_operation ? 'CONTINUE OPERATION' : 'REMOVE FROM SERVICE'}
                    </span>
                  </div>

                  {validationWarnings.length > 0 && (
                    <div className="consistency-warning-box">
                      <h4>🔎 Consistency Check</h4>
                      <ul>
                        {validationWarnings.map((warning, i) => (
                          <li key={i}>{warning}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Overall Summary */}
                  {recommendationSummary.overall_summary && (
                    <div className="overall-summary">
                      <p>{recommendationSummary.overall_summary}</p>
                    </div>
                  )}

                  {/* Operating Decision */}
                  {recommendationSummary.final_operating_decision && (
                    <div className="operating-decision">
                      <div className="decision-header">
                        <div className={`decision-icon ${recommendationSummary.final_operating_decision.can_operate_now ? 'fly' : 'ground'}`}>
                          {recommendationSummary.final_operating_decision.can_operate_now ? '✅' : '🛑'}
                        </div>
                        <div>
                          <div className="decision-title">{recommendationSummary.final_operating_decision.decision?.replace(/_/g, ' ')}</div>
                          <div className="decision-subtitle">
                            {recommendationSummary.final_operating_decision.required_before_continued_operation}
                          </div>
                        </div>
                      </div>
                      <div className="decision-statement">
                        {recommendationSummary.final_operating_decision.ui_statement}
                      </div>
                      {recommendationSummary.final_operating_decision.target_response_time && (
                        <div className="decision-response-time">
                          ⏱ Target response: {recommendationSummary.final_operating_decision.target_response_time}
                        </div>
                      )}
                      {recommendationSummary.final_operating_decision.decision_rationale && (
                        <div className="decision-rationale">
                          💡 {recommendationSummary.final_operating_decision.decision_rationale}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Status Derivation */}
                  {recommendationSummary.status_derivation?.escalation_rules_applied?.length > 0 && (
                    <div className="derivation-card">
                      <h4>🧮 Status Derivation</h4>
                      <div className="derivation-rules">
                        {recommendationSummary.status_derivation.escalation_rules_applied.map((rule, i) => (
                          <span className="manual-ref" key={i}>📖 {rule.rule} — {rule.reason}</span>
                        ))}
                      </div>
                      {recommendationSummary.status_derivation.final_status_reason && (
                        <p className="derivation-reason">{recommendationSummary.status_derivation.final_status_reason}</p>
                      )}
                    </div>
                  )}

                  {/* Threshold Violations */}
                  {recommendationSummary.threshold_violations?.length > 0 && (
                    <div className="violations-section">
                      <h4>⚠ Threshold Violations</h4>
                      {recommendationSummary.threshold_violations.map((v, i) => (
                        <div className="violation-card" key={i}>
                          <div className="violation-header">
                            <span className="violation-param">{v.parameter?.replace(/_/g, ' ')}</span>
                            <span className="violation-severity">{v.severity}</span>
                          </div>
                          <div className="violation-values">
                            <div className="violation-val">
                              <label>Observed</label>
                              <span style={{ color: 'var(--red)' }}>{v.observed_value}</span>
                            </div>
                            <div className="violation-val">
                              <label>Threshold</label>
                              <span>{v.manual_threshold}</span>
                            </div>
                          </div>
                          {v.explanation && <div className="violation-explanation">{v.explanation}</div>}
                          {v.manual_reference && <span className="manual-ref">📖 {v.manual_reference}</span>}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Detected Failure Modes */}
                  {recommendationSummary.detected_failure_modes?.length > 0 && (
                    <div className="failure-modes-section">
                      <h4>🩻 Detected Failure Modes</h4>
                      {recommendationSummary.detected_failure_modes.map((f, i) => (
                        <div className="failure-mode-card" key={i}>
                          <div className="violation-header">
                            <span className="violation-param">{f.failure_mode}</span>
                            <span className="violation-severity">{f.severity}</span>
                          </div>
                          {f.evidence?.length > 0 && (
                            <ul className="evidence-list">
                              {f.evidence.map((e, j) => (
                                <li key={j}>{e}</li>
                              ))}
                            </ul>
                          )}
                          {f.manual_reference && <span className="manual-ref">📖 {f.manual_reference}</span>}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Root Cause Analysis */}
                  {recommendationSummary.root_cause && (
                    <div className="root-cause-card">
                      <h4>🔍 Root Cause Analysis</h4>
                      <div className="cause-title">{recommendationSummary.root_cause.most_likely_cause}</div>
                      <ul className="evidence-list">
                        {(recommendationSummary.root_cause.supporting_evidence || []).map((e, i) => (
                          <li key={i}>{e}</li>
                        ))}
                      </ul>
                      {recommendationSummary.root_cause.manual_reference && (
                        <span className="manual-ref">📖 {recommendationSummary.root_cause.manual_reference}</span>
                      )}
                    </div>
                  )}

                  {/* Maintenance Actions */}
                  {recommendationSummary.maintenance_actions?.length > 0 && (
                    <div className="actions-list">
                      <h4>🔧 Maintenance Actions</h4>
                      {recommendationSummary.maintenance_actions.map((action, i) => (
                        <div className="action-item" key={i}>
                          <div className="action-priority">P{action.priority}</div>
                          <div className="action-content">
                            <div className="action-text">{action.action}</div>
                            <div className="action-reason">{action.reason}</div>
                            {action.manual_reference && <span className="manual-ref">📖 {action.manual_reference}</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Inspection Checklist */}
                  {recommendationSummary.inspection_checklist?.length > 0 && (
                    <div className="checklist-section">
                      <h4>📋 Inspection Checklist</h4>
                      {recommendationSummary.inspection_checklist.map((item, i) => (
                        <div className="checklist-item" key={i}>
                          <div className="checklist-step">{item.step}</div>
                          <div className="checklist-content">
                            <div className="check-title">{item.inspection_item}</div>
                            <div className="check-criteria">✓ {item.acceptance_criteria}</div>
                            {item.manual_reference && <span className="manual-ref">📖 {item.manual_reference}</span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Data Quality Notes */}
                  {recommendationSummary.data_quality_notes?.length > 0 && (
                    <div className="data-quality-box">
                      <h4>🧪 Data Quality Notes</h4>
                      <ul>
                        {recommendationSummary.data_quality_notes.map((note, i) => (
                          <li key={i}>{note}</li>
                        ))}
                      </ul>
                    </div>
                  )}

                  {/* Confidence & Work Order Row */}
                  <div className="info-row">
                    <div className="info-card">
                      <span className="info-label">AI Confidence</span>
                      <span className="info-value" style={{ color: 'var(--purple)' }}>
                        {recommendationSummary.confidence?.score
                          ? `${(recommendationSummary.confidence.score * 100).toFixed(0)}%`
                          : 'N/A'}
                      </span>
                      <div className="confidence-bar-track">
                        <div
                          className="confidence-bar-fill"
                          style={{ width: `${(recommendationSummary.confidence?.score || 0) * 100}%` }}
                        />
                      </div>
                      {recommendationSummary.confidence?.rationale && (
                        <span className="info-sub">{recommendationSummary.confidence.rationale}</span>
                      )}
                    </div>
                    <div className="info-card">
                      <span className="info-label">Work Order Code</span>
                      <span className="info-value" style={{ color: 'var(--sky)' }}>
                        {recommendationSummary.work_order?.work_order_code || 'N/A'}
                      </span>
                      <span className="info-sub">
                        Priority: {recommendationSummary.work_order?.priority || 'N/A'}
                      </span>
                      <span className="info-sub">
                        {recommendationSummary.work_order?.estimated_maintenance_category || ''}
                      </span>
                    </div>
                  </div>

                  {/* Work Order Details */}
                  {recommendationSummary.work_order && (
                    <div className="work-order-card">
                      <h4>📝 Work Order — {recommendationSummary.work_order.title}</h4>
                      <div className="wo-meta">
                        <div className="wo-meta-item">
                          <label>Machine</label>
                          <span>{recommendationSummary.work_order.machine_id}</span>
                        </div>
                        <div className="wo-meta-item">
                          <label>Target Completion</label>
                          <span>{recommendationSummary.work_order.target_completion}</span>
                        </div>
                      </div>

                      {recommendationSummary.work_order.tasks?.length > 0 && (
                        <ul className="wo-tasks">
                          {recommendationSummary.work_order.tasks.map((task, i) => (
                            <li key={i}>{task}</li>
                          ))}
                        </ul>
                      )}

                      {recommendationSummary.work_order.required_parts_or_tools?.length > 0 && (
                        <div className="wo-parts">
                          {recommendationSummary.work_order.required_parts_or_tools.map((part, i) => (
                            <span className="wo-part-tag" key={i}>{part}</span>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Missing Information */}
                  {recommendationSummary.confidence?.missing_information?.length > 0 && (
                    <div className="summary-box" style={{ borderColor: 'rgba(251, 191, 36, 0.15)' }}>
                      <h4 style={{ color: 'var(--amber)' }}>⚡ Missing Information</h4>
                      <ul style={{ listStyle: 'none', padding: 0, margin: '4px 0 0' }}>
                        {recommendationSummary.confidence.missing_information.map((info, i) => (
                          <li key={i} style={{ fontSize: '0.82rem', color: 'var(--text-muted)', padding: '3px 0', paddingLeft: '14px', position: 'relative' }}>
                            <span style={{ position: 'absolute', left: 0, color: 'var(--amber)' }}>·</span>
                            {info}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ) : (
                <p className="empty-state">
                  <span className="empty-state-icon">🧠</span>
                  Generate the AI recommendation to view the intelligent maintenance decision board.
                </p>
              )}
            </PanelCard>
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;