#!/usr/bin/env bash
# =============================================================================
# Azure authentication, shared by every KPI collector.
#
# Source it — do not execute it — so the exported values reach the caller:
#
#   source .gitlab/scripts/common/azure_login.sh
#
# Exports:
#   MANAGEMENT_ENDPOINT  resource manager URL for the current cloud, no
#                        trailing slash
#
# Required environment (resolved by GitLab from the environment scope):
#   RUNNER_CLIENT_ID, RUNNER_CLIENT_SECRET, AZURE_TENANT_ID,
#   AZURE_SUBSCRIPTION_ID
# =============================================================================

echo "[INFO] Logging in to Azure..."
az cloud set --name AzureUSGovernment
az login \
  --service-principal \
  --username "${RUNNER_CLIENT_ID}" \
  --password "${RUNNER_CLIENT_SECRET}" \
  --tenant "${AZURE_TENANT_ID}" \
  --output none

az account set --subscription "${AZURE_SUBSCRIPTION_ID}" --output none
echo "[INFO] Authenticated. Subscription set to ${AZURE_SUBSCRIPTION_ID}"

export MANAGEMENT_ENDPOINT
MANAGEMENT_ENDPOINT=$(az cloud show --query "endpoints.resourceManager" -o tsv | sed 's|/$||')
