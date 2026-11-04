import { useEffect, useState, type ReactElement, type ReactNode } from 'react';
import * as React from 'react';
import { ApiError, type RunMetrics, type ScoringRun } from '../services/api';
import { ChatPanel } from '../components/ChatPanel';
import {
  useCreateScoringRun,
  useRunMetrics,
  useScoringRun,
  useScoringRuns,
} from '../hooks/use-scoring-run';

const dashboardStyles = `
  :root {
    color-scheme: light;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    color: #172b4d;
    background: #f4f7fb;
    font-synthesis: none;
    text-rendering: optimizeLegibility;
  }
  * { box-sizing: border-box; }
  body { min-width: 320px; margin: 0; }
  button, select, textarea { font: inherit; }
  button:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: 3px solid rgba(52, 117, 255, .35);
    outline-offset: 2px;
  }
  .ra-app { min-height: 100vh; background: #f4f7fb; }
  .ra-topbar {
    display: flex; align-items: center; justify-content: space-between; gap: 24px;
    min-height: 76px; padding: 0 clamp(20px, 4vw, 56px); border-bottom: 1px solid #e2e9f2;
    background: rgba(255, 255, 255, .88);
  }
  .ra-brand { display: flex; align-items: center; gap: 12px; color: #182e50; font-size: 14px; font-weight: 750; letter-spacing: -.02em; }
  .ra-brand-mark {
    display: grid; width: 38px; height: 38px; place-items: center; border-radius: 12px;
    background: linear-gradient(145deg, #143d72, #237dc4); color: #fff; font-size: 14px; letter-spacing: -.05em;
    box-shadow: 0 6px 14px rgba(30, 89, 150, .2);
  }
  .ra-topbar-right { display: flex; align-items: center; gap: 18px; }
  .ra-route-status { display: inline-flex; align-items: center; gap: 8px; color: #61738d; font-size: 12px; font-weight: 650; }
  .ra-online-dot { width: 8px; height: 8px; border-radius: 50%; background: #16a66a; box-shadow: 0 0 0 4px #e5f6ef; }
  .ra-user-avatar {
    display: grid; width: 36px; height: 36px; place-items: center; border: 1px solid #dce5ef;
    border-radius: 50%; background: #edf3fb; color: #385678; font-size: 12px; font-weight: 750;
  }
  .ra-content { width: min(1440px, 100%); margin: 0 auto; padding: 42px clamp(20px, 4vw, 56px) 64px; }
  .ra-page-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 30px; }
  .ra-eyebrow { margin: 0 0 8px; color: #52749d; font-size: 10px; font-weight: 800; letter-spacing: .13em; text-transform: uppercase; }
  .ra-page-heading h1 { margin: 0; color: #142b4c; font-size: clamp(28px, 3.4vw, 42px); font-weight: 740; letter-spacing: -.045em; line-height: 1.08; }
  .ra-page-heading p:not(.ra-eyebrow) { max-width: 670px; margin: 12px 0 0; color: #657993; font-size: 14px; line-height: 1.6; }
  .ra-heading-note { display: flex; align-items: center; gap: 9px; padding: 11px 14px; border: 1px solid #dbe6f2; border-radius: 12px; background: #fff; color: #536b88; font-size: 12px; white-space: nowrap; }
  .ra-heading-note-mark { color: #217ac3; font-size: 16px; }
  .ra-controls { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 20px; padding: 16px 18px; border: 1px solid #e1e8f0; border-radius: 16px; background: #fff; box-shadow: 0 7px 22px rgba(30, 55, 88, .035); }
  .ra-run-control { display: flex; align-items: center; gap: 14px; min-width: 0; }
  .ra-run-control-label { display: flex; flex-direction: column; gap: 4px; color: #8492a6; font-size: 10px; font-weight: 750; letter-spacing: .08em; text-transform: uppercase; }
  .ra-run-control select { min-width: 255px; max-width: min(56vw, 440px); padding: 10px 34px 10px 12px; border: 1px solid #dce5ef; border-radius: 10px; background: #fff; color: #213958; font-size: 13px; font-weight: 650; }
  .ra-controls-right { display: flex; align-items: center; justify-content: flex-end; gap: 12px; }
  .ra-selected-meta { color: #7a8ca3; font-size: 11px; }
  .ra-button { display: inline-flex; align-items: center; justify-content: center; gap: 9px; min-height: 40px; padding: 0 15px; border: 1px solid transparent; border-radius: 10px; cursor: pointer; font-size: 12px; font-weight: 750; transition: transform .15s ease, box-shadow .15s ease, background .15s ease; }
  .ra-button:hover:not(:disabled) { transform: translateY(-1px); }
  .ra-button:disabled { cursor: not-allowed; opacity: .56; }
  .ra-button-primary { background: #196fb4; color: #fff; box-shadow: 0 5px 12px rgba(25, 111, 180, .2); }
  .ra-button-primary:hover:not(:disabled) { background: #145f9d; box-shadow: 0 7px 16px rgba(25, 111, 180, .25); }
  .ra-button-quiet { border-color: #dce5ef; background: #fff; color: #49627f; }
  .ra-button-quiet:hover:not(:disabled) { background: #f7faff; }
  .ra-button-send { min-height: 36px; background: #183e6a; color: #fff; }
  .ra-button-send:hover:not(:disabled) { background: #235483; }
  .ra-run-status { display: inline-flex; align-items: center; gap: 7px; padding: 6px 9px; border-radius: 99px; background: #edf2f7; color: #52647a; font-size: 10px; font-weight: 800; letter-spacing: .035em; text-transform: uppercase; white-space: nowrap; }
  .ra-run-status::before { width: 6px; height: 6px; border-radius: 50%; background: currentColor; content: ""; }
  .ra-status-running, .ra-status-queued { background: #fff3da; color: #986517; }
  .ra-status-completed { background: #e7f6ef; color: #16805a; }
  .ra-status-failed { background: #fff0ef; color: #bc4f48; }
  .ra-alert { margin: 0 0 18px; padding: 12px 15px; border: 1px solid #f0c8c5; border-radius: 11px; background: #fff5f4; color: #8a3e39; font-size: 12px; line-height: 1.5; }
  .ra-progress-card { margin-bottom: 20px; padding: 18px 20px; border: 1px solid #e1e8f0; border-radius: 16px; background: #fff; }
  .ra-progress-top { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 15px; }
  .ra-progress-top h2 { margin: 0; color: #283f5c; font-size: 13px; }
  .ra-progress-top span { color: #8291a5; font-size: 11px; }
  .ra-progress-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
  .ra-progress-item { min-width: 0; }
  .ra-progress-label { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 7px; color: #566d88; font-size: 11px; }
  .ra-progress-label strong { color: #273e5c; font-size: 11px; }
  .ra-progress-track { height: 6px; overflow: hidden; border-radius: 99px; background: #eaf0f6; }
  .ra-progress-fill { height: 100%; border-radius: inherit; background: linear-gradient(90deg, #307bb6, #53b4c5); transition: width .35s ease; }
  .ra-metrics-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-bottom: 20px; }
  .ra-metric-card, .ra-card { min-width: 0; border: 1px solid #e1e8f0; border-radius: 16px; background: #fff; box-shadow: 0 7px 22px rgba(30, 55, 88, .035); }
  .ra-metric-card { padding: 19px 20px 18px; }
  .ra-metric-card-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
  .ra-metric-card h2 { margin: 0; color: #263c58; font-size: 13px; font-weight: 750; }
  .ra-metric-icon { display: grid; width: 35px; height: 35px; place-items: center; border-radius: 11px; background: #edf5fc; color: #2876b5; font-size: 16px; font-weight: 750; }
  .ra-metric-card:nth-child(2) .ra-metric-icon { background: #fff4e9; color: #bd7028; }
  .ra-metric-card:nth-child(3) .ra-metric-icon { background: #eaf7f4; color: #238d7c; }
  .ra-metric-primary { margin: 18px 0 4px; color: #172e4d; font-size: 30px; font-weight: 760; letter-spacing: -.045em; line-height: 1; }
  .ra-metric-subtitle { color: #8090a5; font-size: 11px; }
  .ra-metric-divider { height: 1px; margin: 17px 0 13px; background: #edf1f5; }
  .ra-metric-detail-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; color: #8492a5; font-size: 10px; font-weight: 750; letter-spacing: .065em; text-transform: uppercase; }
  .ra-class-list { display: grid; gap: 8px; }
  .ra-class-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; color: #556b85; font-size: 11px; }
  .ra-class-label { display: inline-flex; align-items: center; gap: 7px; }
  .ra-class-dot { width: 7px; height: 7px; border-radius: 2px; background: #5595cf; }
  .ra-class-row:nth-child(2) .ra-class-dot { background: #77b5c5; }
  .ra-class-row:nth-child(3) .ra-class-dot { background: #9a9ed6; }
  .ra-class-value { color: #304865; font-weight: 700; white-space: nowrap; }
  .ra-metric-foot { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 14px; color: #71849b; font-size: 10px; }
  .ra-metric-foot strong { color: #38516e; font-weight: 750; }
  .ra-empty-detail { color: #8a99ab; font-size: 11px; }
  .ra-workspace-grid { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(330px, .9fr); align-items: stretch; gap: 16px; }
  .ra-run-summary { padding: 22px; }
  .ra-card-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
  .ra-card-heading h2 { margin: 0; color: #243b59; font-size: 16px; letter-spacing: -.02em; }
  .ra-card-heading .ra-eyebrow { margin-bottom: 6px; }
  .ra-run-summary-text { max-width: 580px; margin: 13px 0 20px; color: #70839a; font-size: 12px; line-height: 1.65; }
  .ra-run-metadata { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; padding-top: 16px; border-top: 1px solid #edf1f5; }
  .ra-run-metadata-item { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .ra-run-metadata-item span { color: #8593a6; font-size: 10px; }
  .ra-run-metadata-item strong { overflow: hidden; color: #3a506b; font-size: 11px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
  .ra-run-empty { display: grid; min-height: 180px; place-items: center; padding: 26px; text-align: center; }
  .ra-run-empty-mark { display: grid; width: 42px; height: 42px; place-items: center; margin-bottom: 9px; border-radius: 13px; background: #edf5fc; color: #2876b5; font-size: 20px; }
  .ra-run-empty h2 { margin: 0; color: #2a405d; font-size: 14px; }
  .ra-run-empty p { max-width: 330px; margin: 8px 0 0; color: #788ba2; font-size: 11px; line-height: 1.5; }
  .ra-chat-card { display: flex; min-height: 316px; flex-direction: column; padding: 20px; }
  .ra-chat-mark { display: grid; width: 34px; height: 34px; place-items: center; border-radius: 10px; background: #edf4fb; color: #2b78b8; font-size: 18px; }
  .ra-chat-description { margin: 11px 0 14px; color: #788ba2; font-size: 11px; line-height: 1.55; }
  .ra-chat-transcript { display: flex; min-height: 82px; max-height: 260px; flex: 1; flex-direction: column; overflow: auto; margin: 0 -4px 14px; padding: 0 4px; }
  .ra-chat-empty { display: flex; flex: 1; flex-direction: column; align-items: flex-start; justify-content: center; padding: 14px 0; }
  .ra-chat-empty-icon { display: grid; width: 28px; height: 28px; place-items: center; margin-bottom: 10px; border-radius: 9px; background: #f0f5fa; color: #6684a4; font-size: 14px; }
  .ra-chat-empty p { max-width: 270px; margin: 0 0 6px; color: #455e7b; font-size: 12px; font-weight: 700; line-height: 1.45; }
  .ra-chat-empty span:last-child { color: #8998aa; font-size: 10px; line-height: 1.5; }
  .ra-chat-messages { display: grid; gap: 13px; margin: 0; padding: 0; list-style: none; }
  .ra-message { display: grid; max-width: 94%; gap: 5px; }
  .ra-message-user { justify-self: end; text-align: right; }
  .ra-message-label { color: #8997a9; font-size: 9px; font-weight: 750; letter-spacing: .05em; text-transform: uppercase; }
  .ra-message-text, .ra-message-pending { margin: 0; padding: 9px 11px; border-radius: 11px 11px 11px 3px; background: #f1f5f9; color: #3d536d; font-size: 11px; line-height: 1.55; text-align: left; white-space: pre-wrap; overflow-wrap: anywhere; }
  .ra-message-user .ra-message-text { border-radius: 11px 11px 3px 11px; background: #e9f3fc; color: #2b4a6b; }
  .ra-message-pending { display: flex; align-items: center; gap: 8px; color: #627892; }
  .ra-pulse { width: 7px; height: 7px; border-radius: 50%; background: #3187bf; animation: ra-pulse 1.1s ease-in-out infinite; }
  .ra-message-error { display: grid; gap: 5px; padding: 9px 11px; border: 1px solid #f1d2cf; border-radius: 11px 11px 11px 3px; background: #fff6f5; color: #88413c; font-size: 10px; line-height: 1.5; text-align: left; }
  .ra-message-error strong { font-size: 9px; letter-spacing: .04em; }
  .ra-chat-form { padding-top: 12px; border-top: 1px solid #edf1f5; }
  .ra-chat-form textarea { display: block; width: 100%; min-height: 66px; resize: vertical; padding: 10px 11px; border: 1px solid #dce5ef; border-radius: 10px; background: #fbfcfe; color: #334b68; font-size: 11px; line-height: 1.5; }
  .ra-chat-form textarea::placeholder { color: #9aa7b7; }
  .ra-chat-form-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; }
  .ra-chat-form-footer > span { color: #91a0b0; font-size: 9px; }
  .ra-visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; clip-path: inset(50%); }
  .ra-loading { color: #7e8da0; font-size: 12px; }
  .ra-footer-note { margin: 22px 0 0; color: #9aa6b5; font-size: 10px; text-align: right; }
  @keyframes ra-pulse { 0%, 100% { opacity: .35; transform: scale(.85); } 50% { opacity: 1; transform: scale(1.15); } }
  @media (max-width: 1000px) {
    .ra-workspace-grid { grid-template-columns: 1fr; }
    .ra-chat-card { min-height: 300px; }
  }
  @media (max-width: 760px) {
    .ra-content { padding-top: 30px; }
    .ra-page-heading { align-items: flex-start; flex-direction: column; }
    .ra-heading-note { display: none; }
    .ra-controls { align-items: stretch; flex-direction: column; }
    .ra-run-control { align-items: stretch; flex-direction: column; gap: 8px; }
    .ra-run-control select { width: 100%; max-width: none; }
    .ra-controls-right { justify-content: space-between; }
    .ra-metrics-grid { grid-template-columns: 1fr; }
    .ra-progress-grid { grid-template-columns: 1fr; gap: 13px; }
    .ra-run-metadata { grid-template-columns: 1fr 1fr; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
  }
`;

