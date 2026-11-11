# Telco Revenue Assurance Dashboard

This is a frontend-only React dashboard. It renders a static sample dataset in the browser and does not call an API, authenticate users, run predictions, or stream chat responses.

## Run locally

```sh
npm install
npm run dev
```

Open the URL Vite prints, normally `http://localhost:5173/`.

## Verify

```sh
npm run check
npm run e2e
```

The check command runs ESLint, TypeScript, Prettier, and Vitest. Playwright starts Vite automatically for the E2E test.

## Layout

- Header identifies the dashboard, sample period, and update time.
- Four metric cards summarize revenue at risk, churn, fraud exposure, and QoE.
- The primary panel contrasts revenue with at-risk revenue over six months.
- The side panel shows the Good/Fair/Poor QoE distribution.
- The regional table provides the detailed breakdown.
- The floating assistant is a dummy interface: it records questions and returns a placeholder until a backend is connected.

All values are defined in `src/data/dashboard.ts`. Replace that module with real application state when the backend is added later.

## Deployment

The app builds to static files served by unprivileged NGINX. The Helm chart creates a Deployment, Service, and optional OpenShift Route. It has no backend dependency or API proxy.

Build the image:

```sh
docker build -t quay.io/your-org/telco-revenue-assurance-dashboard:0.1.0 .
docker push quay.io/your-org/telco-revenue-assurance-dashboard:0.1.0
```

Install or upgrade:

```sh
helm upgrade --install revenue-assurance-dashboard helm/revenue-assurance-dashboard \
  --namespace telco-revenue \
  --set image.repository=quay.io/your-org/telco-revenue-assurance-dashboard \
  --set image.tag=0.1.0 \
  --set route.host=dashboard.apps.example.com
```

For the existing ptnr2 static-asset release, build the bundle and create dated ConfigMaps before upgrading with `values-showcase.yaml`. The checked-in values intentionally reference the immutable ConfigMaps currently used by that release.
