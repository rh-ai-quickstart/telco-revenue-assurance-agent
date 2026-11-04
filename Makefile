SHELL := /bin/bash

UV ?= uv
PNPM ?= pnpm
HELM ?= helm
OC ?= oc
TRAINING_RELEASE_NAME ?= telco-training
MODEL_STORAGE_REPORT ?= .qs-factory/pipeline/model-storage-permissions.json
CONTAINER_ENGINE ?= podman
NAMESPACE ?= telco-revenue-assurance-agent
RELEASE_NAME ?= telco-revenue-assurance-agent
REGISTRY ?= quay.io/yelias
VERSION ?= 0.1.0
API_IMAGE_TAG ?=
LLM ?= vllm
LLM_ID ?= meta-llama/Llama-3.1-8B-Instruct
LLM_URL ?= http://llm-service:8000/v1
LLM_API_TOKEN ?=

.PHONY: setup lint test helm-depend helm-lint helm-template deploy deploy-api-image undeploy verify-deploy build-all-images verify-model-assets
.PHONY: training-depend prepare-training deploy-training verify-training verify-training-config
.PHONY: prepare-model-storage

setup:
	$(UV) sync --all-packages
	$(PNPM) install --frozen-lockfile

lint:
	$(UV) run ruff check --config packages/configs/ruff.toml packages/
	find packages -type d \( -name .venv -o -name .tox -o -name node_modules \) -prune -o -type f -name '*.py' -print0 | xargs -0 $(UV) run ruff format --check --config packages/configs/ruff.toml
	$(UV) run mypy --config-file packages/configs/mypy.ini packages/
	@if find packages -mindepth 2 -maxdepth 2 -name package.json -print -quit | grep -q .; then \
		$(PNPM) exec turbo run lint typecheck; \
	else \
		printf '%s\n' 'No Node packages are present yet; skipping Node lint and type checks.'; \
	fi
	$(PNPM) exec prettier --config packages/configs/prettier.json --check .pre-commit-config.yaml CONVENTIONS.md package.json pnpm-workspace.yaml turbo.json 'packages/**/*.{ts,tsx,js,mjs,cjs,jsx,json,yml,yaml,md}'

test:
	@if find packages -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit | grep -q .; then \
		$(UV) run pytest -q packages; \
	else \
		printf '%s\n' 'No Python test files are present yet; skipping Python tests.'; \
	fi
	@if find packages -mindepth 2 -maxdepth 2 -name package.json -print -quit | grep -q .; then \
		$(PNPM) exec turbo run test; \
	else \
		printf '%s\n' 'No Node packages are present yet; skipping Node tests.'; \
	fi

helm-depend:
	$(HELM) dependency update chart/
	$(HELM) dependency build chart/

helm-lint:
	$(HELM) lint chart/

helm-template:
	$(HELM) template "$(RELEASE_NAME)" chart/ --namespace "$(NAMESPACE)" >/dev/null

verify-model-assets:
	$(UV) run --frozen python scripts/verify_model_assets.py --helm "$(HELM)"

deploy:
	$(HELM) upgrade --install "$(RELEASE_NAME)" chart/ --namespace "$(NAMESPACE)" --create-namespace

deploy-api-image:
	@test -n "$(API_IMAGE_TAG)" || { printf '%s\n' 'Set API_IMAGE_TAG to the published API image tag.' >&2; exit 2; }
	$(HELM) upgrade "$(RELEASE_NAME)" chart/ \
		--namespace "$(NAMESPACE)" \
		--reuse-values \
		--set-string api.imageTag="$(API_IMAGE_TAG)" \
		--no-hooks \
		--wait --timeout 10m

undeploy:
	$(HELM) uninstall "$(RELEASE_NAME)" --namespace "$(NAMESPACE)" --ignore-not-found

verify-deploy:
	$(HELM) status "$(RELEASE_NAME)" --namespace "$(NAMESPACE)"

training-depend:
	$(HELM) -n "$(NAMESPACE)" repo add telco-training-charts https://rh-ai-quickstart.github.io/ai-architecture-charts --force-update
	$(HELM) -n "$(NAMESPACE)" dependency build deployment/training/

prepare-training: training-depend
	$(UV) run --frozen python deployment/training/scripts/training.py prepare --namespace "$(NAMESPACE)" --oc "$(OC)"

deploy-training: prepare-training
	$(HELM) -n "$(NAMESPACE)" upgrade --install "$(TRAINING_RELEASE_NAME)" deployment/training/ --wait --wait-for-jobs --timeout 10m

verify-training:
	$(UV) run --frozen python deployment/training/scripts/training.py verify --namespace "$(NAMESPACE)" --release "$(TRAINING_RELEASE_NAME)" --oc "$(OC)" --helm "$(HELM)"

verify-training-config:
	$(HELM) -n "$(NAMESPACE)" lint deployment/training/
	$(HELM) -n "$(NAMESPACE)" template "$(TRAINING_RELEASE_NAME)" deployment/training/ >/dev/null
	$(UV) run --frozen pytest -q deployment/training/tests/

prepare-model-storage:
	$(UV) run --frozen --with boto3==1.43.119 python deployment/models/prepare_storage.py --namespace "$(NAMESPACE)" --oc "$(OC)" --report "$(MODEL_STORAGE_REPORT)"

build-all-images:
	@containerfiles=$$(find packages -type f -name Containerfile -print | sort); \
	if [[ -z "$$containerfiles" ]]; then \
		printf '%s\n' 'No package Containerfiles are present yet; skipping image builds.'; \
		exit 0; \
	fi; \
	while IFS= read -r containerfile; do \
		package_path=$${containerfile%/Containerfile}; \
		image_name=$$(basename "$$package_path"); \
		$(CONTAINER_ENGINE) build -f "$$containerfile" -t "$(REGISTRY)/$$image_name:$(VERSION)" .; \
	done <<< "$$containerfiles"
