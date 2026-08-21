# Cloud Run deployment workflow for the FastMCP gateway.
#
# In Google Cloud Shell, the usual deployment command is simply:
#   make deploy
#
# Every setting can be overridden at invocation time, for example:
#   make deploy PROJECT_ID=my-project REGION=us-east1 PUBLIC=false

SHELL := /bin/bash
.DEFAULT_GOAL := help
.NOTPARALLEL:

# Use the project currently selected in gcloud unless PROJECT_ID is provided.
PROJECT_ID ?= $(shell gcloud config get-value project 2>/dev/null)
PROJECT_ID := $(PROJECT_ID)
REGION ?= us-central1
SERVICE ?= et-gateway
REPOSITORY ?= et-gateway
IMAGE_NAME ?= et-gateway

# Use a unique UTC tag for each Make invocation so deployments are traceable.
TAG ?= $(shell date -u +%Y%m%d-%H%M%S)
TAG := $(TAG)
IMAGE_URI = $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(REPOSITORY)/$(IMAGE_NAME):$(TAG)

# Cloud Run runtime settings. PUBLIC=true is convenient for remote MCP clients.
# Use PUBLIC=false when the client can send a Google-signed identity token.
PUBLIC ?= true
PORT ?= 8080
CPU ?= 1
MEMORY ?= 512Mi
CONCURRENCY ?= 80
MIN_INSTANCES ?= 0
MAX_INSTANCES ?= 10
TIMEOUT ?= 3600s
LOG_LIMIT ?= 50
LOG_FRESHNESS ?= 1d

AUTH_FLAG = $(if $(filter true,$(PUBLIC)),--allow-unauthenticated,--no-allow-unauthenticated)

.PHONY: help check config enable-apis repository bootstrap build deploy \
	deploy-private url smoke-test describe logs revisions images

help: ## Show the available commands.
	@echo "FastMCP gateway deployment"
	@echo
	@echo "  make deploy          Enable APIs, build the image, and deploy it"
	@echo "  make deploy-private  Deploy with Google Cloud authentication required"
	@echo "  make bootstrap       Enable APIs and create the image repository"
	@echo "  make build           Build and push only the container image"
	@echo "  make config          Show the settings that will be used"
	@echo "  make url             Print the service URL and MCP HTTP endpoint"
	@echo "  make smoke-test      Verify remote MCP tool discovery"
	@echo "  make logs            Read recent Cloud Run logs"
	@echo "  make describe        Show the deployed Cloud Run service"
	@echo "  make revisions       List deployed revisions"
	@echo "  make images          List images in Artifact Registry"
	@echo
	@echo "Examples:"
	@echo "  make deploy PROJECT_ID=my-gcp-project"
	@echo "  make deploy REGION=us-east1 MAX_INSTANCES=3"
	@echo "  make logs LOG_LIMIT=100 LOG_FRESHNESS=2h"

check: ## Validate local files, gcloud authentication, and input variables.
	@command -v gcloud >/dev/null || { echo "Error: gcloud is not installed." >&2; exit 1; }
	@test -n "$(strip $(PROJECT_ID))" && test "$(PROJECT_ID)" != "(unset)" || { echo "Error: no GCP project is selected. Run 'gcloud config set project PROJECT_ID' or pass PROJECT_ID=..." >&2; exit 1; }
	@gcloud auth list --filter='status:ACTIVE' --format='value(account)' | grep -q . || { echo "Error: gcloud has no active account. Run 'gcloud auth login'." >&2; exit 1; }
	@test "$(PUBLIC)" = "true" || test "$(PUBLIC)" = "false" || { echo "Error: PUBLIC must be true or false." >&2; exit 1; }
	@test -f Dockerfile || { echo "Error: Dockerfile was not found." >&2; exit 1; }
	@test -f server.py || { echo "Error: server.py was not found." >&2; exit 1; }
	@test -f requirements.txt || { echo "Error: requirements.txt was not found." >&2; exit 1; }

config: check ## Display the resolved deployment configuration.
	@echo "PROJECT_ID=$(PROJECT_ID)"
	@echo "REGION=$(REGION)"
	@echo "SERVICE=$(SERVICE)"
	@echo "REPOSITORY=$(REPOSITORY)"
	@echo "IMAGE_URI=$(IMAGE_URI)"
	@echo "PUBLIC=$(PUBLIC)"
	@echo "CPU=$(CPU)"
	@echo "MEMORY=$(MEMORY)"
	@echo "CONCURRENCY=$(CONCURRENCY)"
	@echo "MIN_INSTANCES=$(MIN_INSTANCES)"
	@echo "MAX_INSTANCES=$(MAX_INSTANCES)"
	@echo "TIMEOUT=$(TIMEOUT)"

