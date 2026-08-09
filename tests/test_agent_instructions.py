"""Static regression coverage for the root_agent instruction defect
documented in INVESTIGATION.md (defect 2).

The live-model paraphrasing behavior itself can't be unit tested without a
real API call. What's testable is the specific text-level cause: root_agent's
preview-routing instruction used to tell it "that is INTENT 1" before
delegating to tracker_agent, a label with no defined purpose on the
delegated message. That primed root_agent to invent an "INTENT 1: ..."
paraphrase of the user's request, dropping constraint clauses like "but
don't write anything" along the way (confirmed against a live eval run:
tool_trajectory_avg_score scored 0.0 against the expected verbatim request).
These tests assert the instruction text no longer contains that label and
does require verbatim forwarding, so a future edit reintroducing either
problem is caught here instead of only in a live eval run.
"""

from agent import root_agent

PREVIEW_BULLET_MARKER = "asks to preview, show, or see what would be added WITHOUT"


def _preview_bullet(instruction: str) -> str:
    """Extracts the preview-routing bullet, the one instruction segment
    responsible for the delegated message root_agent sends to tracker_agent
    for a preview request."""
    start = instruction.index(PREVIEW_BULLET_MARKER)
    end = instruction.index("\n\n", start)
    return instruction[start:end]


class TestPreviewRoutingInstruction:
    def test_preview_bullet_exists(self):
        assert PREVIEW_BULLET_MARKER in root_agent.instruction

    def test_does_not_reintroduce_the_intent_1_self_priming_label(self):
        # The exact phrase that caused root_agent to paraphrase into
        # "INTENT 1: ..." and drop the "don't write anything" clause.
        bullet = _preview_bullet(root_agent.instruction)
        assert "INTENT 1" not in bullet

    def test_requires_verbatim_forwarding(self):
        bullet = _preview_bullet(root_agent.instruction)
        assert "VERBATIM" in bullet

    def test_explicitly_protects_the_dont_write_clause(self):
        bullet = _preview_bullet(root_agent.instruction)
        assert "not to write" in bullet or "don't write" in bullet

    def test_still_forbids_paraphrasing_into_a_stage_request(self):
        # Pre-existing protection this fix must not remove.
        bullet = _preview_bullet(root_agent.instruction)
        assert "never paraphrase a preview request into a stage request" in bullet.lower()


INBOX_LISTING_BULLET_MARKER = "asks to show, list, find, or search emails, delegate"


def _inbox_listing_bullet(instruction: str) -> str:
    """Extracts the simple show/list/find/search-delegation bullet, the
    instruction segment responsible for the request root_agent forwards to
    inbox_agent for a plain listing request."""
    start = instruction.index(INBOX_LISTING_BULLET_MARKER)
    end = instruction.index("\n- ", start + len(INBOX_LISTING_BULLET_MARKER))
    return instruction[start:end]


class TestInboxListingVerbatimForwarding:
    # Live eval evidence (2026-08-09 full-suite run): the agent sent "show 5
    # most recent emails" — a paraphrase of the user's "show me my 5 most
    # recent emails" — scoring tool_trajectory_avg_score 0.0 against
    # evals/inbox_listing.test.json's verbatim-request reference, even
    # though the response itself scored 1.0. Same defect class as the
    # dropped "but don't write anything" clause above: information loss at
    # the delegation boundary.

    def test_bullet_exists(self):
        assert INBOX_LISTING_BULLET_MARKER in root_agent.instruction

    def test_requires_verbatim_forwarding_for_simple_listing(self):
        bullet = _inbox_listing_bullet(root_agent.instruction)
        assert "VERBATIM" in bullet

    def test_exempts_the_classification_chain(self):
        # root_agent must still construct its own fetch request to
        # inbox_agent when the results feed classification_agent, not
        # forward the user's "what needs attention"-style phrasing verbatim.
        bullet = _inbox_listing_bullet(root_agent.instruction)
        assert "classification_agent" in bullet
        assert "NOT apply" in bullet

    def test_exempts_the_drafting_chain(self):
        # root_agent must still construct an elaborated inbox_agent request
        # for drafting (explicitly requiring get_email_detail and the full
        # body), not forward the user's drafting request verbatim.
        bullet = _inbox_listing_bullet(root_agent.instruction)
        assert "get_email_detail" in bullet
        assert "drafting a reply" in bullet