function formatInteger(value: number): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value);
}

function formatDecimal(value: number, fractionDigits = 1): string {
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits: fractionDigits,
    minimumFractionDigits: fractionDigits,
  }).format(value);
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return 'Not started';
  }
  return new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(
    new Date(value),
  );
}

function displayError(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.code}: ${error.message}`;
  }
  return 'The request could not be completed. Refresh and try again.';
}

function statusClass(status: ScoringRun['status']): string {
  return `ra-run-status ra-status-${status}`;
}

function ProgressItem({
  label,
  progress,
}: {
  label: string;
  progress: ScoringRun['progress']['churn'];
}): ReactElement {
  const percentage =
    progress.total_rows === 0
      ? 0
      : Math.min(100, Math.round((progress.processed_rows / progress.total_rows) * 100));
  return (
    <div className="ra-progress-item">
      <div className="ra-progress-label">
        <span>
          {label} · {progress.status}
        </span>
        <strong>
          {formatInteger(progress.processed_rows)} / {formatInteger(progress.total_rows)}
        </strong>
      </div>
      <div
        className="ra-progress-track"
        aria-label={`${label} progress`}
        role="progressbar"
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={percentage}
      >
        <div className="ra-progress-fill" style={{ width: `${percentage}%` }} />
      </div>
    </div>
  );
}

function MetricCard({
  title,
  icon,
  value,
  caption,
  children,
}: {
  title: string;
  icon: string;
  value: string;
  caption: string;
  children: ReactNode;
}): ReactElement {
  return (
    <section className="ra-metric-card" aria-label={`${title} metrics`}>
      <div className="ra-metric-card-top">
        <div>
          <p className="ra-eyebrow">Model signal</p>
          <h2>{title}</h2>
        </div>
        <span className="ra-metric-icon" aria-hidden="true">
          {icon}
        </span>
      </div>
      <p className="ra-metric-primary">{value}</p>
      <span className="ra-metric-subtitle">{caption}</span>
      <div className="ra-metric-divider" />
      {children}
    </section>
  );
}

function ClassRows({
  classes,
  showDollarWeight,
}: {
  classes: RunMetrics['churn']['classes'];
  showDollarWeight: boolean;
}): ReactElement {
  if (classes.length === 0) {
    return <p className="ra-empty-detail">No predictions have been committed yet.</p>;
  }
  return (
    <div className="ra-class-list">
      {classes.map((item) => (
        <div className="ra-class-row" key={item.class_id}>
          <span className="ra-class-label">
            <i className="ra-class-dot" aria-hidden="true" />
            Class {item.class_id}
          </span>
          <span className="ra-class-value">
            {formatInteger(item.prediction_count)} predictions
            {showDollarWeight && item.dollar_weight_sum !== null
              ? ` · ${formatDecimal(item.dollar_weight_sum, 0)} dollar weight`
              : ''}
          </span>
        </div>
      ))}
    </div>
  );
}

function MetricsGrid({ metrics }: { metrics: RunMetrics | undefined }): ReactElement {
  if (!metrics) {
    return (
      <div className="ra-metrics-grid" aria-label="Selected run metrics">
        {(['Churn propensity', 'Transaction fraud', 'Service quality'] as const).map((title) => (
          <section className="ra-metric-card" key={title} aria-label={`${title} metrics`}>
            <p className="ra-eyebrow">Model signal</p>
            <h2>{title}</h2>
            <p className="ra-metric-primary">—</p>
            <span className="ra-metric-subtitle">Select a scoring run to view results</span>
          </section>
        ))}
      </div>
    );
  }

  return (
    <div className="ra-metrics-grid" aria-label="Selected run metrics">
      <MetricCard
        title="Churn propensity"
        icon="↗"
        value={formatInteger(metrics.churn.customer_count)}
        caption="Customers scored"
      >
        <div className="ra-metric-detail-heading">
          <span>Raw artifact classes</span>
          <span>Count · dollar weight</span>
        </div>
        <ClassRows classes={metrics.churn.classes} showDollarWeight />
      </MetricCard>
      <MetricCard
        title="Transaction fraud"
        icon="◇"
        value={formatInteger(metrics.fraud.fraud_count)}
        caption={`Class 1 fraud · ${formatInteger(metrics.fraud.transaction_count)} transactions scored`}
      >
        <div className="ra-metric-detail-heading">
          <span>Artifact classes</span>
          <span>Predictions</span>
        </div>
        <ClassRows classes={metrics.fraud.classes} showDollarWeight={false} />
      </MetricCard>
      <MetricCard
        title="Service quality"
        icon="⌁"
        value={metrics.qoe.mean_score === null ? '—' : formatDecimal(metrics.qoe.mean_score)}
        caption="Mean QoE score · 0–100"
      >
        <div className="ra-metric-detail-heading">
          <span>Score range</span>
          <span>Customers scored</span>
        </div>
        <div className="ra-metric-foot">
          <span>
            Minimum{' '}
            <strong>
              {metrics.qoe.minimum_score === null ? '—' : formatDecimal(metrics.qoe.minimum_score)}
            </strong>
          </span>
          <span>
            Maximum{' '}
            <strong>
              {metrics.qoe.maximum_score === null ? '—' : formatDecimal(metrics.qoe.maximum_score)}
            </strong>
          </span>
          <span>
            Customers <strong>{formatInteger(metrics.qoe.customer_count)}</strong>
          </span>
        </div>
      </MetricCard>
    </div>
  );
}

function RunSummary({ run }: { run: ScoringRun | undefined }): ReactElement {
  if (!run) {
    return (
      <section className="ra-card ra-run-empty" aria-label="Scoring run summary">
        <div>
          <span className="ra-run-empty-mark" aria-hidden="true">
            ◷
          </span>
          <h2>Choose a scoring run</h2>
          <p>Your selected run’s status, progress, and model results will appear here.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="ra-card ra-run-summary" aria-label="Scoring run summary">
      <div className="ra-card-heading">
        <div>
          <p className="ra-eyebrow">Run overview</p>
          <h2>Scoring progress</h2>
        </div>
        <span className={statusClass(run.status)}>{run.status}</span>
      </div>
      <p className="ra-run-summary-text">
        This run scores churn propensity, transaction fraud, and service quality against the
        configured source snapshot. Results are persisted as each model batch completes.
      </p>
      <div className="ra-run-metadata">
        <div className="ra-run-metadata-item">
          <span>Run ID</span>
          <strong title={run.run_id}>{run.run_id}</strong>
        </div>
        <div className="ra-run-metadata-item">
          <span>Started</span>
          <strong>{formatDate(run.started_at)}</strong>
        </div>
        <div className="ra-run-metadata-item">
          <span>Completed</span>
          <strong>{formatDate(run.completed_at)}</strong>
        </div>
      </div>
      {run.error_code ? (
        <p className="ra-alert" role="status">
          Run finished with {run.error_code}. Review run logs for the operational cause.
        </p>
      ) : null}
    </section>
  );
}

export function Dashboard(): ReactElement {
  const runsQuery = useScoringRuns();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const runStatusQuery = useScoringRun(selectedRunId);
  const metricsQuery = useRunMetrics(selectedRunId);
  const createRunMutation = useCreateScoringRun();
  const [startError, setStartError] = useState<string | null>(null);
  const runItems = runsQuery.data?.items;
  const runs = runItems ?? [];
  const selectedRunFromList = runs.find((run) => run.run_id === selectedRunId);
  const selectedRun = runStatusQuery.data ?? selectedRunFromList;

  useEffect(() => {
    if (selectedRunId === null && runItems && runItems.length > 0) {
      setSelectedRunId(runItems[0]?.run_id ?? null);
    }
  }, [runItems, selectedRunId]);

  async function handleStartRun(): Promise<void> {
    setStartError(null);
    try {
      const accepted = await createRunMutation.mutateAsync();
      setSelectedRunId(accepted.run_id);
    } catch (error: unknown) {
      setStartError(displayError(error));
    }
  }

  function handleRefresh(): void {
    void runsQuery.refetch();
    if (selectedRunId !== null) {
      void runStatusQuery.refetch();
      void metricsQuery.refetch();
    }
  }

  const currentStatus = selectedRun?.status ?? 'queued';

  return (
    <React.Fragment>
      <div className="ra-app">
        <style>{dashboardStyles}</style>
        <header className="ra-topbar">
          <div className="ra-brand">
            <span className="ra-brand-mark" aria-hidden="true">
              RA
            </span>
            <span>Telco intelligence</span>
          </div>
          <div className="ra-topbar-right">
            <span className="ra-route-status">
              <i className="ra-online-dot" aria-hidden="true" />
              Protected workspace
            </span>
            <span className="ra-user-avatar" aria-label="Analyst workspace">
              AN
            </span>
          </div>
        </header>

        <main className="ra-content">
          <section className="ra-page-heading">
            <div>
              <p className="ra-eyebrow">Revenue assurance · analyst workspace</p>
              <h1>Signals behind every subscriber</h1>
              <p>
                Run the latest scoring models, review persisted outcomes, and ask questions across
                churn, fraud, and service quality.
              </p>
            </div>
            <div className="ra-heading-note">
              <span className="ra-heading-note-mark" aria-hidden="true">
                ✦
              </span>{' '}
              Three model signals · one operational view
            </div>
          </section>

          <section className="ra-controls" aria-label="Scoring run controls">
            <div className="ra-run-control">
              <label className="ra-run-control-label" htmlFor="selected-run">
                Scoring run
                <select
                  id="selected-run"
                  aria-label="Select scoring run"
                  disabled={runs.length === 0}
                  onChange={(event) => setSelectedRunId(event.currentTarget.value || null)}
                  value={selectedRunId ?? ''}
                >
                  {runs.length === 0 ? <option value="">No scoring runs yet</option> : null}
                  {runs.map((run) => (
                    <option key={run.run_id} value={run.run_id}>
                      {formatDate(run.created_at)} · {run.status} · {run.run_id.slice(0, 8)}
                    </option>
                  ))}
                  {selectedRunId && !runs.some((run) => run.run_id === selectedRunId) ? (
                    <option value={selectedRunId}>New run · {selectedRunId.slice(0, 8)}</option>
                  ) : null}
                </select>
              </label>
              <span className={statusClass(currentStatus)}>
                {selectedRun ? currentStatus : 'no run'}
              </span>
            </div>
            <div className="ra-controls-right">
              <span className="ra-selected-meta">
                {runsQuery.isPending
                  ? 'Loading recent runs…'
                  : `${runs.length} recent ${runs.length === 1 ? 'run' : 'runs'}`}
              </span>
              <button
                className="ra-button ra-button-quiet"
                onClick={handleRefresh}
                type="button"
                aria-label="Refresh scoring data"
              >
                <span aria-hidden="true">↻</span> Refresh
              </button>
              <button
                className="ra-button ra-button-primary"
                disabled={createRunMutation.isPending}
                onClick={() => void handleStartRun()}
                type="button"
              >
                <span aria-hidden="true">＋</span>
                {createRunMutation.isPending ? 'Queueing…' : 'Run scoring'}
              </button>
            </div>
          </section>

          {runsQuery.isError ? (
            <p className="ra-alert" role="alert">
              Could not load scoring runs. {displayError(runsQuery.error)}
            </p>
          ) : null}
          {startError ? (
            <p className="ra-alert" role="alert">
              Could not start scoring. {startError}
            </p>
          ) : null}
          {runStatusQuery.isError ? (
            <p className="ra-alert" role="alert">
              Could not load selected run status. {displayError(runStatusQuery.error)}
            </p>
          ) : null}
          {metricsQuery.isError ? (
            <p className="ra-alert" role="alert">
              Could not load selected run metrics. {displayError(metricsQuery.error)}
            </p>
          ) : null}

          {selectedRun ? (
            <section className="ra-progress-card" aria-label="Selected run model progress">
              <div className="ra-progress-top">
                <h2>Model execution</h2>
                <span>
                  {metricsQuery.data
                    ? `Metrics as of ${formatDate(metricsQuery.data.as_of)}`
                    : metricsQuery.isPending
                      ? 'Loading run metrics…'
                      : 'Persisted progress'}
                </span>
              </div>
              <div className="ra-progress-grid">
                <ProgressItem label="Churn" progress={selectedRun.progress.churn} />
                <ProgressItem label="Fraud" progress={selectedRun.progress.fraud} />
                <ProgressItem label="QoE" progress={selectedRun.progress.qoe} />
              </div>
            </section>
          ) : null}

          <MetricsGrid metrics={metricsQuery.data} />

          <section className="ra-workspace-grid" aria-label="Analyst workspace">
            <RunSummary run={selectedRun} />
            <ChatPanel />
          </section>
          <p className="ra-footer-note">
            Metrics reflect persisted predictions for the selected run. Artifact class IDs are shown
            without semantic labels.
          </p>
        </main>
      </div>
    </React.Fragment>
  );
}
