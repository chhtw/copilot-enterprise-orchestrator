---
name: terraform-test
description: >
  Generates Terraform test files (.tftest.hcl) using the native
  `terraform test` framework. Covers unit tests, integration tests,
  variable validation tests, and mock providers.
---

# Terraform Test Guide

Generate `.tftest.hcl` files for every Terraform module using the native
`terraform test` framework (Terraform >= 1.6).

## Directory Structure

```
/src/workload/
  main.tf
  variables.tf
  outputs.tf
  locals.tf
  versions.tf
  providers.tf
  tests/
    unit_basic.tftest.hcl       # Unit tests with plan-only
    unit_variables.tftest.hcl   # Variable validation tests
    integration.tftest.hcl      # Integration tests (apply)
```

## Test File Syntax

A `.tftest.hcl` file contains `run` blocks executed sequentially:

```hcl
# tests/unit_basic.tftest.hcl

variables {
  project_name    = "test-project"
  environment     = "dev"
  location        = "eastasia"
  # ... other required variables with test values
}

run "plan_succeeds" {
  command = plan

  assert {
    condition     = true
    error_message = "Plan should succeed with default inputs."
  }
}

run "verify_resource_group" {
  command = plan

  assert {
    condition     = module.resource_group.name != ""
    error_message = "Resource group name must not be empty."
  }
}
```

## Unit Tests (Plan-Only)

Unit tests use `command = plan` — they validate configuration logic
without creating real infrastructure.

### Basic Plan Validation

```hcl
# tests/unit_basic.tftest.hcl

variables {
  project_name = "myapp"
  environment  = "dev"
  location     = "eastasia"
}

run "plan_completes_successfully" {
  command = plan

  assert {
    condition     = true
    error_message = "Terraform plan should complete without errors."
  }
}
```

### Naming Convention Tests

```hcl
run "verify_naming_convention" {
  command = plan

  assert {
    condition     = startswith(local.name_prefix, "dev-myapp")
    error_message = "Name prefix must follow {env}-{project} pattern."
  }
}

run "verify_resource_group_name" {
  command = plan

  assert {
    condition     = can(regex("^dev-myapp-rg-", azurerm_resource_group.main.name))
    error_message = "Resource group name must follow naming convention."
  }
}
```

### Tag Verification Tests

```hcl
run "verify_tags_present" {
  command = plan

  assert {
    condition     = lookup(local.tags, "project", "") == "myapp"
    error_message = "Tags must include 'project'."
  }

  assert {
    condition     = lookup(local.tags, "environment", "") == "dev"
    error_message = "Tags must include 'environment'."
  }

  assert {
    condition     = lookup(local.tags, "managed_by", "") == "terraform"
    error_message = "Tags must include 'managed_by = terraform'."
  }
}
```

### Private Endpoint Tests

```hcl
run "verify_no_public_access" {
  command = plan

  assert {
    condition     = module.storage.public_network_access_enabled == false
    error_message = "Storage account must have public access disabled."
  }
}
```

## Variable Validation Tests

Test that `validation` blocks reject invalid inputs:

```hcl
# tests/unit_variables.tftest.hcl

run "reject_invalid_environment" {
  command = plan

  variables {
    project_name = "test"
    environment  = "invalid"
    location     = "eastasia"
  }

  expect_failures = [
    var.environment,
  ]
}

run "accept_valid_environment_dev" {
  command = plan

  variables {
    project_name = "test"
    environment  = "dev"
    location     = "eastasia"
  }

  assert {
    condition     = var.environment == "dev"
    error_message = "Should accept 'dev' as valid environment."
  }
}

run "accept_valid_environment_prod" {
  command = plan

  variables {
    project_name = "test"
    environment  = "prod"
    location     = "eastasia"
  }

  assert {
    condition     = var.environment == "prod"
    error_message = "Should accept 'prod' as valid environment."
  }
}
```

## Integration Tests (Apply)

Integration tests use `command = apply` and create **real** resources.
Use a separate provider config to target a test subscription or environment.

```hcl
# tests/integration.tftest.hcl

provider "azurerm" {
  features {}
  # Target test subscription
  subscription_id = "00000000-0000-0000-0000-000000000000"
}

variables {
  project_name = "tftest"
  environment  = "test"
  location     = "eastasia"
}

run "deploy_and_verify" {
  command = apply

  assert {
    condition     = output.resource_group_id != ""
    error_message = "Resource group should be created."
  }

  assert {
    condition     = output.vnet_id != ""
    error_message = "Virtual network should be created."
  }
}
```

> **Note**: Integration tests should be run in CI/CD pipelines with
> proper credentials, not during local development.

## Mock Providers

For unit testing without cloud access, use `mock_provider`:

```hcl
# tests/unit_with_mocks.tftest.hcl

mock_provider "azurerm" {}

variables {
  project_name = "mocktest"
  environment  = "dev"
  location     = "eastasia"
}

run "plan_with_mock_provider" {
  command = plan

  assert {
    condition     = true
    error_message = "Plan should succeed with mock provider."
  }
}
```

## Override Files

Use `override_*` blocks to substitute module inputs in tests:

```hcl
run "test_with_override" {
  command = plan

  override_module {
    target = module.vnet
    outputs = {
      resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test-rg/providers/Microsoft.Network/virtualNetworks/test-vnet"
      name        = "test-vnet"
    }
  }

  assert {
    condition     = true
    error_message = "Plan should succeed with overridden module."
  }
}
```

## Test Patterns by Resource Type

### Network Resources

```hcl
run "verify_vnet_address_space" {
  command = plan

  assert {
    condition     = length(module.vnet.address_space) > 0
    error_message = "VNet must have at least one address space."
  }
}

run "verify_subnet_count" {
  command = plan

  assert {
    condition     = length(keys(module.vnet.subnets)) >= 2
    error_message = "VNet must have at least 2 subnets."
  }
}
```

### Security Resources

```hcl
run "verify_keyvault_rbac" {
  command = plan

  assert {
    condition     = length(keys(module.keyvault.role_assignments)) > 0
    error_message = "Key Vault must have RBAC role assignments."
  }
}

run "verify_keyvault_private" {
  command = plan

  assert {
    condition     = module.keyvault.public_network_access_enabled == false
    error_message = "Key Vault must not have public network access."
  }
}
```

## Checklist

When generating test files, ensure:

- [ ] At least one `unit_basic.tftest.hcl` with plan-only checks
- [ ] Variable validation tests for every `validation {}` block
- [ ] Naming convention assertions
- [ ] Tag presence assertions
- [ ] Private endpoint / public access assertions for PaaS resources
- [ ] `mock_provider` or `override_module` used for isolated unit tests
- [ ] Tests are in the `tests/` subdirectory of the module
- [ ] All test `variables {}` blocks provide valid default values
