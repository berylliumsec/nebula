"""Deterministic vulnerability intelligence normalization and correlation."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from enum import Enum
from typing import Any
from urllib.parse import unquote

import httpx
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field
from semantic_version import Version as SemanticVersion

from .artifacts import ArtifactStore
from .domain import (
    Advisory,
    Correlation,
    CorrelationMethod,
    CorrelationStatus,
    Severity,
    SoftwareComponent,
    SourceSnapshot,
    utc_now,
)
from .storage import NebulaStore


class IntelligenceError(RuntimeError):
    pass


class VersionRange(BaseModel):
    scheme: str | None = None
    introduced: str | None = None
    fixed: str | None = None
    last_affected: str | None = None
    start_including: str | None = None
    start_excluding: str | None = None
    end_including: str | None = None
    end_excluding: str | None = None


class AffectedProduct(BaseModel):
    purl: str | None = None
    ecosystem: str | None = None
    package: str | None = None
    cpe: str | None = None
    vendor: str | None = None
    product: str | None = None
    versions: list[str] = Field(default_factory=list)
    ranges: list[VersionRange] = Field(default_factory=list)


class CorrelationCandidate(BaseModel):
    advisory_id: str
    method: CorrelationMethod
    status: CorrelationStatus
    confidence: float = Field(ge=0, le=1)
    rationale: str
    matched_identifiers: dict[str, str] = Field(default_factory=dict)

    def to_domain(
        self,
        *,
        engagement_id: str,
        component_id: str | None = None,
        service_id: str | None = None,
        evidence_ids: list[str] | None = None,
    ) -> Correlation:
        return Correlation(
            engagement_id=engagement_id,
            component_id=component_id,
            service_id=service_id,
            advisory_id=self.advisory_id,
            method=self.method,
            status=self.status,
            confidence=self.confidence,
            rationale=self.rationale,
            matched_identifiers=self.matched_identifiers,
            supporting_evidence_ids=evidence_ids or [],
        )


class ActionCategory(str, Enum):
    ACT = "act"
    ATTEND = "attend"
    TRACK = "track"
    DEFER = "defer"
    REVIEW = "review"


class PriorityFactors(BaseModel):
    exposure: bool | None = None
    reachable: bool | None = None
    asset_criticality: Severity = Severity.MEDIUM
    match_confidence: float = Field(ge=0, le=1)
    kev: bool = False
    epss_probability: float | None = Field(default=None, ge=0, le=1)
    cvss_base: float | None = Field(default=None, ge=0, le=10)
    exploit_available: bool | None = None
    prerequisites_met: bool | None = None
    mitigation_present: bool | None = None
    validated: bool = False


class PriorityDecision(BaseModel):
    category: ActionCategory
    rationale: list[str]
    factors: PriorityFactors


def prioritize(factors: PriorityFactors) -> PriorityDecision:
    """Return a transparent SSVC-style action category, not an AI score."""

    reasons: list[str] = []
    if factors.mitigation_present:
        reasons.append("a verified mitigation is present")
    if factors.kev:
        reasons.append("the advisory appears in CISA KEV")
    if factors.epss_probability is not None:
        reasons.append(f"EPSS probability is {factors.epss_probability:.1%}")
    if factors.exposure:
        reasons.append("the affected asset is externally exposed")
    if factors.reachable:
        reasons.append("the affected service is reachable")
    if factors.validated:
        reasons.append("the finding has been technically validated")
    if factors.match_confidence < 0.8 or not factors.validated:
        return PriorityDecision(
            category=ActionCategory.REVIEW,
            rationale=["applicability requires analyst review", *reasons],
            factors=factors,
        )
    if factors.mitigation_present and not factors.kev:
        return PriorityDecision(
            category=ActionCategory.TRACK,
            rationale=reasons,
            factors=factors,
        )
    if factors.kev and (factors.exposure or factors.reachable):
        return PriorityDecision(
            category=ActionCategory.ACT,
            rationale=reasons,
            factors=factors,
        )
    if (
        factors.asset_criticality in {Severity.HIGH, Severity.CRITICAL}
        and (factors.cvss_base or 0) >= 7
    ) or (factors.epss_probability or 0) >= 0.2:
        return PriorityDecision(
            category=ActionCategory.ATTEND,
            rationale=reasons,
            factors=factors,
        )
    if factors.reachable or factors.exposure:
        return PriorityDecision(
            category=ActionCategory.TRACK,
            rationale=reasons,
            factors=factors,
        )
    return PriorityDecision(
        category=ActionCategory.DEFER,
        rationale=reasons or ["no current exposure or exploitation signal"],
        factors=factors,
    )


def _version(value: str) -> Version | tuple[tuple[int, int | str], ...]:
    try:
        return Version(value)
    except InvalidVersion:
        tokens = re.findall(r"\d+|[a-zA-Z]+", value.lower())
        return tuple(
            (0, int(token)) if token.isdigit() else (1, token) for token in tokens
        )


def _compare(left: str, right: str) -> int:
    a = _version(left)
    b = _version(right)
    if type(a) is not type(b):
        # Invalid vendor versions compare through a stable token representation.
        a = tuple(
            (1, str(a)),
        )
        b = tuple(
            (1, str(b)),
        )
    return (a > b) - (a < b)


def _range_is_comparable(
    version: str | None,
    affected: VersionRange,
    ecosystem: str | None = None,
) -> bool:
    if not version or (affected.scheme or "").upper() == "GIT":
        return False
    bounds = [
        affected.introduced,
        affected.fixed,
        affected.last_affected,
        affected.start_including,
        affected.start_excluding,
        affected.end_including,
        affected.end_excluding,
    ]
    values = [version, *(value for value in bounds if value not in {None, "0"})]
    scheme = (affected.scheme or "").upper()
    if scheme == "SEMVER":
        try:
            for value in values:
                SemanticVersion(value)
        except (ValueError, TypeError):
            return False
        return True
    if scheme == "ECOSYSTEM":
        # OSV delegates comparison rules to each ecosystem. Only PyPI/PEP 440
        # is implemented here; npm, Maven, rpm, deb, and others stay candidates.
        if (ecosystem or "").casefold() != "pypi":
            return False
        try:
            for value in values:
                Version(value)
        except InvalidVersion:
            return False
        return True
    # NVD CPE ranges omit a scheme. Confirm only simple dotted numeric values.
    return all(re.fullmatch(r"\d+(?:\.\d+){0,5}", value) for value in values)


def version_in_range(
    version: str | None,
    affected: VersionRange,
    *,
    ecosystem: str | None = None,
) -> bool:
    if not _range_is_comparable(version, affected, ecosystem):
        return False
    assert version is not None
    use_semver = (affected.scheme or "").upper() == "SEMVER"

    def compare(left: str, right: str) -> int:
        if use_semver:
            a = SemanticVersion(left)
            b = SemanticVersion(right)
            return (a > b) - (a < b)
        return _compare(left, right)

    if (
        affected.introduced not in {None, "0"}
        and compare(version, affected.introduced) < 0
    ):
        return False
    if affected.fixed is not None and compare(version, affected.fixed) >= 0:
        return False
    if (
        affected.last_affected is not None
        and compare(version, affected.last_affected) > 0
    ):
        return False
    if (
        affected.start_including is not None
        and compare(version, affected.start_including) < 0
    ):
        return False
    if (
        affected.start_excluding is not None
        and compare(version, affected.start_excluding) <= 0
    ):
        return False
    if (
        affected.end_including is not None
        and compare(version, affected.end_including) > 0
    ):
        return False
    if (
        affected.end_excluding is not None
        and compare(version, affected.end_excluding) >= 0
    ):
        return False
    return True


def _affected_version(version: str | None, product: AffectedProduct) -> bool:
    if not version:
        return False
    if product.versions and version in product.versions:
        return True
    return any(
        version_in_range(version, item, ecosystem=product.ecosystem)
        for item in product.ranges
    )


def _has_comparable_version_data(version: str | None, product: AffectedProduct) -> bool:
    return bool(product.versions) or any(
        _range_is_comparable(version, item, product.ecosystem)
        for item in product.ranges
    )


class ParsedPurl(BaseModel):
    ecosystem: str
    namespace: str | None = None
    name: str
    version: str | None = None


def parse_purl(value: str) -> ParsedPurl:
    if not value.startswith("pkg:"):
        raise ValueError("PURL must start with pkg:")
    body = value[4:].split("#", 1)[0].split("?", 1)[0]
    package, separator, version = body.partition("@")
    ecosystem, slash, path = package.partition("/")
    if not slash or not ecosystem or not path:
        raise ValueError("PURL is missing type or package name")
    parts = [unquote(part).lower() for part in path.split("/") if part]
    if not parts:
        raise ValueError("PURL is missing a package name")
    return ParsedPurl(
        ecosystem=ecosystem.lower(),
        namespace="/".join(parts[:-1]) or None,
        name=parts[-1],
        version=unquote(version) if separator and version else None,
    )


def _split_cpe(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":":
            parts.append("".join(current))
            current = []
        else:
            current.append(character)
    parts.append("".join(current))
    return parts


def parse_cpe(value: str) -> tuple[str, str, str, str | None]:
    parts = _split_cpe(value.lower())
    if len(parts) < 6 or parts[0] != "cpe" or parts[1] != "2.3":
        raise ValueError("only CPE 2.3 identifiers are supported")
    version = None if parts[5] in {"*", "-", ""} else parts[5]
    return parts[2], parts[3], parts[4], version


def normalize_affected(raw: dict[str, Any]) -> AffectedProduct:
    ranges: list[VersionRange] = []
    for item in raw.get("ranges", []):
        if "events" in item:
            current: dict[str, str] = {"scheme": item.get("type", "")}
            for event in item.get("events", []):
                if "introduced" in event:
                    if any(key != "scheme" for key in current):
                        ranges.append(VersionRange.model_validate(current))
                    current = {
                        "scheme": item.get("type", ""),
                        "introduced": event["introduced"],
                    }
                elif "fixed" in event:
                    current["fixed"] = event["fixed"]
                    ranges.append(VersionRange.model_validate(current))
                    current = {"scheme": item.get("type", "")}
                elif "last_affected" in event:
                    current["last_affected"] = event["last_affected"]
                    ranges.append(VersionRange.model_validate(current))
                    current = {"scheme": item.get("type", "")}
            if any(key != "scheme" for key in current):
                ranges.append(VersionRange.model_validate(current))
        else:
            allowed = VersionRange.model_fields.keys()
            ranges.append(
                VersionRange.model_validate(
                    {key: value for key, value in item.items() if key in allowed}
                )
            )
    package = raw.get("package")
    package_name = (
        package.get("name") if isinstance(package, dict) else raw.get("package_name")
    )
    ecosystem = (
        package.get("ecosystem") if isinstance(package, dict) else raw.get("ecosystem")
    )
    purl = package.get("purl") if isinstance(package, dict) else raw.get("purl")
    return AffectedProduct(
        purl=purl,
        ecosystem=ecosystem,
        package=package_name,
        cpe=raw.get("cpe") or raw.get("criteria"),
        vendor=raw.get("vendor"),
        product=raw.get("product"),
        versions=[
            item["version"] if isinstance(item, dict) else str(item)
            for item in raw.get("versions", [])
            if not isinstance(item, dict)
            or item.get("status", "affected") == "affected"
        ],
        ranges=ranges,
    )


class CorrelationEngine:
    """Match identifiers and ranges in a fixed, auditable order."""

    def correlate(
        self,
        component: SoftwareComponent,
        advisory: Advisory,
        *,
        scanner_cve_ids: list[str] | None = None,
        service_banner: str | None = None,
    ) -> CorrelationCandidate | None:
        affected = [normalize_affected(item) for item in advisory.affected]
        purl_match = self._purl(component, affected)
        if purl_match:
            return self._candidate(advisory, CorrelationMethod.PURL, *purl_match)
        cpe_match = self._cpe(component, affected)
        if cpe_match:
            return self._candidate(advisory, CorrelationMethod.CPE, *cpe_match)
        if advisory.advisory_id.upper() in {
            value.upper() for value in scanner_cve_ids or []
        }:
            product_supported = self._product_supported(component, affected)
            if product_supported:
                status = (
                    CorrelationStatus.CONFIRMED
                    if component.version
                    and any(
                        _affected_version(component.version, item) for item in affected
                    )
                    else CorrelationStatus.CANDIDATE
                )
                return CorrelationCandidate(
                    advisory_id=advisory.advisory_id,
                    method=CorrelationMethod.SCANNER_CVE,
                    status=status,
                    confidence=0.9 if status == CorrelationStatus.CONFIRMED else 0.7,
                    rationale=(
                        "scanner CVE identifier agrees with the observed product and version"
                        if status == CorrelationStatus.CONFIRMED
                        else "scanner CVE identifier agrees with the product, but version applicability is incomplete"
                    ),
                    matched_identifiers={"scanner_cve": advisory.advisory_id},
                )
        return self._fuzzy(component, advisory, affected, service_banner)

    @staticmethod
    def _candidate(
        advisory: Advisory,
        method: CorrelationMethod,
        status: CorrelationStatus,
        rationale: str,
        identifiers: dict[str, str],
    ) -> CorrelationCandidate:
        return CorrelationCandidate(
            advisory_id=advisory.advisory_id,
            method=method,
            status=status,
            confidence=(
                0.99
                if status
                in {CorrelationStatus.CONFIRMED, CorrelationStatus.NOT_AFFECTED}
                else 0.75
            ),
            rationale=rationale,
            matched_identifiers=identifiers,
        )

    @staticmethod
    def _purl(
        component: SoftwareComponent, affected: list[AffectedProduct]
    ) -> tuple[CorrelationStatus, str, dict[str, str]] | None:
        if not component.purl:
            return None
        try:
            observed = parse_purl(component.purl)
        except ValueError:
            return None
        observed_version = component.version or observed.version
        identity_seen = False
        has_version_data = False
        for product in affected:
            if product.purl:
                try:
                    expected = parse_purl(product.purl)
                except ValueError:
                    continue
                identity_match = (
                    observed.ecosystem == expected.ecosystem
                    and observed.namespace == expected.namespace
                    and observed.name == expected.name
                )
            else:
                identity_match = (
                    bool(product.ecosystem and product.package)
                    and observed.ecosystem == product.ecosystem.lower()
                    and observed.name == product.package.rsplit("/", 1)[-1].lower()
                )
            if not identity_match:
                continue
            identity_seen = True
            has_version_data = has_version_data or _has_comparable_version_data(
                observed_version, product
            )
            if _affected_version(observed_version, product):
                return (
                    CorrelationStatus.CONFIRMED,
                    "exact PURL/ecosystem identity and affected version range match",
                    {"purl": component.purl, "version": observed_version or "unknown"},
                )
            if observed_version is None:
                return (
                    CorrelationStatus.CANDIDATE,
                    "exact PURL identity match; observed version is missing",
                    {"purl": component.purl},
                )
        if identity_seen and observed_version and has_version_data:
            return (
                CorrelationStatus.NOT_AFFECTED,
                "exact PURL identity match, but the observed version is outside every affected range",
                {"purl": component.purl, "version": observed_version},
            )
        if identity_seen:
            return (
                CorrelationStatus.CANDIDATE,
                "exact PURL identity match; advisory version applicability is incomplete",
                {"purl": component.purl, "version": observed_version or "unknown"},
            )
        return None

    @staticmethod
    def _cpe(
        component: SoftwareComponent, affected: list[AffectedProduct]
    ) -> tuple[CorrelationStatus, str, dict[str, str]] | None:
        identity_seen: tuple[str, str | None] | None = None
        has_version_data = False
        for observed_value in component.cpes:
            try:
                observed_part, observed_vendor, observed_product, observed_version = (
                    parse_cpe(observed_value)
                )
            except ValueError:
                continue
            observed_version = component.version or observed_version
            for product in affected:
                if not product.cpe:
                    continue
                try:
                    expected_part, expected_vendor, expected_product, _ = parse_cpe(
                        product.cpe
                    )
                except ValueError:
                    continue
                if (observed_part, observed_vendor, observed_product) != (
                    expected_part,
                    expected_vendor,
                    expected_product,
                ):
                    continue
                identity_seen = (observed_value, observed_version)
                has_version_data = has_version_data or _has_comparable_version_data(
                    observed_version, product
                )
                if _affected_version(observed_version, product):
                    return (
                        CorrelationStatus.CONFIRMED,
                        "exact CPE vendor/product and affected version range match",
                        {
                            "cpe": observed_value,
                            "version": observed_version or "unknown",
                        },
                    )
                if observed_version is None:
                    return (
                        CorrelationStatus.CANDIDATE,
                        "exact CPE product match; observed version is missing",
                        {"cpe": observed_value},
                    )
        if identity_seen and identity_seen[1] and has_version_data:
            return (
                CorrelationStatus.NOT_AFFECTED,
                "exact CPE product match, but the observed version is outside every affected range",
                {"cpe": identity_seen[0], "version": identity_seen[1]},
            )
        if identity_seen:
            return (
                CorrelationStatus.CANDIDATE,
                "exact CPE product match; advisory version applicability is incomplete",
                {"cpe": identity_seen[0], "version": identity_seen[1] or "unknown"},
            )
        return None

    @staticmethod
    def _product_supported(
        component: SoftwareComponent, affected: list[AffectedProduct]
    ) -> bool:
        names = {component.name.casefold()}
        if component.vendor:
            names.add(f"{component.vendor} {component.name}".casefold())
        return any(
            (item.product and item.product.replace("_", " ").casefold() in names)
            or (
                item.package
                and item.package.rsplit("/", 1)[-1].casefold()
                == component.name.casefold()
            )
            for item in affected
        )

    @staticmethod
    def _fuzzy(
        component: SoftwareComponent,
        advisory: Advisory,
        affected: list[AffectedProduct],
        banner: str | None,
    ) -> CorrelationCandidate | None:
        observed = " ".join(
            value for value in [component.vendor, component.name, banner] if value
        ).casefold()
        if not observed:
            return None
        best_name = ""
        best_ratio = 0.0
        for item in affected:
            expected = (
                " ".join(
                    value
                    for value in [item.vendor, item.product, item.package]
                    if value
                )
                .replace("_", " ")
                .casefold()
            )
            if expected:
                ratio = SequenceMatcher(None, observed, expected).ratio()
                if ratio > best_ratio:
                    best_ratio, best_name = ratio, expected
        if best_ratio < 0.72:
            return None
        return CorrelationCandidate(
            advisory_id=advisory.advisory_id,
            method=CorrelationMethod.FUZZY_BANNER,
            status=CorrelationStatus.CANDIDATE,
            confidence=min(0.69, best_ratio * 0.7),
            rationale="fuzzy banner/product similarity; analyst confirmation is mandatory",
            matched_identifiers={"observed": observed, "candidate": best_name},
        )


class FeedPage(BaseModel):
    source: str
    fetched_at: datetime
    raw_bytes: bytes
    advisories: list[Advisory] = Field(default_factory=list)
    record_count: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def snapshot(self, *, artifact_id: str | None = None) -> SourceSnapshot:
        return SourceSnapshot(
            source=self.source,
            fetched_at=self.fetched_at,
            sha256=hashlib.sha256(self.raw_bytes).hexdigest(),
            record_count=(
                self.record_count
                if self.record_count is not None
                else len(self.advisories)
            ),
            artifact_id=artifact_id,
            metadata=self.metadata,
        )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_nvd(record: dict[str, Any]) -> Advisory:
    cve = record.get("cve", record)
    descriptions = cve.get("descriptions", [])
    description = next(
        (item.get("value", "") for item in descriptions if item.get("lang") == "en"),
        "",
    )
    affected: list[dict[str, Any]] = []
    for configuration in cve.get("configurations", []):
        for node in configuration.get("nodes", []):
            for match in node.get("cpeMatch", []):
                if not match.get("vulnerable", True):
                    continue
                ranges = [
                    {
                        "start_including": match.get("versionStartIncluding"),
                        "start_excluding": match.get("versionStartExcluding"),
                        "end_including": match.get("versionEndIncluding"),
                        "end_excluding": match.get("versionEndExcluding"),
                    }
                ]
                affected.append({"cpe": match.get("criteria"), "ranges": ranges})
    return Advisory(
        advisory_id=cve["id"],
        source="nvd",
        title=cve["id"],
        description=description,
        published_at=_parse_time(cve.get("published")),
        modified_at=_parse_time(cve.get("lastModified")),
        cvss=cve.get("metrics", {}),
        cwes=[
            item.get("description", [{}])[0].get("value", "")
            for item in cve.get("weaknesses", [])
        ],
        affected=affected,
        references=[item.get("url", "") for item in cve.get("references", [])],
        raw=cve,
    )


def normalize_osv(record: dict[str, Any]) -> Advisory:
    return Advisory(
        advisory_id=record["id"],
        source="osv",
        title=record.get("summary") or record["id"],
        description=record.get("details", ""),
        published_at=_parse_time(record.get("published")),
        modified_at=_parse_time(record.get("modified")),
        cwes=[item for item in record.get("database_specific", {}).get("cwe_ids", [])],
        affected=record.get("affected", []),
        references=[item.get("url", "") for item in record.get("references", [])],
        raw=record,
    )


def normalize_cve_v5(record: dict[str, Any]) -> Advisory:
    """Normalize a CVE List V5/Vulnrichment record without model inference."""

    metadata = record.get("cveMetadata", {})
    containers = record.get("containers", {})
    cna = containers.get("cna", {})
    descriptions = cna.get("descriptions", [])
    description = next(
        (
            item.get("value", "")
            for item in descriptions
            if item.get("lang", "").lower().startswith("en")
        ),
        descriptions[0].get("value", "") if descriptions else "",
    )
    affected: list[dict[str, Any]] = []
    for product in cna.get("affected", []):
        normalized: dict[str, Any] = {
            "vendor": product.get("vendor"),
            "product": product.get("product"),
            "purl": product.get("packageName")
            if str(product.get("packageName", "")).startswith("pkg:")
            else None,
            "versions": [],
            "ranges": [],
        }
        for version in product.get("versions", []):
            if version.get("status") != "affected":
                continue
            value = version.get("version")
            less_than = version.get("lessThan")
            less_or_equal = version.get("lessThanOrEqual")
            if less_than or less_or_equal:
                normalized["ranges"].append(
                    {
                        "scheme": version.get("versionType"),
                        "start_including": None if value in {None, "0"} else value,
                        "end_excluding": less_than,
                        "end_including": less_or_equal,
                    }
                )
            elif value:
                normalized["versions"].append(value)
        for cpe in product.get("cpes", []):
            copy = dict(normalized)
            copy["cpe"] = cpe
            affected.append(copy)
        if not product.get("cpes"):
            affected.append(normalized)
    metrics = cna.get("metrics", [])
    adp = containers.get("adp", [])
    kev = any(
        "kev" in json.dumps(container, sort_keys=True).casefold() for container in adp
    )
    return Advisory(
        advisory_id=metadata["cveId"],
        source="cve-list-v5",
        title=cna.get("title") or metadata["cveId"],
        description=description,
        published_at=_parse_time(metadata.get("datePublished")),
        modified_at=_parse_time(metadata.get("dateUpdated")),
        cvss={"metrics": metrics},
        cwes=[
            item.get("description", [{}])[0].get("cweId", "")
            for item in cna.get("problemTypes", [])
        ],
        affected=affected,
        references=[item.get("url", "") for item in cna.get("references", [])],
        kev=kev,
        raw=record,
    )


class VulnerabilityFeeds:
    """Small source adapters; callers persist every raw page before applying it."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._external_client = client

    async def _get(self, url: str, **params: Any) -> httpx.Response:
        if self._external_client is not None:
            response = await self._external_client.get(url, params=params or None)
        else:
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
                response = await client.get(url, params=params or None)
        response.raise_for_status()
        return response

    async def nvd_page(
        self,
        *,
        start_index: int = 0,
        results_per_page: int = 2000,
        last_modified_start: datetime | None = None,
        last_modified_end: datetime | None = None,
    ) -> FeedPage:
        params: dict[str, Any] = {
            "startIndex": start_index,
            "resultsPerPage": min(results_per_page, 2000),
        }
        if last_modified_start:
            params["lastModStartDate"] = last_modified_start.isoformat()
        if last_modified_end:
            params["lastModEndDate"] = last_modified_end.isoformat()
        response = await self._get(
            "https://services.nvd.nist.gov/rest/json/cves/2.0", **params
        )
        payload = response.json()
        return FeedPage(
            source="nvd",
            fetched_at=utc_now(),
            raw_bytes=response.content,
            advisories=[
                normalize_nvd(item) for item in payload.get("vulnerabilities", [])
            ],
            metadata={
                "start_index": payload.get("startIndex", start_index),
                "total_results": payload.get("totalResults"),
            },
        )

    async def cisa_kev(self) -> FeedPage:
        response = await self._get(
            "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
        )
        payload = response.json()
        advisories = [
            Advisory(
                advisory_id=item["cveID"],
                source="cisa-kev",
                title=item.get("vulnerabilityName") or item["cveID"],
                description=item.get("shortDescription", ""),
                kev=True,
                affected=[
                    {
                        "vendor": item.get("vendorProject"),
                        "product": item.get("product"),
                    }
                ],
                references=[item["notes"]]
                if item.get("notes", "").startswith("http")
                else [],
                raw=item,
            )
            for item in payload.get("vulnerabilities", [])
        ]
        return FeedPage(
            source="cisa-kev",
            fetched_at=utc_now(),
            raw_bytes=response.content,
            advisories=advisories,
            metadata={"catalog_version": payload.get("catalogVersion")},
        )

    async def epss(self) -> tuple[FeedPage, dict[str, tuple[float, float]]]:
        response = await self._get(
            "https://epss.cyentia.com/epss_scores-current.csv.gz"
        )
        content = gzip.decompress(response.content)
        text = content.decode("utf-8")
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        values: dict[str, tuple[float, float]] = {}
        for row in csv.DictReader(io.StringIO("\n".join(lines))):
            values[row["cve"]] = (float(row["epss"]), float(row["percentile"]))
        return (
            FeedPage(
                source="first-epss",
                fetched_at=utc_now(),
                raw_bytes=response.content,
                record_count=len(values),
                metadata={"record_count": len(values)},
            ),
            values,
        )

    async def osv(self, *, purl: str, version: str | None = None) -> FeedPage:
        request = {"package": {"purl": purl}}
        if version:
            request["version"] = version
        if self._external_client is not None:
            response = await self._external_client.post(
                "https://api.osv.dev/v1/query", json=request
            )
        else:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    "https://api.osv.dev/v1/query", json=request
                )
        response.raise_for_status()
        payload = response.json()
        canonical = json.dumps(payload, sort_keys=True).encode()
        return FeedPage(
            source="osv",
            fetched_at=utc_now(),
            raw_bytes=canonical,
            advisories=[normalize_osv(item) for item in payload.get("vulns", [])],
            metadata={"query_purl": purl, "query_version": version},
        )


