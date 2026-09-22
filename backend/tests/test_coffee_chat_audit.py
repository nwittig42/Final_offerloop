"""
Tests for coffee chat prep audit fixes.
Covers: credit refund on failure, detect_commonality PDL field handling,
status enum sync, education_strings type safety, LinkedIn URL normalization,
news eligibility false positives, partial failure tracking, error consistency.
"""

import inspect
import re
import pytest
from unittest.mock import patch, MagicMock


# =============================================================================
# BUG 2 — Credit refund on background failure
# =============================================================================

class TestCreditRefundOnFailure:
    """Verify credits are refunded when background processing fails."""

    def test_refund_import_in_route(self):
        """Route file imports refund_credits_atomic."""
        import app.routes.coffee_chat_prep as mod
        source = inspect.getsource(mod)
        assert "refund_credits_atomic" in source

    def test_background_worker_has_credits_charged_param(self):
        """Background worker accepts credits_charged parameter."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
        sig = inspect.signature(process_coffee_chat_prep_background)
        assert "credits_charged" in sig.parameters
        from app.config import COFFEE_CHAT_CREDITS
        default = sig.parameters["credits_charged"].default
        assert default == COFFEE_CHAT_CREDITS

    def test_refund_on_exception_in_source(self):
        """Background worker calls refund on exception."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
        source = inspect.getsource(process_coffee_chat_prep_background)
        assert "refund_credits_atomic(user_id, credits_charged" in source
        assert '"creditsRefunded": True' in source

    def test_refund_on_enrichment_failure_in_source(self):
        """Background worker refunds credits when PDL enrichment fails."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
        source = inspect.getsource(process_coffee_chat_prep_background)
        assert "coffee_chat_enrichment_failed" in source

    @patch("app.routes.coffee_chat_prep.get_db")
    @patch("app.routes.coffee_chat_prep.enrich_linkedin_profile", return_value=None)
    @patch("app.routes.coffee_chat_prep.refund_credits_atomic", return_value=(True, 150))
    def test_enrichment_failure_triggers_refund(self, mock_refund, mock_enrich, mock_db):
        """When PDL returns None, credits are refunded."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background

        mock_prep_ref = MagicMock()
        mock_db.return_value.collection.return_value.document.return_value \
            .collection.return_value.document.return_value = mock_prep_ref

        process_coffee_chat_prep_background(
            prep_id="test-123",
            linkedin_url="https://linkedin.com/in/test",
            user_id="user-456",
            resume_text="Some resume text here with enough length to pass checks",
        )

        from app.config import COFFEE_CHAT_CREDITS
        mock_refund.assert_called_once_with("user-456", COFFEE_CHAT_CREDITS, "coffee_chat_enrichment_failed")
        # Verify status was set to failed with refund flag
        update_call = mock_prep_ref.update.call_args_list[-1]
        update_data = update_call[0][0]
        assert update_data["status"] == "failed"
        assert update_data["creditsRefunded"] is True

    @patch("app.routes.coffee_chat_prep.get_db")
    @patch("app.routes.coffee_chat_prep.enrich_linkedin_profile", side_effect=RuntimeError("API down"))
    @patch("app.routes.coffee_chat_prep.refund_credits_atomic", return_value=(True, 150))
    def test_exception_triggers_refund(self, mock_refund, mock_enrich, mock_db):
        """When background worker throws, credits are refunded."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background

        mock_prep_ref = MagicMock()
        mock_db.return_value.collection.return_value.document.return_value \
            .collection.return_value.document.return_value = mock_prep_ref

        process_coffee_chat_prep_background(
            prep_id="test-123",
            linkedin_url="https://linkedin.com/in/test",
            user_id="user-456",
            resume_text="Some resume text here",
        )

        from app.config import COFFEE_CHAT_CREDITS
        mock_refund.assert_called_once_with("user-456", COFFEE_CHAT_CREDITS, "coffee_chat_prep_failure")


# =============================================================================
# BUG 6 — detect_commonality PDL field support
# =============================================================================

class TestDetectCommonalityPDL:
    """Verify detect_commonality works with PDL-enriched field names."""

    def test_university_match_via_education_array(self):
        """Should match university from educationArray (PDL format)."""
        from app.utils.coffee_chat_prep import detect_commonality
        user_info = {"university": "Stanford University"}
        contact = {
            "educationArray": [
                {"school": "Stanford University", "degree": "BS", "major": "CS"}
            ],
            "company": "Google",
        }
        ctype, details = detect_commonality(user_info, contact, "")
        assert ctype == "university"
        assert details["university"] == "Stanford University"

    def test_university_match_via_legacy_fields(self):
        """Should still match via legacy College field."""
        from app.utils.coffee_chat_prep import detect_commonality
        user_info = {"university": "Harvard University"}
        contact = {"College": "Harvard University", "Company": "McKinsey"}
        ctype, _ = detect_commonality(user_info, contact, "")
        assert ctype == "university"

    def test_company_match_via_pdl_lowercase(self):
        """Should match company from PDL lowercase 'company' field."""
        from app.utils.coffee_chat_prep import detect_commonality
        user_info = {"university": "MIT"}
        contact = {"company": "Google", "educationArray": []}
        resume = "Software Engineer Intern at Google, Mountain View"
        ctype, details = detect_commonality(user_info, contact, resume)
        assert ctype == "company"
        assert "Google" in details["company"]

    def test_hometown_match_via_pdl_location(self):
        """Should match hometown from PDL lowercase 'location' field."""
        from app.utils.coffee_chat_prep import detect_commonality
        user_info = {"university": "NYU"}
        # The match checks: user_hometown.lower() in contact_city.lower()
        # extract_hometown_from_resume returns "Los Angeles, CA" from resume
        # So contact location must contain "los angeles, ca"
        contact = {"location": "Los Angeles, CA", "company": "Netflix", "educationArray": []}
        resume = "Based in Los Angeles, CA\nSoftware Engineer at Startup"
        ctype, details = detect_commonality(user_info, contact, resume)
        assert ctype == "hometown"
        assert "Los Angeles" in details["hometown"]

    def test_general_fallback_when_no_match(self):
        """Should return 'general' when nothing matches."""
        from app.utils.coffee_chat_prep import detect_commonality
        user_info = {"university": "MIT"}
        contact = {"company": "Apple", "educationArray": [{"school": "Stanford"}]}
        ctype, details = detect_commonality(user_info, contact, "Worked at Startup in Boston")
        assert ctype == "general"
        assert details == {}

    def test_empty_education_array(self):
        """Should handle empty educationArray without crashing."""
        from app.utils.coffee_chat_prep import detect_commonality
        user_info = {"university": "USC"}
        contact = {"educationArray": [], "company": "Meta"}
        ctype, _ = detect_commonality(user_info, contact, "")
        assert ctype == "general"

    def test_mixed_field_formats(self):
        """Should handle contact with both PDL and legacy fields."""
        from app.utils.coffee_chat_prep import detect_commonality
        user_info = {"university": "Duke University"}
        contact = {
            "educationArray": [{"school": "Duke University", "degree": "MBA"}],
            "College": "Duke University",
            "company": "Goldman Sachs",
        }
        ctype, _ = detect_commonality(user_info, contact, "")
        assert ctype == "university"


# =============================================================================
# BUG 1 — Model status enum sync
# =============================================================================

class TestModelStatusEnum:
    """Verify model status enum matches route stage names."""

    def test_valid_route_stages(self):
        """All stages used in route should be valid in model."""
        from app.models.coffee_chat_prep import validate_prep_status
        route_stages = ["processing", "enriching", "researching", "analyzing",
                        "generating", "building", "completed", "failed"]
        for stage in route_stages:
            assert validate_prep_status(stage), f"Stage '{stage}' should be valid"

    def test_old_stages_invalid(self):
        """Old incompatible stages should not be valid."""
        from app.models.coffee_chat_prep import validate_prep_status
        assert not validate_prep_status("enriching_profile")
        assert not validate_prep_status("fetching_news")
        assert not validate_prep_status("generating_content")
        assert not validate_prep_status("generating_pdf")


# =============================================================================
# BUG 5 — education_strings type safety
# =============================================================================

class TestEducationStringsTypeSafety:
    """Verify education_strings handling in background worker."""

    def test_education_strings_handles_all_types_in_source(self):
        """Source code handles str, list, and dict fallbacks for education."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
        source = inspect.getsource(process_coffee_chat_prep_background)
        assert "isinstance(fallback, str)" in source
        assert "isinstance(fallback, list)" in source
        assert "isinstance(e, dict)" in source


