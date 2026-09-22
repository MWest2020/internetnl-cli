import io
import json
import pathlib

from internetnl_cli import gating
from internetnl_cli.findings import SCHEMA, build_document, render_findings

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _reply(domains, request_type="web", finished_date="2026-09-22T10:00:00+00:00"):
    return {
        "api_version": "2.7.0",
        "request": {
            "request_id": "e94251da69c54da7b16fc5202a69c5c2",
            "request_type": request_type,
            "status": "done",
            "finished_date": finished_date,
        },
        "domains": domains,
    }


def test_schema_field_is_pinned():
    assert build_document(_reply({}))["schema"] == SCHEMA


def test_domains_and_tests_sorted_alphabetically():
    reply = _reply(
        {
            "b.example": {"status": "ok", "results": {"categories": {}, "tests": {}}},
            "a.example": {
                "status": "ok",
                "results": {
                    "categories": {},
                    "tests": {
                        "z_test": {"status": "passed", "verdict": "good"},
                        "a_test": {"status": "passed", "verdict": "good"},
                    },
                },
            },
        }
    )
    doc = build_document(reply)
    assert [d["domain"] for d in doc["domains"]] == ["a.example", "b.example"]
    assert [t["test"] for t in doc["domains"][0]["results"]] == ["a_test", "z_test"]


def test_category_is_longest_matching_prefix():
    # No `category_groups` given -> the testname-prefix fallback rule.
    reply = _reply(
        {
            "example.nl": {
                "status": "ok",
                "results": {
                    "categories": {"web_https": {}, "web_https_dane": {}},
                    "tests": {
                        "web_https_dane_exist": {"status": "passed", "verdict": "good"},
                        "web_https_cert_chain": {"status": "passed", "verdict": "good"},
                    },
                },
            }
        }
    )
    by_test = {t["test"]: t["category"] for t in build_document(reply)["domains"][0]["results"]}
    assert by_test["web_https_dane_exist"] == "web_https_dane"
    assert by_test["web_https_cert_chain"] == "web_https"


def test_no_matching_category_is_null_and_test_still_appears():
    # No `category_groups` given -> the testname-prefix fallback rule.
    reply = _reply(
        {
            "example.nl": {
                "status": "ok",
                "results": {
                    "categories": {"web_https": {}},
                    "tests": {"web_ns_rpki_exists": {"status": "passed", "verdict": "good"}},
                },
            }
        }
    )
    entries = build_document(reply)["domains"][0]["results"]
    assert len(entries) == 1
    assert entries[0]["category"] is None
    assert entries[0]["test"] == "web_ns_rpki_exists"


def test_detail_is_always_null():
    reply = _reply(
        {
            "example.nl": {
                "status": "ok",
                "results": {"categories": {}, "tests": {"t": {"status": "passed", "verdict": "good"}}},
            }
        }
    )
    assert build_document(reply)["domains"][0]["results"][0]["detail"] is None


def test_error_status_and_verdict_carried_verbatim_distinct_from_failed():
    reply = _reply(
        {
            "example.nl": {
                "status": "ok",
                "results": {"categories": {}, "tests": {"t": {"status": "error", "verdict": "other"}}},
            }
        }
    )
    entry = build_document(reply)["domains"][0]["results"][0]
    assert entry["status"] == "error"
    assert entry["status"] != "failed"
    assert entry["verdict"] == "other"


def test_measured_at_is_request_finished_date_same_for_every_domain():
    reply = _reply(
        {
            "a.example": {"status": "ok", "results": {"categories": {}, "tests": {}}},
            "b.example": {"status": "ok", "results": {"categories": {}, "tests": {}}},
        },
        finished_date="2026-09-22T12:34:56+00:00",
    )
    doc = build_document(reply)
    assert {d["measured_at"] for d in doc["domains"]} == {"2026-09-22T12:34:56+00:00"}


