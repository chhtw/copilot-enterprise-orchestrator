"""
retail_prices.py — 透過 Azure Retail Prices REST API 查詢各資源的官方單價。

API Endpoint: https://prices.azure.com/api/retail/prices
  - 免費、無需驗證
  - 支援 OData $filter
  - 回傳 1000 筆/頁，有 NextPageLink 分頁
  - Filter 欄位: serviceName, armRegionName, skuName, meterName, priceType, ...
  - Filter 值區分大小寫

使用方式：
  prices = await fetch_prices_for_line_items(line_items)
  # prices: dict[int, RetailPrice | None]  — key = line_item index
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

from .contracts import CostLineItem

logger = logging.getLogger("orchestrator.retail_prices")

API_URL = "https://prices.azure.com/api/retail/prices"

# Timeout for each HTTP request (seconds)
REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Dataclass for a price record returned by the API
# ---------------------------------------------------------------------------
@dataclass
class RetailPrice:
    """Simplified representation of an Azure Retail Price record."""

    service_name: str
    sku_name: str
    arm_region_name: str
    meter_name: str
    product_name: str
    unit_of_measure: str
    retail_price: float  # USD per unit
    currency_code: str
    price_type: str  # "Consumption" | "Reservation" | ...

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> RetailPrice:
        return cls(
            service_name=item.get("serviceName", ""),
            sku_name=item.get("skuName", ""),
            arm_region_name=item.get("armRegionName", ""),
            meter_name=item.get("meterName", ""),
            product_name=item.get("productName", ""),
            unit_of_measure=item.get("unitOfMeasure", ""),
            retail_price=float(item.get("retailPrice", 0)),
            currency_code=item.get("currencyCode", "USD"),
            price_type=item.get("type", ""),
        )


# ---------------------------------------------------------------------------
# product_id → serviceName 映射表
# ---------------------------------------------------------------------------
# product_id 是 Step 3a (CostStructureOutput) 中 LLM 給出的 slug，
# serviceName 是 Azure Retail Prices API 用的官方服務名（case-sensitive）。
PRODUCT_SLUG_TO_SERVICE: dict[str, str] = {
    "resource-groups": "",  # Free, no pricing
    "ddos-protection": "Azure DDoS Protection",
    "virtual-network": "Virtual Network",
    "network-security-groups": "",  # Free
    "log-analytics": "Log Analytics",
    "microsoft-sentinel": "Sentinel",
    "microsoft-defender-for-cloud": "Microsoft Defender for Cloud",
    "key-vault": "Key Vault",
    "dns": "Azure DNS",
    "storage-accounts": "Storage",
    "azure-firewall": "Azure Firewall",
    "azure-bastion": "Azure Bastion",
    "vpn-gateway": "VPN Gateway",
    "front-door": "Azure Front Door Service",
    "azure-policy": "",  # Free (built-in)
    # Common additions
    "virtual-machines": "Virtual Machines",
    "app-service": "Azure App Service",
    "sql-database": "SQL Database",
    "cosmos-db": "Azure Cosmos DB",
    "container-instances": "Container Instances",
    "container-registry": "Container Registry",
    "kubernetes-service": "Azure Kubernetes Service",
    "redis-cache": "Azure Cache for Redis",
    "application-gateway": "Application Gateway",
    "load-balancer": "Load Balancer",
    "public-ip": "Virtual Network",  # Public IP is under VNet pricing
    "private-endpoints": "Azure Private Link",
    "nat-gateway": "NAT Gateway",
    "express-route": "ExpressRoute",
    "api-management": "API Management",
    "functions": "Functions",
    "logic-apps": "Logic Apps",
    "event-hubs": "Event Hubs",
    "service-bus": "Service Bus",
    "cognitive-services": "Cognitive Services",
    "openai": "Azure OpenAI Service",
    "monitor": "Azure Monitor",
}

# Terraform resource_type → serviceName 備用映射
TERRAFORM_TYPE_TO_SERVICE: dict[str, str] = {
    "azurerm_resource_group": "",
    "azurerm_network_ddos_protection_plan": "Azure DDoS Protection",
    "azurerm_virtual_network": "Virtual Network",
    "azurerm_subnet": "",
    "azurerm_network_security_group": "",
    "azurerm_log_analytics_workspace": "Log Analytics",
    "azurerm_sentinel_log_analytics_workspace_onboarding": "Sentinel",
    "azurerm_security_center_subscription_pricing": "Microsoft Defender for Cloud",
    "azurerm_key_vault": "Key Vault",
    "azurerm_key_vault_key": "Key Vault",
    "azurerm_private_dns_zone": "Azure DNS",
    "azurerm_storage_account": "Storage",
    "azurerm_firewall": "Azure Firewall",
    "azurerm_firewall_policy": "Azure Firewall",
    "azurerm_bastion_host": "Azure Bastion",
    "azurerm_virtual_network_gateway": "VPN Gateway",
    "azurerm_cdn_frontdoor_profile": "Azure Front Door Service",
    "azurerm_cdn_frontdoor_firewall_policy": "Azure Front Door Service",
    "azurerm_policy_assignment": "",
    # Common extras
    "azurerm_virtual_machine": "Virtual Machines",
    "azurerm_linux_virtual_machine": "Virtual Machines",
    "azurerm_windows_virtual_machine": "Virtual Machines",
    "azurerm_app_service": "Azure App Service",
    "azurerm_app_service_plan": "Azure App Service",
    "azurerm_service_plan": "Azure App Service",
    "azurerm_linux_web_app": "Azure App Service",
    "azurerm_windows_web_app": "Azure App Service",
    "azurerm_cosmosdb_account": "Azure Cosmos DB",
    "azurerm_mssql_database": "SQL Database",
    "azurerm_mssql_server": "SQL Database",
    "azurerm_kubernetes_cluster": "Azure Kubernetes Service",
    "azurerm_container_registry": "Container Registry",
    "azurerm_redis_cache": "Azure Cache for Redis",
    "azurerm_application_gateway": "Application Gateway",
    "azurerm_lb": "Load Balancer",
    "azurerm_public_ip": "Virtual Network",
    "azurerm_private_endpoint": "Azure Private Link",
    "azurerm_nat_gateway": "NAT Gateway",
    "azurerm_api_management": "API Management",
    "azurerm_function_app": "Functions",
    "azurerm_logic_app_workflow": "Logic Apps",
    "azurerm_eventhub_namespace": "Event Hubs",
    "azurerm_servicebus_namespace": "Service Bus",
}

# ARM resource type → serviceName 備用映射
ARM_TYPE_TO_SERVICE: dict[str, str] = {
    "Microsoft.Network/virtualNetworks": "Virtual Network",
    "Microsoft.Network/networkSecurityGroups": "",
    "Microsoft.Network/privateDnsZones": "Azure DNS",
    "Microsoft.Network/azureFirewalls": "Azure Firewall",
    "Microsoft.Network/firewallPolicies": "Azure Firewall",
    "Microsoft.Network/bastionHosts": "Azure Bastion",
    "Microsoft.Network/virtualNetworkGateways": "VPN Gateway",
    "Microsoft.Network/publicIPAddresses": "Virtual Network",
    "Microsoft.Network/privateEndpoints": "Azure Private Link",
    "Microsoft.Network/natGateways": "NAT Gateway",
    "Microsoft.Network/applicationGateways": "Application Gateway",
    "Microsoft.Network/loadBalancers": "Load Balancer",
    "Microsoft.OperationalInsights/workspaces": "Log Analytics",
    "Microsoft.KeyVault/vaults": "Key Vault",
    "Microsoft.Storage/storageAccounts": "Storage",
    "Microsoft.Cdn/profiles": "Azure Front Door Service",
    "Microsoft.Cdn/FrontDoorWebApplicationFirewallPolicies": "Azure Front Door Service",
    "Microsoft.Compute/virtualMachines": "Virtual Machines",
    "Microsoft.Web/sites": "Azure App Service",
    "Microsoft.Web/serverFarms": "Azure App Service",
    "Microsoft.DocumentDB/databaseAccounts": "Azure Cosmos DB",
    "Microsoft.Sql/servers": "SQL Database",
    "Microsoft.ContainerService/managedClusters": "Azure Kubernetes Service",
    "Microsoft.ContainerRegistry/registries": "Container Registry",
    "Microsoft.Cache/Redis": "Azure Cache for Redis",
    "Microsoft.ApiManagement/service": "API Management",
}


# ---------------------------------------------------------------------------
# Resolve service name for a CostLineItem
# ---------------------------------------------------------------------------
def resolve_service_name(item: CostLineItem) -> str:
    """
    Determine the Azure Retail Prices API 'serviceName' for a given CostLineItem.
    Uses product_id slug, then terraform resource_type, then ARM type as fallbacks.
    """
    # 1. Try product_id slug
    if item.product_id:
        slug = item.product_id.lower().strip()
        svc = PRODUCT_SLUG_TO_SERVICE.get(slug)
        if svc:
            return svc
        if svc == "":    # explicitly mapped as free
            return ""

    # 2. Try terraform resource_type (azurerm_*)
    rt = item.resource_type.strip()
    if rt.startswith("azurerm_"):
        svc = TERRAFORM_TYPE_TO_SERVICE.get(rt)
        if svc is not None:
            return svc

    # 3. Try ARM type (Microsoft.*)
    if rt.startswith("Microsoft."):
        svc = ARM_TYPE_TO_SERVICE.get(rt)
        if svc is not None:
            return svc

    # 4. Fallback — return empty (will use LLM estimate)
    logger.warning("[RetailPrices] Cannot resolve serviceName for %s / %s", rt, item.product_id)
    return ""


# ---------------------------------------------------------------------------
# Build OData filter
# ---------------------------------------------------------------------------
def build_filter(
    service_name: str,
    region: str,
    *,
    sku_contains: str = "",
    price_type: str = "Consumption",
) -> str:
    """
    Build an OData $filter string for the Retail Prices API.
    Values are case-sensitive in the API.
    """
    parts: list[str] = []
    parts.append(f"serviceName eq '{service_name}'")

    # Global services (Front Door, DNS, Defender, Policy) have no region filter
    region_lower = region.lower().strip()
    if region_lower and region_lower != "global":
        parts.append(f"armRegionName eq '{region_lower}'")

    if price_type:
        parts.append(f"priceType eq '{price_type}'")

    return " and ".join(parts)


# ---------------------------------------------------------------------------
# Query the API (with pagination)
# ---------------------------------------------------------------------------
async def _query_api(
    session: aiohttp.ClientSession,
    odata_filter: str,
    *,
    max_pages: int = 3,
) -> list[dict[str, Any]]:
    """
    Query Azure Retail Prices API with pagination.
    Returns raw Items list from the API response.
    """
    all_items: list[dict[str, Any]] = []
    url: str | None = API_URL
    params: dict[str, str] = {"$filter": odata_filter, "api-version": "2023-01-01-preview"}
    pages = 0

    while url and pages < max_pages:
        try:
            async with session.get(
                url,
                params=params if pages == 0 else None,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            logger.error("[RetailPrices] API error: %s (filter=%s)", e, odata_filter)
            break

        items = data.get("Items", [])
        all_items.extend(items)
        url = data.get("NextPageLink")
        pages += 1

    logger.info("[RetailPrices] filter=%s → %d items (%d pages)", odata_filter, len(all_items), pages)
    return all_items


# ---------------------------------------------------------------------------
# Score & pick the best price match for a line item
# ---------------------------------------------------------------------------
def _score_match(item: CostLineItem, price: dict[str, Any]) -> float:
    """
    Score how well a price record matches the line item (higher = better).
    Returns negative for obvious mismatches.
    """
    score = 0.0
    sku_name = price.get("skuName", "").lower()
    meter_name = price.get("meterName", "").lower()
    product_name = price.get("productName", "").lower()

    item_sku = item.sku.lower().strip() if item.sku else ""
    item_display = item.display_name.lower().strip() if item.display_name else ""

    # Strong SKU match
    if item_sku and item_sku in sku_name:
        score += 10
    elif item_sku and item_sku in product_name:
        score += 5

    # Meter / display name similarity
    if item.meter and item.meter.lower() in meter_name:
        score += 3

    # Prefer non-zero price for paid resources
    retail_price = float(price.get("retailPrice", 0))
    if retail_price > 0 and item.estimated_monthly_usd > 0:
        score += 2
    elif retail_price == 0 and item.estimated_monthly_usd == 0:
        score += 2

    # Penalize reservation prices when we want PAYG
    if price.get("type", "") == "Reservation":
        score -= 5

    # Prefer primary meters (avoid "Overage", "Low Priority" etc.)
    if "overage" in meter_name or "low priority" in meter_name or "spot" in meter_name:
        score -= 3

    return score


def pick_best_price(item: CostLineItem, api_items: list[dict[str, Any]]) -> RetailPrice | None:
    """Pick the single best matching price record for a CostLineItem."""
    if not api_items:
        return None

    scored = [(i, _score_match(item, i)) for i in api_items]
    scored.sort(key=lambda x: x[1], reverse=True)

    best_item, best_score = scored[0]
    logger.debug(
        "[RetailPrices] Best match for %s: score=%.1f sku=%s meter=%s price=%.4f",
        item.display_name,
        best_score,
        best_item.get("skuName"),
        best_item.get("meterName"),
        best_item.get("retailPrice", 0),
    )
    return RetailPrice.from_api(best_item)


# ---------------------------------------------------------------------------
# PricedLineItem: combines original CostLineItem + API price
# ---------------------------------------------------------------------------
@dataclass
class PricedLineItem:
    """A CostLineItem enriched with the official retail price."""

    line_item: CostLineItem
    retail_price: RetailPrice | None
    unit_price_usd: float  # Official unit price (0 if not found)
    monthly_cost_usd: float  # quantity × unit_price (or fallback to LLM estimate)
    source: str  # "retail_api" | "llm_estimate" | "free"

    @property
    def display_name(self) -> str:
        return self.line_item.display_name or self.line_item.name


# ---------------------------------------------------------------------------
# Main entry point: fetch prices for all line items
# ---------------------------------------------------------------------------
async def fetch_prices_for_line_items(
    line_items: list[CostLineItem],
) -> list[PricedLineItem]:
    """
    For each CostLineItem, query Azure Retail Prices API and return
    a PricedLineItem with official pricing (or fallback to LLM estimate).
    """
    results: list[PricedLineItem] = []

    async with aiohttp.ClientSession() as session:
        # Group items by service_name to reduce API calls
        service_groups: dict[str, list[tuple[int, CostLineItem]]] = {}
        for idx, item in enumerate(line_items):
            svc = resolve_service_name(item)
            service_groups.setdefault(svc, []).append((idx, item))

        # Query API for each unique service (except free ones)
        api_cache: dict[str, list[dict[str, Any]]] = {}

        tasks = []
        for svc in service_groups:
            if not svc:
                continue  # Free service, skip API call
            # Use the first item's region as representative
            first_item = service_groups[svc][0][1]
            region = first_item.region or ""
            cache_key = f"{svc}|{region}"
            if cache_key not in api_cache:
                odata_filter = build_filter(svc, region)
                tasks.append((cache_key, odata_filter))

        # Execute API calls concurrently (max 5 at a time)
        sem = asyncio.Semaphore(5)

        async def _fetch(key: str, flt: str) -> tuple[str, list[dict[str, Any]]]:
            async with sem:
                items = await _query_api(session, flt)
                return key, items

        if tasks:
            fetched = await asyncio.gather(
                *[_fetch(k, f) for k, f in tasks],
                return_exceptions=True,
            )
            for result in fetched:
                if isinstance(result, Exception):
                    logger.error("[RetailPrices] Fetch error: %s", result)
                    continue
                key, items = result
                api_cache[key] = items

        # Match each line item
        for svc, group in service_groups.items():
            for idx, item in group:
                if not svc:
                    # Free resource
                    results.append(PricedLineItem(
                        line_item=item,
                        retail_price=None,
                        unit_price_usd=0.0,
                        monthly_cost_usd=0.0,
                        source="free",
                    ))
                    continue

                region = item.region or ""
                cache_key = f"{svc}|{region}"
                api_items = api_cache.get(cache_key, [])
                best = pick_best_price(item, api_items)

                if best and best.retail_price > 0:
                    monthly = item.quantity * best.retail_price
                    results.append(PricedLineItem(
                        line_item=item,
                        retail_price=best,
                        unit_price_usd=best.retail_price,
                        monthly_cost_usd=monthly,
                        source="retail_api",
                    ))
                else:
                    # Fallback to LLM estimate from Step 3a
                    results.append(PricedLineItem(
                        line_item=item,
                        retail_price=best,
                        unit_price_usd=0.0,
                        monthly_cost_usd=item.estimated_monthly_usd,
                        source="llm_estimate",
                    ))

    # Sort by original order (idx)
    # We lost the idx tracking in the grouping; let's keep insertion order
    # which already follows group → item order. Re-sort by line_items index.
    item_order = {id(item): i for i, item in enumerate(line_items)}
    results.sort(key=lambda r: item_order.get(id(r.line_item), 999))

    total = sum(r.monthly_cost_usd for r in results)
    api_count = sum(1 for r in results if r.source == "retail_api")
    logger.info(
        "[RetailPrices] Priced %d items: %d from API, %d fallback, total=$%.2f/mo",
        len(results),
        api_count,
        len(results) - api_count,
        total,
    )
    return results