# =============================================================================
# BUG 4 — No double Firestore read
# =============================================================================

class TestNoDoubleFirestoreRead:
    """Verify redundant Firestore read was removed from create route."""

    def test_no_second_get_after_usage_reset(self):
        """create_coffee_chat_prep should not re-fetch user_doc after usage check."""
        from app.routes.coffee_chat_prep import create_coffee_chat_prep
        source = inspect.getsource(create_coffee_chat_prep)
        # After check_and_reset_usage, should NOT have another user_ref.get()
        # Count occurrences of user_ref.get() — should be exactly 1
        # (the initial read)
        get_calls = source.count("user_ref.get()")
        # There was a double-get bug before; now should only be the initial one
        # Also acceptable: 0 if it was refactored differently
        assert get_calls <= 1, f"Expected at most 1 user_ref.get() call, found {get_calls}"


# =============================================================================
# BUG 3 — Consistent error handling
# =============================================================================

class TestErrorHandlingConsistency:
    """Verify error handling is consistent across endpoints."""

    def test_get_all_returns_json_on_error(self):
        """get_all_coffee_chat_preps should return JSON, not raise."""
        from app.routes.coffee_chat_prep import get_all_coffee_chat_preps
        source = inspect.getsource(get_all_coffee_chat_preps)
        # Should NOT raise OfferloopException
        assert "raise OfferloopException" not in source
        # Should return JSON with empty preps
        assert '"preps": []' in source or "\"preps\": []" in source

    def test_history_returns_json_on_error(self):
        """get_coffee_chat_history also returns JSON on error."""
        from app.routes.coffee_chat_prep import get_coffee_chat_history
        source = inspect.getsource(get_coffee_chat_history)
        assert '"history": []' in source or "\"history\": []" in source


