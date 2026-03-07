---
name: azure-verified-modules
description: >
  Guidelines for using Azure Verified Modules (AVM) as the preferred
  building blocks for Azure infrastructure. Covers module discovery,
  version pinning, interface conventions, and fallback rules.
---

# Azure Verified Modules (AVM) Guide

## What is AVM?

Azure Verified Modules (AVM) are **Microsoft-maintained, tested, and supported**
Terraform modules for Azure resources. They follow a consistent interface and
are published to the Terraform Registry under the `Azure` namespace.

- **Index — Resource Modules**: <https://azure.github.io/Azure-Verified-Modules/indexes/terraform/tf-resource-modules/>
- **Index — Pattern Modules**: <https://azure.github.io/Azure-Verified-Modules/indexes/terraform/tf-pattern-modules/>
- **Registry**: <https://registry.terraform.io/namespaces/Azure>

## Module Source Convention

All AVM modules follow this source pattern:

```hcl
module "<logical_name>" {
  source  = "Azure/avm-res-<service>-<resource>/azurerm"
  version = "~> X.Y"
  # ...
}
```

### Naming Pattern

| Prefix | Description | Example |
|--------|-------------|---------|
| `avm-res-` | Single-resource module | `Azure/avm-res-network-virtualnetwork/azurerm` |
| `avm-ptn-` | Multi-resource pattern module | `Azure/avm-ptn-hubnetworking/azurerm` |

## Version Pinning (Mandatory)

Every module block **MUST** have an explicit version constraint:

```hcl
# Preferred: pessimistic constraint pins to minor version
module "vnet" {
  source  = "Azure/avm-res-network-virtualnetwork/azurerm"
  version = "~> 0.7"
}
```

| Constraint Style | Meaning | When to Use |
|---|---|---|
| `"~> 0.7"` | `>= 0.7.0, < 0.8.0` | Default — allows patch updates |
| `"0.7.1"` | Exactly `0.7.1` | When exact reproducibility is required |
| `">= 0.5"` | Any version `>= 0.5.0` | **Avoid** — too loose for production |

## Common AVM Modules Reference

| Azure Service | Module Source | Example Version |
|---|---|---|
| Resource Group | `Azure/avm-res-resources-resourcegroup/azurerm` | `~> 0.2` |
| Virtual Network | `Azure/avm-res-network-virtualnetwork/azurerm` | `~> 0.7` |
| Subnet | (part of VNet module) | — |
| NSG | `Azure/avm-res-network-networksecuritygroup/azurerm` | `~> 0.4` |
| Key Vault | `Azure/avm-res-keyvault-vault/azurerm` | `~> 0.9` |
| Storage Account | `Azure/avm-res-storage-storageaccount/azurerm` | `~> 0.4` |
| App Service Plan | `Azure/avm-res-web-serverfarm/azurerm` | `~> 0.4` |
| App Service | `Azure/avm-res-web-site/azurerm` | `~> 0.13` |
| SQL Server | `Azure/avm-res-sql-server/azurerm` | `~> 0.3` |
| SQL Database | (part of SQL Server module) | — |
| Cosmos DB | `Azure/avm-res-documentdb-databaseaccount/azurerm` | `~> 0.4` |
| AKS | `Azure/avm-res-containerservice-managedcluster/azurerm` | `~> 0.6` |
| ACR | `Azure/avm-res-containerregistry-registry/azurerm` | `~> 0.5` |
| Private Endpoint | `Azure/avm-res-network-privateendpoint/azurerm` | `~> 0.3` |
| Private DNS Zone | `Azure/avm-res-network-privatednszone/azurerm` | `~> 0.5` |
| User-Assigned Identity | `Azure/avm-res-managedidentity-userassignedidentity/azurerm` | `~> 0.3` |
| Log Analytics Workspace | `Azure/avm-res-operationalinsights-workspace/azurerm` | `~> 0.4` |
| Application Insights | `Azure/avm-res-insights-component/azurerm` | `~> 0.2` |

