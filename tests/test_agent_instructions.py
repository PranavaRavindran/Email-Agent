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
from agents.drafting_agent import drafting_agent

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


DRAFT_PRESENTATION_MARKER = "Present the result to the user in exactly this order"


def _draft_presentation_block(instruction: str) -> str:
    """Extracts the draft-presentation instruction (steps a-d plus the
    trailing 'never summarize' sentence), the segment responsible for what
    root_agent shows the user after drafting_agent returns a draft."""
    start = instruction.index(DRAFT_PRESENTATION_MARKER)
    end = instruction.index("\n\n", start)
    return instruction[start:end]


class TestNoReplyCaveatIsAdditive:
    # Live eval evidence (2026-08-09 15:33 run): for a Cisco rejection sent
    # from a no-reply address, the agent's entire final response was the
    # "Replying to" line followed by "This email is a post-only address, so
    # a reply is not possible. A reply to this address may not be received."
    # No draft was ever shown. The judge scored acknowledges_rejection and
    # gracious_professional at 0.0 because there was no acknowledgement to
    # judge — the no-reply caveat had been produced instead of the draft,
    # not alongside it, even though notes_no_reply_address and both
    # tool-use rubrics scored 1.0. The old wording (step c standalone, with
    # no statement that the draft in step b is mandatory regardless of
    # reply-ability) left room for that substitution. These tests assert
    # the instruction now says so explicitly, so a future edit that drops
    # the "additive" language is caught here instead of only in a live eval.

    def test_bullet_exists(self):
        assert DRAFT_PRESENTATION_MARKER in root_agent.instruction

    def test_no_reply_note_is_explicitly_additive(self):
        block = _draft_presentation_block(root_agent.instruction)
        assert "ADDITIVE" in block

    def test_no_reply_status_is_never_a_reason_to_skip_the_draft(self):
        block = _draft_presentation_block(root_agent.instruction)
        lowered = block.lower()
        assert "never a reason to skip" in lowered or "never omit step (b)" in lowered

    def test_draft_step_still_present(self):
        # Pre-existing requirement this fix must not remove.
        block = _draft_presentation_block(root_agent.instruction)
        assert "The complete draft text exactly as drafting_agent returned it" in block

    def test_send_question_still_present(self):
        # Pre-existing requirement this fix must not remove.
        block = _draft_presentation_block(root_agent.instruction)
        assert "Would you like to send this?" in block


class TestDraftingAgentNeverDeclines:
    # Same live run: the trajectory attributes the substitute "post-only"
    # statement to drafting_agent itself, not just to root_agent's
    # presentation step. drafting_agent's old instruction for application
    # confirmations ("keep the reply brief or note that no reply is
    # typically needed") could be read as license to return a note instead
    # of a drafted reply. These tests assert drafting_agent's instruction
    # unconditionally requires producing draft text.

    def test_never_declines_based_on_reply_ability(self):
        assert "not your concern" in drafting_agent.instruction
        assert "never decline to draft" in drafting_agent.instruction.lower()

    def test_confirmation_reply_is_still_an_actual_draft(self):
        # Pre-existing requirement this fix must not remove: confirmation
        # replies are brief, but brief still means drafted text.
        assert "brief" in drafting_agent.instruction.lower()
        assert "never a note that no reply is needed" in drafting_agent.instruction.lower()
