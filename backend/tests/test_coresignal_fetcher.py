"""Unit tests for backend/pipeline/coresignal.py.

HTTP is mocked — no real Coresignal calls. Focus:
  - Filter-value correctness (empirically-verified values, not doc values)
  - Fuzzy-match protection (rejects "Meta Power Solutions" for a "Meta" query)
  - Credit budget enforcement
  - Fail-soft on 5xx / timeouts
  - Schema mapping matches Greenhouse/Lever/Ashby fetcher shape
  - Dedup against existing Firestore IDs
"""
from unittest.mock import MagicMock, patch


def _mock_response(json_data, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.text = str(json_data)[:500]
    return r


class TestCanonicalMatchProtection:
    """The killer bug the Coresignal probe surfaced: fuzzy matching returns
    'Meta Power Solutions' for 'Meta' queries. Verify our guard catches it."""

    def test_exact_match_accepted(self):
        from backend.pipeline.coresignal import _accept_company
        assert _accept_company("Meta", "Meta") is True
        assert _accept_company("Anthropic", "Anthropic") is True

    def test_case_insensitive(self):
        from backend.pipeline.coresignal import _accept_company
        assert _accept_company("meta", "META") is True

    def test_common_suffixes_stripped(self):
        from backend.pipeline.coresignal import _accept_company
        assert _accept_company("Meta", "Meta Inc.") is True
        assert _accept_company("Google", "Google LLC") is True

    def test_rejects_fuzzy_impostors(self):
        """These are the actual results the empirical probe returned."""
        from backend.pipeline.coresignal import _accept_company
        assert _accept_company("Meta", "Meta Power Solutions") is False
        assert _accept_company("Citadel", "Citadel Healthcare") is False
        assert _accept_company("Morgan Stanley", "Stanley Automotive") is False
        assert _accept_company("JPMorgan Chase", "Chase Brexton Health Care") is False
        assert _accept_company("McKinsey", "Vulcan Materials Company") is False

    def test_rejects_empty(self):
        from backend.pipeline.coresignal import _accept_company
        assert _accept_company("Meta", "") is False
        assert _accept_company("", "Meta") is False


class TestBuildDiscoveryQuery:
    def test_uses_empirically_verified_seniority_values(self):
        """Docs said 'intern' — probe showed real value is 'internship'."""
        from backend.pipeline.coresignal import _build_discovery_query

        q = _build_discovery_query()
        seniority_clauses = q["query"]["bool"]["must"][1]["bool"]["should"]
        values = [c["match"]["seniority"] for c in seniority_clauses]
        assert "internship" in values
        assert "entry" in values
        # These are what the docs suggested — reject them
        assert "intern" not in values
        assert "new_grad" not in values

    def test_country_us_native_filter(self):
        from backend.pipeline.coresignal import _build_discovery_query

        q = _build_discovery_query()
        must = q["query"]["bool"]["must"]
        country_clause = next((c for c in must if "match" in c and "country" in c["match"]), None)
        assert country_clause is not None
        assert country_clause["match"]["country"] == "United States"

    def test_company_names_from_target_list(self):
        from backend.pipeline.coresignal import _build_discovery_query, TARGET_COMPANIES

        q = _build_discovery_query()
        must = q["query"]["bool"]["must"]
        company_bool = next((c for c in must if "bool" in c and "should" in c["bool"] and
                             any("company_name" in s.get("match", {}) for s in c["bool"]["should"])), None)
        assert company_bool is not None
        company_values = [s["match"]["company_name"] for s in company_bool["bool"]["should"]]
        for target in TARGET_COMPANIES:
            assert target in company_values


class TestBuildTechStackQuery:
    def test_uses_nested_query_path(self):
        from backend.pipeline.coresignal import _build_tech_stack_query
        q = _build_tech_stack_query("pytorch")
        must = q["query"]["bool"]["must"]
        nested = next((c for c in must if "nested" in c), None)
        assert nested is not None
        assert nested["nested"]["path"] == "company_technologies"
        # Verify the tech name is passed through unchanged
        assert nested["nested"]["query"]["match"]["company_technologies.technology"] == "pytorch"


class TestSearch:
    def test_returns_list_of_ints(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "post", return_value=_mock_response([1, 2, 3])):
            result = coresignal._search("fake_key", {"query": {}}, "test")
        assert result == [1, 2, 3]

    def test_filters_non_int_shapes(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "post", return_value=_mock_response([1, "bad", 2, None])):
            result = coresignal._search("fake_key", {"query": {}}, "test")
        assert result == [1, 2]

    def test_returns_empty_on_http_error(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "post", return_value=_mock_response({}, status=500)):
            result = coresignal._search("fake_key", {"query": {}}, "test")
        assert result == []

    def test_returns_empty_on_exception(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "post", side_effect=RuntimeError("timeout")):
            result = coresignal._search("fake_key", {"query": {}}, "test")
        assert result == []

    def test_returns_empty_when_response_is_dict(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "post", return_value=_mock_response({"error": "bad"})):
            result = coresignal._search("fake_key", {"query": {}}, "test")
        assert result == []


class TestCollect:
    def test_returns_dict(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "get", return_value=_mock_response({"id": 1, "title": "SWE"})):
            result = coresignal._collect("fake_key", 1)
        assert result == {"id": 1, "title": "SWE"}

    def test_returns_none_on_404(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "get", return_value=_mock_response({}, status=404)):
            result = coresignal._collect("fake_key", 1)
        assert result is None

    def test_returns_none_on_exception(self):
        from backend.pipeline import coresignal
        with patch.object(coresignal.requests, "get", side_effect=RuntimeError("timeout")):
            result = coresignal._collect("fake_key", 1)
        assert result is None


class TestNormalizeCoresignalJob:
    def _sample(self, **overrides):
        base = {
            "id": 195330167,
            "title": "Web Producer, CMS Publishing",
            "company_name": "Anthropic",
            "location": "New York, NY",
            "remote": False,
            "seniority": "Entry level",
            "date_posted": "2026-06-13",
            "url": "https://anthropic.com/careers/195330167",
            "description": "Build tools",
            "employment_statuses": ["full_time"],
            "min_annual_salary_usd": 120000,
            "max_annual_salary_usd": 180000,
        }
        base.update(overrides)
        return base

    def test_produces_standard_job_dict_shape(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job

        result = _normalize_coresignal_job(self._sample())
        assert result is not None
        for field in ("job_id", "source", "title", "company", "location",
                      "remote", "description_raw", "apply_url", "posted_at",
                      "salary_min", "salary_max", "salary_period"):
            assert field in result, f"missing field: {field}"

    def test_job_id_prefixed_with_coresignal(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample(id=42))
        assert result["job_id"] == "coresignal_42"

    def test_source_is_coresignal(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample())
        assert result["source"] == "coresignal"

    def test_maps_employment_type_correctly(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job

        result = _normalize_coresignal_job(self._sample(employment_statuses=["full_time"]))
        assert result["_employment_type"] == "FULLTIME"

        result = _normalize_coresignal_job(self._sample(employment_statuses=["internship"]))
        assert result["_employment_type"] == "INTERNSHIP"

    def test_rejects_junk_salary(self):
        """Empirical probe found $290M/year parsing bug. Must be rejected."""
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample(
            min_annual_salary_usd=290_172_480,
            max_annual_salary_usd=290_172_480,
        ))
        assert result["salary_min"] is None
        assert result["salary_max"] is None

    def test_accepts_plausible_salary(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample())
        assert result["salary_min"] == 120000.0
        assert result["salary_max"] == 180000.0

    def test_returns_none_when_title_missing(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample(title=""))
        assert result is None

    def test_returns_none_when_company_missing(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample(company_name=""))
        assert result is None

    def test_returns_none_when_id_missing(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample(id=None))
        assert result is None

    def test_falls_back_to_country_when_location_missing(self):
        from backend.pipeline.coresignal import _normalize_coresignal_job
        result = _normalize_coresignal_job(self._sample(location="", country="United States"))
        assert result["location"] == "United States"


class TestFetchAllCoresignal:
    def test_returns_empty_when_no_api_key(self):
        from backend.pipeline import coresignal
        with patch.dict("os.environ", {}, clear=True):
            result = coresignal.fetch_all_coresignal()
        assert result == []

    def test_dedup_skips_existing_job_ids(self):
        from backend.pipeline import coresignal

        # Search returns [1, 2, 3], but 2 is already in Firestore
        with patch.dict("os.environ", {"CORESIGNAL_API_KEY": "fake"}):
            with patch.object(coresignal, "_search", return_value=[1, 2, 3]):
                collect_calls = []
                def fake_collect(_key, jid):
                    collect_calls.append(jid)
                    return {
                        "id": jid,
                        "title": "SWE",
                        "company_name": "Anthropic",
                        "location": "SF",
                    }
                with patch.object(coresignal, "_collect", side_effect=fake_collect):
                    coresignal.fetch_all_coresignal(existing_job_ids={"coresignal_2"})

        # Only 1 and 3 should have been collected
        assert 2 not in collect_calls
        assert 1 in collect_calls
        assert 3 in collect_calls

    def test_enforces_collect_budget(self):
        from backend.pipeline import coresignal

        # Return way more IDs than the budget allows
        with patch.dict("os.environ", {"CORESIGNAL_API_KEY": "fake"}):
            with patch.object(coresignal, "_search", return_value=list(range(1, 50))):
                collect_calls = []
                def fake_collect(_key, jid):
                    collect_calls.append(jid)
                    return {
                        "id": jid,
                        "title": "SWE",
                        "company_name": "Anthropic",
                        "location": "SF",
                    }
                with patch.object(coresignal, "_collect", side_effect=fake_collect):
                    coresignal.fetch_all_coresignal()

        # Must not exceed COLLECT_BUDGET_PER_RUN
        assert len(collect_calls) <= coresignal.COLLECT_BUDGET_PER_RUN

    def test_rejects_fuzzy_match_companies(self):
        """The whole point of the fuzzy-match guard. If Coresignal returns
        'Meta Power Solutions' for a 'Meta' target, drop it."""
        from backend.pipeline import coresignal

        with patch.dict("os.environ", {"CORESIGNAL_API_KEY": "fake"}):
            with patch.object(coresignal, "_search", return_value=[1, 2]):
                def fake_collect(_key, jid):
                    if jid == 1:
                        return {
                            "id": 1,
                            "title": "CAD Drafter",
                            "company_name": "Meta Power Solutions",
                            "location": "Palm Beach Gardens, FL",
                        }
                    return {
                        "id": 2,
                        "title": "ML Engineer",
                        "company_name": "Meta",
                        "location": "Menlo Park, CA",
                    }
                with patch.object(coresignal, "_collect", side_effect=fake_collect):
                    result = coresignal.fetch_all_coresignal()

        # Only the real Meta job should survive
        assert len(result) == 1
        assert result[0]["company"] == "Meta"