> **Note**: Versions listed above are illustrative; always verify the latest
> version at <https://registry.terraform.io/namespaces/Azure>.

## AVM Interface Conventions

All AVM modules share a consistent interface:

### Required Inputs

| Variable | Type | Description |
|---|---|---|
| `name` | `string` | Resource name |
| `resource_group_name` | `string` | Target resource group |
| `location` | `string` | Azure region |

### Common Optional Inputs

| Variable | Type | Description |
|---|---|---|
| `tags` | `map(string)` | Azure tags — always pass `local.tags` |
| `lock` | `object` | Resource lock configuration |
| `role_assignments` | `map(object)` | RBAC role assignments |
| `diagnostic_settings` | `map(object)` | Diagnostic settings (to Log Analytics / Storage) |
| `private_endpoints` | `map(object)` | Private endpoint configuration |
| `managed_identities` | `object` | System / user-assigned identity |
| `customer_managed_key` | `object` | CMK encryption |

### Private Endpoint Pattern (AVM)

```hcl
module "storage" {
  source  = "Azure/avm-res-storage-storageaccount/azurerm"
  version = "~> 0.4"

  name                = "${local.name_prefix}-sa"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  # Disable public access
  public_network_access_enabled = false

  # Private Endpoint via AVM interface
  private_endpoints = {
    blob = {
      name                          = "${local.name_prefix}-sa-pe-blob"
      subnet_resource_id            = module.vnet.subnets["private-endpoints"].resource_id
      subresource_names             = ["blob"]
      private_dns_zone_resource_ids = [module.dns_blob.resource_id]
    }
  }

  tags = local.tags
}
```

### Role Assignment Pattern (AVM)

```hcl
module "keyvault" {
  source  = "Azure/avm-res-keyvault-vault/azurerm"
  version = "~> 0.9"

  name                = "${local.name_prefix}-kv"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tenant_id           = data.azurerm_client_config.current.tenant_id

  role_assignments = {
    deployer_secrets = {
      role_definition_id_or_name = "Key Vault Secrets Officer"
      principal_id               = var.deployer_object_id
    }
    app_reader = {
      role_definition_id_or_name = "Key Vault Secrets User"
      principal_id               = module.app_identity.principal_id
    }
  }

  tags = local.tags
}
```

## Fallback Rules

When an AVM module does **not exist** for a resource:

1. Use the `azurerm_*` resource directly.
2. Follow the same naming, tagging, and private-endpoint conventions.
3. Document the gap in `README.md` under a "## AVM Coverage Gaps" section.

```hcl
# No AVM module for Azure Bastion — using azurerm_bastion_host directly
resource "azurerm_bastion_host" "main" {
  name                = "${local.name_prefix}-bastion"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Standard"

  ip_configuration {
    name                 = "bastion-ip"
    subnet_id            = module.vnet.subnets["AzureBastionSubnet"].resource_id
    public_ip_address_id = azurerm_public_ip.bastion.id
  }

  tags = local.tags
}
```

## Provider Version Compatibility

AVM modules declare their own provider version constraints internally.
You **must** ensure compatibility:

```hcl
# versions.tf
terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"   # Must match AVM module constraints
    }
  }
}
```

- If using `azurerm ~> 4.0`, all AVM module versions must be **compatible with azurerm 4.x**.
- Check each module's page on <https://registry.terraform.io> for its `required_providers` constraint.
- Mismatched versions are the **#1 cause of `terraform init` failures**.

## Checklist

Before submitting generated Terraform code, verify:

- [ ] Every module uses `Azure/avm-*` source (or has a documented fallback reason)
- [ ] Every module block has `version = "~> X.Y"`
- [ ] `tags = local.tags` is passed to every resource/module
- [ ] Private endpoints are configured for all PaaS services
- [ ] `role_assignments` are set for deployer + workload identity
- [ ] `azurerm` provider version in `versions.tf` is compatible with all AVM modules