def test_report_url_and_score_percent_passed_through_unmodified():
    reply = _reply(
        {
            "example.nl": {
                "status": "ok",
                "report": {"url": "https://instance.example/site/example.nl/1/"},
                "scoring": {"percentage": 83},
                "results": {"categories": {}, "tests": {}},
            }
        }
    )
    domain = build_document(reply)["domains"][0]
    assert domain["report_url"] == "https://instance.example/site/example.nl/1/"
    assert domain["score_percent"] == 83


def test_broken_domain_appears_with_its_status_and_empty_results():
    reply = _reply({"broken.example": {"status": "error"}})
    domain = build_document(reply)["domains"][0]
    assert domain["status"] == "error"
    assert domain["results"] == []
    assert domain["score_percent"] is None
    assert domain["report_url"] is None


def test_render_findings_writes_one_document_with_trailing_newline():
    doc = build_document(_reply({}))
    stream = io.StringIO()
    render_findings(doc, stream)
    text = stream.getvalue()
    assert text.endswith("\n")
    assert json.loads(text) == doc


def test_two_renders_of_the_same_batch_are_byte_identical():
    reply = _reply(
        {
            "b.example": {
                "status": "ok",
                "results": {"categories": {}, "tests": {"t": {"status": "passed", "verdict": "good"}}},
            },
            "a.example": {"status": "ok", "results": {"categories": {}, "tests": {}}},
        }
    )
    first = io.StringIO()
    second = io.StringIO()
    render_findings(build_document(reply), first)
    render_findings(build_document(reply), second)
    assert first.getvalue() == second.getvalue()


# --- golden fixtures: real measurements from 2026-09-22, see runs/01-export.md
# and runs/02-categorie-uit-metadata.md ---

METADATA = _load("metadata-report-20260922.json")


def test_web_fixture_matches_golden_findings_export():
    reply = _load("batch-v2-web-20260922.json")
    expected = _load("findings-v1-web-20260922.json")
    category_groups = gating.category_groups_from_metadata(METADATA, "web")
    assert build_document(reply, category_groups=category_groups) == expected


def test_mail_fixture_matches_golden_findings_export():
    reply = _load("batch-v2-mail-20260922.json")
    expected = _load("findings-v1-mail-20260922.json")
    category_groups = gating.category_groups_from_metadata(METADATA, "mail")
    assert build_document(reply, category_groups=category_groups) == expected


def test_metadata_placed_rpki_ns_variants_are_never_null_category():
    """Regression for run 02: `_category_for` used to take the longest key
    in a domain's own `results.categories` that prefixed the test name —
    that rule is blind to the `ns`/`mx_ns` infix RPKI's nameserver variants
    carry, so `web_ns_rpki_*`, `mail_ns_rpki_*` and `mail_mx_ns_rpki_*` fell
    to `category: null` even though the instance's own metadata hierarchy
    places every one of them under `web_rpki`/`mail_rpki`.
    """
    web_groups = gating.category_groups_from_metadata(METADATA, "web")
    mail_groups = gating.category_groups_from_metadata(METADATA, "mail")

    web_doc = build_document(_load("batch-v2-web-20260922.json"), category_groups=web_groups)
    mail_doc = build_document(_load("batch-v2-mail-20260922.json"), category_groups=mail_groups)

    web_by_test = {e["test"]: e["category"] for d in web_doc["domains"] for e in d["results"]}
    mail_by_test = {e["test"]: e["category"] for d in mail_doc["domains"] for e in d["results"]}

    assert web_by_test["web_ns_rpki_exists"] == "web_rpki"
    assert web_by_test["web_ns_rpki_valid"] == "web_rpki"
    assert mail_by_test["mail_ns_rpki_exists"] == "mail_rpki"
    assert mail_by_test["mail_ns_rpki_valid"] == "mail_rpki"
    assert mail_by_test["mail_mx_ns_rpki_exists"] == "mail_rpki"
    assert mail_by_test["mail_mx_ns_rpki_valid"] == "mail_rpki"