# =============================================================================
# W9 — News eligibility "ai" false positive fix
# =============================================================================

class TestNewsEligibilityFalsePositives:
    """Verify 'ai' rejection doesn't catch 'maintain', 'railway', etc."""

    def test_maintain_not_rejected_same_company(self):
        """News about maintaining infrastructure with company name should not be rejected."""
        from app.services.coffee_chat import _is_news_eligible
        item = {
            "title": "PipelineCo to maintain aging pipeline infrastructure",
            "snippet": "Maintenance program expands to cover 500 miles of pipeline"
        }
        # Same company mentioned — should pass
        assert _is_news_eligible(item, "PipelineCo", "industrial_engineering") is True

    def test_railway_not_rejected_peer(self):
        """Railway news with peer indicators should not be rejected."""
        from app.services.coffee_chat import _is_news_eligible
        item = {
            "title": "Railway expansion engineering project approved",
            "snippet": "Industrial contractor wins new railway manufacturing contract"
        }
        assert _is_news_eligible(item, "RailCo", "industrial_engineering") is True

    def test_sustainability_not_rejected_same_company(self):
        """Sustainability news mentioning the company should not be rejected."""
        from app.services.coffee_chat import _is_news_eligible
        item = {
            "title": "PlantCo sustainability initiative at manufacturing plant",
            "snippet": "The plant aims to reduce emissions through process improvements"
        }
        assert _is_news_eligible(item, "PlantCo", "industrial_engineering") is True

    def test_actual_ai_still_rejected(self):
        """Actual AI news should still be rejected for non-company items."""
        from app.services.coffee_chat import _is_news_eligible
        item = {
            "title": "New AI startup raises $50M for machine learning platform",
            "snippet": "The AI company focuses on artificial intelligence solutions"
        }
        assert _is_news_eligible(item, "OtherCo", "general") is False

    def test_ai_in_company_name_still_allowed(self):
        """If company is mentioned alongside AI, it should still be allowed."""
        from app.services.coffee_chat import _is_news_eligible
        item = {
            "title": "TechCorp launches AI division for manufacturing",
            "snippet": "TechCorp integrates ai into their engineering operations"
        }
        # Same company — should be allowed
        assert _is_news_eligible(item, "TechCorp", "industrial_engineering") is True

    def test_ml_word_boundary(self):
        """'ml' should not match 'html' or 'xml'."""
        from app.services.coffee_chat import _is_news_eligible
        item = {
            "title": "Company updates HTML documentation portal",
            "snippet": "New XML format for engineering specifications"
        }
        # Company mentioned, no actual ML content
        assert _is_news_eligible(item, "Company", "general") is True


