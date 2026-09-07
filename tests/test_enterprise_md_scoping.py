"""Tests for issue #100 — a docs/spec-only PR only ever reached DX.

The Enterprise profile's Architecture, Security, and GxP Compliance specialists
all excluded ``.md`` from their ``file_patterns`` (Architecture and Security via
an explicit ``"!*.md"``, GxP via patterns that were purely filename-keyword-based
with no markdown glob at all). A GxP-controlled specification markdown file —
the exact shape reviewed in PRs F2iLLC/vigil#5658 and #5618 — therefore never
reached any of the three specialists whose domain expertise (module boundaries,
auth/trust boundaries, audit-trail scope) is directly relevant to a spec
document, leaving DX as the only reviewer of record.

This only changes the Enterprise profile. The default/general-purpose profile
deliberately keeps Architecture/Security scoped away from markdown — a
general-purpose repo does not want those specialists running on every README.
"""

from vigil.diff_parser import FileHunk, filter_hunks
from vigil.personas import DEFAULT_PROFILE, ENTERPRISE_PROFILE, Persona, ReviewProfile


def _persona_named(profile: ReviewProfile, name: str) -> Persona:
    return next(p for p in profile.specialists if p.name == name)


def _scoped_by(persona: Persona, path: str) -> bool:
    hunk = FileHunk(path=path, header=f"diff --git a/{path} b/{path}", content="")
    return bool(filter_hunks([hunk], persona.file_patterns))


class TestEnterpriseSpecialistsNowReviewMarkdown:
    """Architecture, Security, and GxP Compliance must see a spec-only diff."""

    def test_architecture_is_scoped_to_markdown(self):
        persona = _persona_named(ENTERPRISE_PROFILE, "Architecture")
        assert _scoped_by(persona, "docs/DESIGN_SPECIFICATION.md")
        assert _scoped_by(persona, "docs/spec.mdx")

    def test_security_is_scoped_to_markdown(self):
        persona = _persona_named(ENTERPRISE_PROFILE, "Security")
        assert _scoped_by(persona, "docs/DESIGN_SPECIFICATION.md")

    def test_gxp_compliance_is_scoped_to_markdown(self):
        """GxP's patterns were purely filename-keyword-based (*audit*, *gxp*,
        ...); a spec file whose name doesn't contain one of those words must
        still reach it now that *.md/*.mdx are in its file_patterns. Self-gating
        against irrelevant markdown is left to its system prompt, not the glob."""
        persona = _persona_named(ENTERPRISE_PROFILE, "GxP Compliance")
        assert _scoped_by(persona, "docs/DESIGN_SPECIFICATION.md")
        assert _scoped_by(persona, "docs/spec.mdx")

    def test_default_profile_architecture_and_security_still_exclude_markdown(self):
        """The default profile's own Architecture/Security personas are a
        separate, general-purpose pair — this fix must not touch them. A
        general-purpose repo should not get Architecture/Security running on
        every README."""
        assert not _scoped_by(_persona_named(DEFAULT_PROFILE, "Architecture"), "docs/spec.md")
        assert not _scoped_by(_persona_named(DEFAULT_PROFILE, "Security"), "docs/spec.md")

    def test_default_profile_has_no_gxp_specialist(self):
        names = [p.name for p in DEFAULT_PROFILE.specialists]
        assert "GxP Compliance" not in names

    def test_other_enterprise_specialists_are_unchanged(self):
        """Test Strategy, Data Architecture, and Performance were not in scope
        for this fix and must keep their existing markdown behavior."""
        assert not _scoped_by(_persona_named(ENTERPRISE_PROFILE, "Data Architecture"), "docs/spec.md")
        assert not _scoped_by(_persona_named(ENTERPRISE_PROFILE, "Performance"), "docs/spec.md")

    def test_dx_still_covers_markdown_as_before(self):
        assert _scoped_by(_persona_named(ENTERPRISE_PROFILE, "DX"), "docs/spec.md")
