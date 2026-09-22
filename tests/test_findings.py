import io
import json
import pathlib
from datetime import datetime, timezone

from internetnl_cli import gating
from internetnl_cli.findings import SCHEMA, build_document, render_findings

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

GENERATED_AT = datetime(2026, 9, 22, 10, 0, 0, tzinfo=timezone.utc)
ENDPOINT_HOST = "netnl.example"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


def _build(reply, **kwargs):
    kwargs.setdefault("generated_at", GENERATED_AT)
    kwargs.setdefault("endpoint_host", ENDPOINT_HOST)
    return build_document(reply, **kwargs)


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
    assert _build(_reply({}))["schema"] == SCHEMA


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
    doc = _build(reply)
    assert [d["domain"] for d in doc["domains"]] == ["a.example", "b.example"]
    assert [t["test"] for t in doc["domains"][0]["results"]] == ["a_test", "z_test"]


def test_category_is_longest_matching_prefix():
    # No `categories_by_test` given -> the testname-prefix fallback rule.
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
    by_test = {t["test"]: t["category"] for t in _build(reply)["domains"][0]["results"]}
    assert by_test["web_https_dane_exist"] == "web_https_dane"
    assert by_test["web_https_cert_chain"] == "web_https"


def test_no_matching_category_is_null_and_test_still_appears():
    # No `categories_by_test` given -> the testname-prefix fallback rule.
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
    entries = _build(reply)["domains"][0]["results"]
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
    assert _build(reply)["domains"][0]["results"][0]["detail"] is None


def test_error_status_and_verdict_carried_verbatim_distinct_from_failed():
    reply = _reply(
        {
            "example.nl": {
                "status": "ok",
                "results": {"categories": {}, "tests": {"t": {"status": "error", "verdict": "other"}}},
            }
        }
    )
    entry = _build(reply)["domains"][0]["results"][0]
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
    doc = _build(reply)
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
    domain = _build(reply)["domains"][0]
    assert domain["report_url"] == "https://instance.example/site/example.nl/1/"
    assert domain["score_percent"] == 83


def test_broken_domain_appears_with_its_status_and_empty_results():
    reply = _reply({"broken.example": {"status": "error"}})
    domain = _build(reply)["domains"][0]
    assert domain["status"] == "error"
    assert domain["results"] == []
    assert domain["score_percent"] is None
    assert domain["report_url"] is None


def test_render_findings_writes_one_document_with_trailing_newline():
    doc = _build(_reply({}))
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
    render_findings(_build(reply), first)
    render_findings(_build(reply), second)
    assert first.getvalue() == second.getvalue()


# --- golden fixtures: real measurements from 2026-09-22, see runs/01-export.md,
# runs/02-categorie-uit-metadata.md and runs/03-hierarchie-is-drie-lagen.md ---

# `metadata-report-20260922.json` is the instance's actual `GET
# /metadata/report` reply (fetched from api.westerweel.work) — three layers
# deep (category -> subtestgroup -> test, four for RPKI's nameserver
# variants), not the flat category -> test shape an earlier run fabricated.
METADATA = _load("metadata-report-20260922.json")

# The real endpoint and the real moment these two exports were written (run
# 04-herkomst-in-de-kop.md): `generated_at` is each batch's own
# `finished_date`, truncated to whole seconds, since these golden files were
# written immediately after the batch finished.
REAL_ENDPOINT_HOST = "api.westerweel.work"
WEB_GENERATED_AT = datetime(2026, 9, 22, 10, 25, 16, tzinfo=timezone.utc)
MAIL_GENERATED_AT = datetime(2026, 9, 22, 10, 29, 6, tzinfo=timezone.utc)


def test_web_fixture_matches_golden_findings_export():
    reply = _load("batch-v2-web-20260922.json")
    expected = _load("findings-v1-web-20260922.json")
    categories_by_test = gating.category_by_test_from_metadata(METADATA, "web")
    doc = build_document(
        reply,
        generated_at=WEB_GENERATED_AT,
        endpoint_host=REAL_ENDPOINT_HOST,
        categories_by_test=categories_by_test,
    )
    assert doc == expected


def test_mail_fixture_matches_golden_findings_export():
    reply = _load("batch-v2-mail-20260922.json")
    expected = _load("findings-v1-mail-20260922.json")
    categories_by_test = gating.category_by_test_from_metadata(METADATA, "mail")
    doc = build_document(
        reply,
        generated_at=MAIL_GENERATED_AT,
        endpoint_host=REAL_ENDPOINT_HOST,
        categories_by_test=categories_by_test,
    )
    assert doc == expected