# =============================================================================
# W5 — LinkedIn URL normalization
# =============================================================================

class TestLinkedInURLNormalization:
    """Verify LinkedIn URL cleanup in create route."""

    def test_url_normalization_in_source(self):
        """Route strips trailing slashes, query params, and fragments."""
        from app.routes.coffee_chat_prep import create_coffee_chat_prep
        source = inspect.getsource(create_coffee_chat_prep)
        assert '.rstrip("/")' in source
        assert '.split("?")[0]' in source
        assert '.split("#")[0]' in source


# =============================================================================
# W1 — Partial failure tracking
# =============================================================================

class TestPartialFailureTracking:
    """Verify AI generation partial failures are logged and stored."""

    def test_partial_failures_tracked_in_source(self):
        """Background worker tracks which AI sections failed."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
        source = inspect.getsource(process_coffee_chat_prep_background)
        assert "partial_failures" in source
        assert '"common_ground"' in source
        assert '"questions"' in source
        assert '"cheat_sheet"' in source
        assert '"strategy"' in source

    def test_partial_failures_stored_in_firestore(self):
        """Partial failures are included in completion update."""
        from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
        source = inspect.getsource(process_coffee_chat_prep_background)
        assert '"partialFailures"' in source or "partialFailures" in source


# =============================================================================
# build_coffee_chat_user_context tests (coverage gap)
# =============================================================================

class TestBuildCoffeeChatUserContext:
    """Test user context builder from parsed resume."""

    def test_full_parsed_resume(self):
        """Should extract all fields from a fully parsed resume."""
        from app.utils.users import build_coffee_chat_user_context
        parsed = {
            "name": "Jane Doe",
            "education": {
                "university": "MIT",
                "major": "Computer Science",
                "minor": "Math",
                "graduation": "May 2025",
                "gpa": "3.9",
                "honors": ["Dean's List"],
            },
            "experience": [
                {
                    "company": "Google",
                    "title": "Software Engineer Intern",
                    "dates": "Jun-Aug 2024",
                    "bullets": ["Built ML pipeline", "Improved latency 30%"],
                }
            ],
            "projects": [
                {
                    "name": "ChatBot",
                    "description": "An AI chatbot",
                    "technologies": ["Python", "Flask"],
                }
            ],
            "skills": {
                "programming_languages": ["Python", "Java"],
                "tools_frameworks": ["React"],
                "databases": ["PostgreSQL"],
                "cloud_devops": ["AWS"],
                "core_skills": [],
                "soft_skills": ["Leadership"],
                "languages": ["English", "Spanish"],
            },
            "extracurriculars": [
                {"activity": "Robotics Club", "role": "President"}
            ],
            "awards": ["Hackathon Winner"],
            "objective": "Seeking SWE role at top tech company",
        }
        ctx = build_coffee_chat_user_context(parsed)
        assert ctx["name"] == "Jane Doe"
        assert ctx["university"] == "MIT"
        assert ctx["major"] == "Computer Science"
        assert ctx["minor"] == "Math"
        assert ctx["year"] == "May 2025"
        assert ctx["gpa"] == "3.9"
        assert "Python" in ctx["skills"]
        assert "React" in ctx["skills"]
        assert "Leadership" in ctx["skills"]
        assert len(ctx["languages"]) == 2
        assert ctx["experiences"][0]["company"] == "Google"
        assert ctx["experiences"][0]["type"] == "internship"
        assert ctx["projects"][0]["name"] == "ChatBot"
        assert "President — Robotics Club" in ctx["clubs"]
        assert "Hackathon Winner" in ctx["awards"]
        assert ctx["careerGoals"] == "Seeking SWE role at top tech company"

    def test_empty_parsed_resume(self):
        """Should return safe defaults for empty dict."""
        from app.utils.users import build_coffee_chat_user_context
        ctx = build_coffee_chat_user_context({})
        assert ctx["name"] == ""
        assert ctx["skills"] == []
        assert ctx["experiences"] == []
        assert ctx["projects"] == []

    def test_profile_fallback(self):
        """Should fall back to user_profile when resume fields are empty."""
        from app.utils.users import build_coffee_chat_user_context
        parsed = {"name": ""}
        profile = {
            "displayName": "John Smith",
            "university": "Stanford",
            "major": "Finance",
            "graduationYear": "2026",
        }
        ctx = build_coffee_chat_user_context(parsed, profile)
        assert ctx["name"] == "John Smith"
        assert ctx["university"] == "Stanford"
        assert ctx["major"] == "Finance"
        assert ctx["year"] == "2026"

    def test_skills_as_list(self):
        """Should handle legacy skills format (list instead of dict)."""
        from app.utils.users import build_coffee_chat_user_context
        parsed = {"skills": ["Python", "Java", "SQL"]}
        ctx = build_coffee_chat_user_context(parsed)
        assert ctx["skills"] == ["Python", "Java", "SQL"]


# =============================================================================
# _empty_coffee_chat_user_context tests
# =============================================================================

class TestEmptyCoffeeChatUserContext:
    """Test empty user context factory."""

    def test_returns_all_expected_keys(self):
        from app.utils.users import _empty_coffee_chat_user_context
        ctx = _empty_coffee_chat_user_context()
        expected_keys = {
            "name", "university", "major", "minor", "year", "gpa",
            "skills", "interests", "clubs", "awards", "languages",
            "experiences", "projects", "careerGoals", "targetIndustries",
            "targetRoles",
        }
        assert set(ctx.keys()) == expected_keys

    def test_lists_are_empty(self):
        from app.utils.users import _empty_coffee_chat_user_context
        ctx = _empty_coffee_chat_user_context()
        for key in ["skills", "interests", "clubs", "awards", "languages",
                     "experiences", "projects", "targetIndustries", "targetRoles"]:
            assert ctx[key] == [], f"{key} should be empty list"

    def test_strings_are_empty(self):
        from app.utils.users import _empty_coffee_chat_user_context
        ctx = _empty_coffee_chat_user_context()
        for key in ["name", "university", "major", "minor", "year", "careerGoals"]:
            assert ctx[key] == "", f"{key} should be empty string"


# =============================================================================
# _parse_strategy_sections tests (PDF parsing)
# =============================================================================

class TestParseStrategySections:
    """Test strategy markdown parsing for PDF template."""

    def test_parses_do_items(self):
        from app.services.pdf_builder import _parse_strategy_sections
        md = """**CONVERSATION FLOW**
