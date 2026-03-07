---
name: terraform-style-guide
description: >
  Enforces consistent Terraform code style, naming conventions,
  file layout, and formatting rules aligned with the official
  Terraform Style Guide and Azure best practices.
---

# Terraform Style Guide

Apply these rules to **every** `.tf` file you generate or modify.

## File Layout

Each Terraform module MUST contain these files (in this order of convention):

| File             | Purpose                                       |
|------------------|-----------------------------------------------|
| `versions.tf`    | `terraform {}` block: `required_version`, `required_providers` |
| `providers.tf`   | Provider configuration blocks                 |
| `main.tf`        | Primary resource / module declarations        |
| `variables.tf`   | All `variable` blocks                         |
| `locals.tf`      | All `locals` blocks                           |
| `outputs.tf`     | All `output` blocks                           |

For larger modules, split `main.tf` into logical files (e.g., `network.tf`, `compute.tf`).

## Naming Conventions

### Resources & Data Sources

```hcl
# Pattern: <provider>_<service>_<component>
# Name:    snake_case, descriptive, NO redundant type prefix
resource "azurerm_resource_group" "main" { ... }
resource "azurerm_virtual_network" "hub" { ... }
data "azurerm_client_config" "current" {}
```

**Do:**
- Use `this` when there is only one resource of a type in the module.
- Use a meaningful short name when there are multiple (`hub`, `spoke`, `primary`).

**Don't:**
- `resource "azurerm_resource_group" "azurerm_resource_group"` (redundant type).
- `resource "azurerm_resource_group" "rg-1"` (avoid hyphens in Terraform identifiers).

### Variables

```hcl
variable "location" {
  description = "Azure region for all resources."
  type        = string
  default     = "eastasia"
}
```

- Use `snake_case`.
- Always provide `description` and `type`.
- Provide `default` only when a sensible universal default exists.
- Use `validation` blocks for constrained inputs.

### Outputs

```hcl
output "resource_group_id" {
  description = "Resource ID of the resource group."
  value       = azurerm_resource_group.main.id
}
```

- Name pattern: `<resource_logical_name>_<attribute>`.
- Always provide `description`.

### Locals

```hcl
locals {
  project  = var.project_name
  env      = var.environment
  location = var.location

  tags = {
    project     = local.project
    environment = local.env
    managed_by  = "terraform"
  }

  name_prefix = "${local.env}-${local.project}"
}
```

- Centralise tags, naming prefixes, and computed values in `locals`.
- Reference `local.<name>` throughout — never hardcode strings.

## Formatting Rules

1. **Indentation**: 2 spaces (Terraform standard). No tabs.
2. **Alignment**: Align `=` signs within a block for readability.
3. **Blank lines**: One blank line between top-level blocks. No multiple consecutive blanks.
4. **Block ordering within a resource**:
   1. Meta-arguments first (`count`, `for_each`, `depends_on`, `provider`, `lifecycle`).
   2. Required arguments.
   3. Optional arguments.
   4. Nested blocks (e.g., `identity {}`, `network_rules {}`).
5. **String interpolation**: Use `"${var.x}"` only when combining with other text. Use bare `var.x` otherwise.
6. **Collections**: Trailing comma after the last element.
7. **Comments**: Use `#` for single-line comments. Reserve `//` for URLs.

## Module Block Style

```hcl
module "vnet" {
  source  = "Azure/avm-res-network-virtualnetwork/azurerm"
  version = "~> 0.7"

  name                = "${local.name_prefix}-vnet"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location

  address_space = ["10.0.0.0/16"]

  tags = local.tags
}
```

- `source` and `version` always come **first** inside a module block.
- Pin version with `~>` operator (pessimistic constraint).
- Group related arguments visually.

## Variable Validation

```hcl
variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "stg", "prod"], var.environment)
    error_message = "environment must be one of: dev, stg, prod."
  }
}
```

## Sensitive Values

```hcl
variable "admin_password" {
  description = "Admin password for the VM."
  type        = string
  sensitive   = true
}

output "connection_string" {
  description = "Database connection string (sensitive)."
  value       = azurerm_mssql_server.main.connection_string
  sensitive   = true
}
```

## Dynamic Blocks

Use `dynamic` blocks to reduce repetition — but keep them readable:

```hcl
dynamic "subnet" {
  for_each = var.subnets
  content {
    name             = subnet.value.name
    address_prefixes = subnet.value.address_prefixes
  }
}
```

## Anti-Patterns to Avoid

| Anti-Pattern | Correct Approach |
|---|---|
| Hardcoded names/locations | Use `var.*` / `local.*` |
| Missing `description` on variables/outputs | Always add `description` |
| `version = ">= 0.1"` (too loose) | Use `~> X.Y` pessimistic constraint |
| Providers declared in child modules | Declare providers only in root module |
| `depends_on` when implicit dependency exists | Remove unnecessary `depends_on` |
| `count` for complex conditional resources | Use `for_each` with a map |
