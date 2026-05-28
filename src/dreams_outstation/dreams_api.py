from __future__ import annotations

import json
import ssl
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .models import AppConfig, DreamsApiConfig, SiteConfig


def build_plant_dnp3_id_url(config: DreamsApiConfig) -> str:
    if not config.base_url:
        raise ValueError("dreams_api.base_url is required")
    if not config.plant_meter_no:
        raise ValueError("dreams_api.plant_meter_no is required")

    base = config.base_url.rstrip("/")
    api_root = base if base.endswith("/api") else f"{base}/api"
    path = f"{api_root}/plants/plantMeterNo/{quote(config.plant_meter_no, safe='')}"
    query = urlencode({"token": config.site_token}) if config.site_token else ""
    return f"{path}?{query}" if query else path


def fetch_plant_dnp3_ids(config: DreamsApiConfig) -> list[dict[str, Any]]:
    url = build_plant_dnp3_id_url(config)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "dreams-outstation/1.0"})
    context = None
    if url.lower().startswith("https://") and not config.verify_tls:
        context = ssl._create_unverified_context()

    with urlopen(request, timeout=config.timeout_seconds, context=context) as response:
        payload = response.read().decode("utf-8")
    data = json.loads(payload)
    return normalize_plant_dnp3_ids(data)


def normalize_plant_dnp3_ids(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ValueError("DREAMS DNP3 ID API response must be a JSON array")

    plants: list[dict[str, Any]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"DREAMS DNP3 ID API item {index} must be an object")
        try:
            dnp3_address = int(item["dnp3Address"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"DREAMS DNP3 ID API item {index} has invalid dnp3Address") from exc
        plants.append(
            {
                "plantNo": str(item.get("plantNo", "")),
                "plantName": str(item.get("plantName", "")),
                "dnp3Address": dnp3_address,
            }
        )
    return plants


def apply_dnp3_id_lookup(config: AppConfig) -> tuple[AppConfig, dict[str, Any]]:
    status: dict[str, Any] = {
        "enabled": config.dreams_api.enabled,
        "applied": False,
        "ok": None,
        "message": "DREAMS API lookup is disabled.",
        "plants": [],
        "matches": [],
        "unmatched_sites": [],
        "unmatched_plants": [],
    }
    if not config.dreams_api.enabled:
        return config, status
    try:
        plants = fetch_plant_dnp3_ids(config.dreams_api)
        updated, mapping_status = apply_plant_mappings_to_config(config, plants)
    except Exception as exc:
        status.update({"ok": False, "message": str(exc)})
        return config, status

    status.update(mapping_status)
    status["ok"] = True
    status["message"] = _status_message(status)
    return updated, status


def apply_plant_mappings_to_config(config: AppConfig, plants: list[dict[str, Any]]) -> tuple[AppConfig, dict[str, Any]]:
    normalized = normalize_plant_dnp3_ids(plants)
    status: dict[str, Any] = {
        "enabled": config.dreams_api.enabled,
        "applied": False,
        "plants": normalized,
        "matches": [],
        "unmatched_sites": [],
        "unmatched_plants": [],
    }
    if not config.dreams_api.apply_to_sites:
        status["message"] = "DREAMS API lookup completed; apply_to_sites is disabled."
        return config, status

    updated_sites = list(config.sites)
    used_plants: set[int] = set()
    used_sites: set[int] = set()

    for site_index, site in enumerate(updated_sites):
        plant_index, matched_by = _find_named_plant(site, normalized, used_plants)
        if plant_index is None:
            continue
        updated_sites[site_index] = _site_with_plant(site, normalized[plant_index])
        used_sites.add(site_index)
        used_plants.add(plant_index)
        status["matches"].append(_match_payload(site, updated_sites[site_index], normalized[plant_index], matched_by))

    remaining_sites = [
        index
        for index, site in enumerate(updated_sites)
        if index not in used_sites and not _is_wildcard(site.logger_id)
    ]
    remaining_plants = [index for index, _plant in enumerate(normalized) if index not in used_plants]
    if len(remaining_sites) == len(remaining_plants):
        for site_index, plant_index in zip(remaining_sites, remaining_plants):
            site = updated_sites[site_index]
            updated_sites[site_index] = _site_with_plant(site, normalized[plant_index])
            used_sites.add(site_index)
            used_plants.add(plant_index)
            status["matches"].append(_match_payload(site, updated_sites[site_index], normalized[plant_index], "order"))

    status["unmatched_sites"] = [
        {"site_id": site.site_id, "logger_id": site.logger_id, "dnp3_address": site.dnp3_address}
        for index, site in enumerate(updated_sites)
        if index not in used_sites
    ]
    status["unmatched_plants"] = [plant for index, plant in enumerate(normalized) if index not in used_plants]
    status["applied"] = bool(status["matches"])
    status["message"] = _status_message(status)
    return replace(config, sites=tuple(updated_sites)), status


def _find_named_plant(site: SiteConfig, plants: list[dict[str, Any]], used_plants: set[int]) -> tuple[int | None, str | None]:
    if _is_wildcard(site.logger_id):
        return None, None
    candidates: list[tuple[str, str]] = []
    if site.plant_no:
        candidates.append(("plant_no", site.plant_no))
    candidates.append(("logger_id", site.logger_id))

    for matched_by, value in candidates:
        for index, plant in enumerate(plants):
            if index in used_plants:
                continue
            if value == plant.get("plantNo"):
                return index, matched_by
    return None, None


def _site_with_plant(site: SiteConfig, plant: dict[str, Any]) -> SiteConfig:
    return replace(
        site,
        dnp3_address=int(plant["dnp3Address"]),
        plant_no=str(plant.get("plantNo") or site.plant_no or ""),
        plant_name=str(plant.get("plantName") or site.plant_name or ""),
        dnp3_address_source="dreams_api",
    )


def _match_payload(
    old_site: SiteConfig,
    new_site: SiteConfig,
    plant: dict[str, Any],
    matched_by: str | None,
) -> dict[str, Any]:
    return {
        "site_id": old_site.site_id,
        "logger_id": old_site.logger_id,
        "plantNo": plant.get("plantNo"),
        "plantName": plant.get("plantName"),
        "old_dnp3_address": old_site.dnp3_address,
        "dnp3Address": new_site.dnp3_address,
        "matched_by": matched_by or "unknown",
    }


def _status_message(status: dict[str, Any]) -> str:
    fetched = len(status.get("plants") or [])
    matched = len(status.get("matches") or [])
    if fetched == 0:
        return "DREAMS API lookup returned no plants."
    if matched == 0:
        return f"DREAMS API lookup fetched {fetched} plant(s), but no configured logger matched."
    return f"DREAMS API lookup fetched {fetched} plant(s) and applied {matched} DNP3 ID mapping(s)."


def _is_wildcard(value: str) -> bool:
    return value.strip() in {"", "*"}