Opening (0-2 min): Start with shared USC connection.

**DO THIS**
- Reference their Intel experience
- Ask about LTE protocols
- Mention your startup
- Send a follow-up within 24 hours

**AVOID THIS**
- Generic questions
- Talking about salary
- Being too formal
- Asking about exit opps
"""
        result = _parse_strategy_sections(md)
        assert len(result["do_items"]) == 4
        assert "Reference their Intel experience" in result["do_items"]
        assert len(result["avoid_items"]) == 4
        assert "Generic questions" in result["avoid_items"]
        assert result["flow_html"]  # Should have some HTML content

    def test_empty_strategy(self):
        from app.services.pdf_builder import _parse_strategy_sections
        result = _parse_strategy_sections("")
        assert result["do_items"] == []
        assert result["avoid_items"] == []
        assert result["flow_html"] == ""

    def test_none_strategy(self):
        from app.services.pdf_builder import _parse_strategy_sections
        result = _parse_strategy_sections(None)
        assert result["do_items"] == []
        assert result["avoid_items"] == []

    def test_bullet_variants(self):
        """Should handle both - and • bullet formats."""
        from app.services.pdf_builder import _parse_strategy_sections
        md = """Some flow content.

**DO THIS**
• Item one
• Item two
- Item three
- Item four