enable-apis: check ## Enable services used by Cloud Build, Artifact Registry, and Cloud Run.
	@echo "Enabling required Google Cloud APIs in $(PROJECT_ID)..."
	gcloud services enable \
		run.googleapis.com \
		artifactregistry.googleapis.com \
		cloudbuild.googleapis.com \
		--project="$(PROJECT_ID)" \
		--quiet

repository: enable-apis ## Create the Docker repository if it does not already exist.
	@echo "Checking Artifact Registry repository $(REPOSITORY)..."
	@if gcloud artifacts repositories describe "$(REPOSITORY)" --location="$(REGION)" --project="$(PROJECT_ID)" >/dev/null 2>&1; then \
		echo "Artifact Registry repository already exists."; \
	else \
		gcloud artifacts repositories create "$(REPOSITORY)" \
			--repository-format=docker \
			--location="$(REGION)" \
			--description="Container images for the FastMCP gateway" \
			--project="$(PROJECT_ID)" \
			--quiet; \
	fi

bootstrap: repository ## Prepare the GCP project without building or deploying.
	@echo "Google Cloud project setup is complete."

build: repository ## Build the Dockerfile remotely and push the image to Artifact Registry.
	@echo "Building and pushing $(IMAGE_URI)..."
	gcloud builds submit . \
		--tag="$(IMAGE_URI)" \
		--region="$(REGION)" \
		--project="$(PROJECT_ID)" \
		--quiet

deploy: build ## Run the complete deployment and print the MCP endpoint.
	@echo "Deploying $(SERVICE) to Cloud Run..."
	gcloud run deploy "$(SERVICE)" \
		--image="$(IMAGE_URI)" \
		--region="$(REGION)" \
		--project="$(PROJECT_ID)" \
		--platform=managed \
		--execution-environment=gen2 \
		--port="$(PORT)" \
		--cpu="$(CPU)" \
		--memory="$(MEMORY)" \
		--concurrency="$(CONCURRENCY)" \
		--min-instances="$(MIN_INSTANCES)" \
		--max-instances="$(MAX_INSTANCES)" \
		--timeout="$(TIMEOUT)" \
		--ingress=all \
		$(AUTH_FLAG) \
		--quiet
	@echo
	@$(MAKE) --no-print-directory url PROJECT_ID="$(PROJECT_ID)" REGION="$(REGION)" SERVICE="$(SERVICE)"

deploy-private: ## Run the complete deployment with IAM authentication required.
	@$(MAKE) --no-print-directory deploy \
		PROJECT_ID="$(PROJECT_ID)" \
		REGION="$(REGION)" \
		SERVICE="$(SERVICE)" \
		REPOSITORY="$(REPOSITORY)" \
		IMAGE_NAME="$(IMAGE_NAME)" \
		PUBLIC=false

url: check ## Print the Cloud Run base URL and the Streamable HTTP MCP URL.
	@SERVICE_URL="$$(gcloud run services describe "$(SERVICE)" --region="$(REGION)" --project="$(PROJECT_ID)" --format='value(status.url)')"; \
		echo "Service URL: $$SERVICE_URL"; \
		echo "MCP Streamable HTTP endpoint: $$SERVICE_URL/mcp"

smoke-test: check ## Verify tools/list on the deployed public HTTP endpoint.
	@SERVICE_URL="$$(gcloud run services describe "$(SERVICE)" --region="$(REGION)" --project="$(PROJECT_ID)" --format='value(status.url)')"; \
		if ! RESPONSE="$$(curl --silent --show-error --fail --max-time 10 \
			--request POST "$$SERVICE_URL/mcp" \
			--header 'Content-Type: application/json' \
			--header 'Accept: application/json, text/event-stream' \
			--data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')"; then \
			echo "Error: MCP tools/list request failed." >&2; \
			exit 1; \
		fi; \
		if grep -q 'get_show_or_movie_info' <<<"$$RESPONSE" && grep -q 'get_weather_info' <<<"$$RESPONSE"; then \
			echo "MCP tool discovery is healthy: $$SERVICE_URL/mcp"; \
		else \
			echo "Error: MCP response did not include both expected tools." >&2; \
			exit 1; \
		fi

describe: check ## Show the current Cloud Run service configuration.
	gcloud run services describe "$(SERVICE)" \
		--region="$(REGION)" \
		--project="$(PROJECT_ID)"

logs: check ## Read recent Cloud Run service logs.
	gcloud run services logs read "$(SERVICE)" \
		--region="$(REGION)" \
		--project="$(PROJECT_ID)" \
		--limit="$(LOG_LIMIT)" \
		--freshness="$(LOG_FRESHNESS)"

revisions: check ## List the service's deployed revisions.
	gcloud run revisions list \
		--service="$(SERVICE)" \
		--region="$(REGION)" \
		--project="$(PROJECT_ID)"

images: check ## List container images built for this gateway.
	gcloud artifacts docker images list \
		"$(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(REPOSITORY)/$(IMAGE_NAME)" \
		--include-tags \
		--project="$(PROJECT_ID)"