def persist_feed_page(
    page: FeedPage,
    *,
    store: NebulaStore,
    artifact_store: ArtifactStore,
) -> tuple[SourceSnapshot, list[Advisory]]:
    """Persist raw source bytes and normalized records as one logical ingestion."""

    stored = artifact_store.put_bytes_with_status(
        page.raw_bytes,
        engagement_id="system:vulnerability-intelligence",
        filename=f"{page.source}-{page.fetched_at.isoformat()}.snapshot",
        media_type="application/octet-stream",
        source=page.source,
        metadata={"fetched_at": page.fetched_at.isoformat()},
    )
    snapshot = page.snapshot(artifact_id=stored.artifact.id)
    advisories = [
        advisory.model_copy(update={"source_snapshot_id": snapshot.id})
        for advisory in page.advisories
    ]
    try:
        with store.transaction() as transaction:
            transaction.add(stored.artifact)
            transaction.add(snapshot)
            transaction.add_all(advisories)
    except Exception:
        artifact_store.discard_new_blob(stored)
        raise
    return snapshot, advisories


def merge_advisory_signals(
    advisories: list[Advisory],
    *,
    kev_ids: set[str] | None = None,
    epss: dict[str, tuple[float, float]] | None = None,
) -> list[Advisory]:
    """Deterministically enrich records while retaining each source snapshot."""

    kev = {value.upper() for value in (kev_ids or set())}
    probabilities = {key.upper(): value for key, value in (epss or {}).items()}
    merged: list[Advisory] = []
    for advisory in advisories:
        metrics = probabilities.get(advisory.advisory_id.upper())
        merged.append(
            advisory.model_copy(
                update={
                    "kev": advisory.kev or advisory.advisory_id.upper() in kev,
                    "epss_probability": (
                        metrics[0] if metrics else advisory.epss_probability
                    ),
                    "epss_percentile": (
                        metrics[1] if metrics else advisory.epss_percentile
                    ),
                }
            )
        )
    return merged


__all__ = [
    "ActionCategory",
    "AffectedProduct",
    "CorrelationCandidate",
    "CorrelationEngine",
    "FeedPage",
    "PriorityDecision",
    "PriorityFactors",
    "VersionRange",
    "VulnerabilityFeeds",
    "normalize_nvd",
    "normalize_osv",
    "normalize_cve_v5",
    "merge_advisory_signals",
    "parse_cpe",
    "parse_purl",
    "prioritize",
    "persist_feed_page",
    "version_in_range",
]