**AVOID THIS**
- Avoid one
• Avoid two
- Avoid three
- Avoid four
"""
        result = _parse_strategy_sections(md)
        assert len(result["do_items"]) == 4
        assert len(result["avoid_items"]) == 4


# =============================================================================
# _md_to_html tests
# =============================================================================

class TestMdToHtml:
    """Test markdown to HTML conversion for PDFs."""

    def test_empty_input(self):
        from app.services.pdf_builder import _md_to_html
        assert "Not available" in _md_to_html("")

    def test_none_input(self):
        from app.services.pdf_builder import _md_to_html
        assert "Not available" in _md_to_html(None)

    def test_strips_code_fences(self):
        from app.services.pdf_builder import _md_to_html
        result = _md_to_html("```markdown\n# Hello\n```")
        assert "```" not in result
        assert "Hello" in result

    def test_basic_markdown(self):
        from app.services.pdf_builder import _md_to_html
        result = _md_to_html("**Bold** text")
        assert "<strong>" in result or "<b>" in result


# =============================================================================
# Tier limits for coffee chat
# =============================================================================

class TestCoffeeChatTierLimits:
    """Verify tier config has correct limits."""

    def test_free_tier_limit(self):
        from app.config import TIER_CONFIGS
        assert TIER_CONFIGS["free"]["coffee_chat_preps"] == 3

    def test_pro_tier_limit(self):
        from app.config import TIER_CONFIGS
        assert TIER_CONFIGS["pro"]["coffee_chat_preps"] == 10

    def test_elite_tier_unlimited(self):
        from app.config import TIER_CONFIGS
        assert TIER_CONFIGS["elite"]["coffee_chat_preps"] == "unlimited"

    def test_credit_cost(self):
        from app.config import COFFEE_CHAT_CREDITS
        assert COFFEE_CHAT_CREDITS == 30


# =============================================================================
# can_access_feature for coffee_chat_prep
# =============================================================================

class TestCanAccessCoffeeChat:
    """Verify feature access checks for coffee chat prep."""

    def test_free_tier_within_limit(self):
        from app.services.auth import can_access_feature
        from app.config import TIER_CONFIGS
        user_data = {"coffeeChatPrepsUsed": 2}
        allowed, reason = can_access_feature("free", "coffee_chat_prep", user_data, TIER_CONFIGS["free"])
        assert allowed is True

    def test_free_tier_at_limit(self):
        from app.services.auth import can_access_feature
        from app.config import TIER_CONFIGS
        user_data = {"coffeeChatPrepsUsed": 3}
        allowed, reason = can_access_feature("free", "coffee_chat_prep", user_data, TIER_CONFIGS["free"])
        assert allowed is False
        assert "limit" in reason.lower()

    def test_elite_tier_unlimited(self):
        from app.services.auth import can_access_feature
        from app.config import TIER_CONFIGS
        user_data = {"coffeeChatPrepsUsed": 999}
        allowed, _ = can_access_feature("elite", "coffee_chat_prep", user_data, TIER_CONFIGS["elite"])
        assert allowed is True


# =============================================================================
# validate_prep_status
# =============================================================================

class TestValidatePrepStatus:
    """Verify status validation covers all used stages."""

    def test_all_processing_stages_valid(self):
        from app.models.coffee_chat_prep import validate_prep_status
        for stage in ["pending", "processing", "enriching", "researching",
                       "analyzing", "generating", "building", "completed", "failed"]:
            assert validate_prep_status(stage), f"{stage} should be valid"

    def test_unknown_stage_invalid(self):
        from app.models.coffee_chat_prep import validate_prep_status
        assert not validate_prep_status("unknown")
        assert not validate_prep_status("")
        assert not validate_prep_status("enriching_profile")
