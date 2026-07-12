import pytest

from nebula.v3.domain import (
    Advisory,
    CorrelationMethod,
    CorrelationStatus,
    SoftwareComponent,
)
from nebula.v3.intelligence import (
    CorrelationEngine,
    VersionRange,
    parse_cpe,
    parse_purl,
    version_in_range,
)


def _advisory(affected):
    return Advisory(
        advisory_id="CVE-2026-1234",
        source="test-feed",
        title="Example vulnerability",
        affected=affected,
    )


@pytest.mark.parametrize(
    "version,expected",
    [
        ("1.2.0", True),
        ("1.9.9", True),
        ("2.0.0", False),
        ("1.1.9", False),
        (None, False),
    ],
)
def test_version_range_boundaries_are_deterministic(version, expected):
    affected = VersionRange(start_including="1.2.0", end_excluding="2.0.0")
    assert version_in_range(version, affected) is expected


def test_exact_purl_and_affected_version_confirm_correlation():
    advisory = _advisory(
        [
            {
                "package": {
                    "ecosystem": "PyPI",
                    "name": "acme/widgets",
                    "purl": "pkg:pypi/acme/widgets",
                },
                "ranges": [
                    {
                        "events": [
                            {"introduced": "1.2.0"},
                            {"fixed": "2.0.0"},
                        ]
                    }
                ],
            }
        ]
    )
    component = SoftwareComponent(
        engagement_id="eng-1",
        name="widgets",
        version="1.8.4",
        ecosystem="PyPI",
        purl="pkg:pypi/acme/widgets@1.8.4",
    )

    match = CorrelationEngine().correlate(component, advisory)
    assert match is not None
    assert match.method == CorrelationMethod.PURL
    assert match.status == CorrelationStatus.CONFIRMED
    assert match.confidence == 0.99
    assert match.matched_identifiers["version"] == "1.8.4"
    parsed = parse_purl(component.purl)
    assert (parsed.ecosystem, parsed.namespace, parsed.name, parsed.version) == (
        "pypi",
        "acme",
        "widgets",
        "1.8.4",
    )


def test_exact_cpe_and_affected_version_confirm_correlation():
    advisory = _advisory(
        [
            {
                "cpe": "cpe:2.3:a:apache:http_server:*:*:*:*:*:*:*:*",
                "ranges": [{"start_including": "2.4.0", "end_excluding": "2.4.58"}],
            }
        ]
    )
    component = SoftwareComponent(
        engagement_id="eng-1",
        name="http_server",
        vendor="apache",
        version="2.4.57",
        cpes=["cpe:2.3:a:apache:http_server:2.4.57:*:*:*:*:*:*:*"],
    )

    match = CorrelationEngine().correlate(component, advisory)
    assert match is not None
    assert match.method == CorrelationMethod.CPE
    assert match.status == CorrelationStatus.CONFIRMED
    assert parse_cpe(component.cpes[0]) == (
        "a",
        "apache",
        "http_server",
        "2.4.57",
    )


def test_missing_version_stays_candidate_instead_of_auto_confirming():
    advisory = _advisory(
        [
            {
                "package": {
                    "ecosystem": "npm",
                    "name": "left-pad",
                    "purl": "pkg:npm/left-pad",
                },
                "ranges": [{"events": [{"introduced": "0"}]}],
            }
        ]
    )
    component = SoftwareComponent(
        engagement_id="eng-1",
        name="left-pad",
        purl="pkg:npm/left-pad",
    )
    match = CorrelationEngine().correlate(component, advisory)
    assert match is not None
    assert match.method == CorrelationMethod.PURL
    assert match.status == CorrelationStatus.CANDIDATE


def test_fuzzy_banner_match_never_auto_confirms():
    advisory = _advisory([{"vendor": "Acme", "product": "Enterprise Widget Server"}])
    component = SoftwareComponent(
        engagement_id="eng-1",
        name="Enterprise Widget Server",
        vendor="Acme",
    )

    match = CorrelationEngine().correlate(component, advisory)
    assert match is not None
    assert match.method == CorrelationMethod.FUZZY_BANNER
    assert match.status == CorrelationStatus.CANDIDATE
    assert match.confidence <= 0.69
    assert "analyst confirmation" in match.rationale


def test_known_fixed_purl_version_is_not_reintroduced_as_fuzzy_candidate():
    advisory = _advisory(
        [
            {
                "package": {
                    "ecosystem": "PyPI",
                    "name": "django",
                    "purl": "pkg:pypi/django",
                },
                "ranges": [{"events": [{"introduced": "4.2"}, {"fixed": "4.2.10"}]}],
            }
        ]
    )
    fixed = SoftwareComponent(
        engagement_id="eng-1",
        name="django",
        version="4.2.10",
        purl="pkg:pypi/django@4.2.10",
    )

    match = CorrelationEngine().correlate(fixed, advisory)
    assert match is None or match.status == CorrelationStatus.NOT_AFFECTED


def test_unsupported_ecosystem_version_semantics_stay_candidate():
    advisory = _advisory(
        [
            {
                "package": {
                    "ecosystem": "npm",
                    "name": "left-pad",
                    "purl": "pkg:npm/left-pad",
                },
                "ranges": [
                    {
                        "type": "ECOSYSTEM",
                        "events": [
                            {"introduced": "1.0.0-beta.1"},
                            {"fixed": "2.0.0 || >=3"},
                        ],
                    }
                ],
            }
        ]
    )
    component = SoftwareComponent(
        engagement_id="eng-1",
        name="left-pad",
        version="1.5.0",
        ecosystem="npm",
        purl="pkg:npm/left-pad@1.5.0",
    )

    match = CorrelationEngine().correlate(component, advisory)
    assert match is not None
    assert match.method == CorrelationMethod.PURL
    assert match.status == CorrelationStatus.CANDIDATE