def test_metadata_placed_rpki_ns_variants_are_never_null_category():
    """Regression for run 02: `_category_for` used to take the longest key
    in a domain's own `results.categories` that prefixed the test name —
    that rule is blind to the `ns`/`mx_ns` infix RPKI's nameserver variants
    carry, so `web_ns_rpki_*`, `mail_ns_rpki_*` and `mail_mx_ns_rpki_*` fell
    to `category: null` even though the instance's own metadata hierarchy
    places every one of them under `web_rpki`/`mail_rpki`.
    """
    web_by_test = gating.category_by_test_from_metadata(METADATA, "web")
    mail_by_test = gating.category_by_test_from_metadata(METADATA, "mail")

    web_doc = _build(_load("batch-v2-web-20260922.json"), categories_by_test=web_by_test)
    mail_doc = _build(_load("batch-v2-mail-20260922.json"), categories_by_test=mail_by_test)

    web_by_test_result = {e["test"]: e["category"] for d in web_doc["domains"] for e in d["results"]}
    mail_by_test_result = {e["test"]: e["category"] for d in mail_doc["domains"] for e in d["results"]}

    assert web_by_test_result["web_ns_rpki_exists"] == "web_rpki"
    assert web_by_test_result["web_ns_rpki_valid"] == "web_rpki"
    assert mail_by_test_result["mail_ns_rpki_exists"] == "mail_rpki"
    assert mail_by_test_result["mail_ns_rpki_valid"] == "mail_rpki"
    assert mail_by_test_result["mail_mx_ns_rpki_exists"] == "mail_rpki"
    assert mail_by_test_result["mail_mx_ns_rpki_valid"] == "mail_rpki"


def test_real_hierarchy_leaves_no_test_uncategorised():
    """Regression for run 03: the fabricated metadata fixture was flat
    (category -> test directly), so the real three-layer hierarchy (category
    -> subtestgroup -> test) made `category_by_test_from_metadata` miss 14
    of 38 web tests and 12 of the mail tests. Against the real hierarchy,
    every measured test must resolve to a category.

    This rebuilds from the raw batch replies; it does not read the golden
    files. Drift in a committed golden file is caught by
    `test_web_fixture_matches_golden_findings_export` and its mail twin,
    which compare the whole document.
    """
    web_by_test = gating.category_by_test_from_metadata(METADATA, "web")
    mail_by_test = gating.category_by_test_from_metadata(METADATA, "mail")

    web_doc = _build(_load("batch-v2-web-20260922.json"), categories_by_test=web_by_test)
    mail_doc = _build(_load("batch-v2-mail-20260922.json"), categories_by_test=mail_by_test)

    web_nulls = [e["test"] for d in web_doc["domains"] for e in d["results"] if e["category"] is None]
    mail_nulls = [e["test"] for d in mail_doc["domains"] for e in d["results"] if e["category"] is None]

    assert web_nulls == []
    assert mail_nulls == []


# --- provenance header: generated_at / source (runs/04-herkomst-in-de-kop.md) ---


def test_generated_at_is_rfc3339_utc_from_the_injected_clock():
    # No `datetime.now()` inside build_document — the caller's clock, verbatim.
    doc = _build(_reply({}), generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert doc["generated_at"] == "2026-01-02T03:04:05Z"


def test_source_endpoint_is_the_injected_host_not_a_url():
    doc = _build(_reply({}), endpoint_host="netnl.westerweel.work")
    assert doc["source"]["endpoint"] == "netnl.westerweel.work"


def test_source_api_label_is_the_fixed_name_plus_major_api_version():
    reply = _reply({})
    reply["api_version"] = "2.7.0"
    assert _build(reply)["source"]["api"] == "internet.nl batch v2"


def test_source_request_id_is_not_empty_for_a_batch_that_has_one():
    # Regression for run 04: request_id exists on the wire (reply.request.
    # request_id) and must not be dropped from the export.
    reply = _reply({})
    reply["request"]["request_id"] = "b2dda607433522760b51faecbcb23c87"
    request_id = _build(reply)["source"]["request_id"]
    assert request_id
    assert request_id == "b2dda607433522760b51faecbcb23c87"


def test_two_renders_with_the_same_generated_at_are_byte_identical():
    reply = _reply({"a.example": {"status": "ok", "results": {"categories": {}, "tests": {}}}})
    first = io.StringIO()
    second = io.StringIO()
    render_findings(_build(reply, generated_at=GENERATED_AT), first)
    render_findings(_build(reply, generated_at=GENERATED_AT), second)
    assert first.getvalue() == second.getvalue()
