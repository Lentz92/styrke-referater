"""The matcher and the slug history in styrke/matching.py, on hand-made and real decisions and rules."""

from styrke.matching import (
    Candidate,
    FormerSlug,
    LiveRule,
    RuleRefs,
    SlugRegistry,
    carry_slugs,
    match_decisions,
    quote_overlap,
)

LICENS = Candidate("Licensgebyr", "Licensgebyret er 300 kr. pr. løfter pr. år.", "vedtaget", (10, 20))
KLUBSKIFTE = Candidate("Klubskifte", "Et klubskifte kræver tre måneders karantæne.", "vedtaget", (40, 50))
DOMMERE = Candidate("Dommerforpligtelse", "Klubberne skal stille to dommere pr. stævne.", "forkastet", (70, 85))


def _pairs(old, new) -> set[tuple[int, int]]:
    return {(m.old, m.new) for m in match_decisions(old, new)}


# ---------------------------------------------------------------- decisions

def test_reordered_decisions_keep_their_partners():
    assert _pairs([LICENS, KLUBSKIFTE, DOMMERE], [DOMMERE, LICENS, KLUBSKIFTE]) == {(0, 1), (1, 2), (2, 0)}


def test_shifted_quote_boundaries_and_new_wording_still_match():
    licens = Candidate("Licensgebyr", "Licensen koster 300 kr. pr. løfter om året.", "vedtaget", (13, 25))
    klubskifte = Candidate("Klubskifte", "Skifter man klub, er der tre måneders karantæne.", "vedtaget", (37, 46))
    assert _pairs([LICENS, KLUBSKIFTE], [klubskifte, licens]) == {(0, 1), (1, 0)}


def test_a_short_quote_inside_a_long_one_is_the_same_passage():
    # Overlap is measured against the shorter quote: 5 of 5 words, not 5 of 30.
    assert quote_overlap((10, 40), (20, 25)) == 1
    reworded = Candidate("Gebyr for licens", "Hver løfter betaler årligt et beløb til forbundet.", "vedtaget",
                         (20, 25))
    long_quote = Candidate(LICENS.emne, LICENS.tekst, "vedtaget", (10, 40))
    assert _pairs([long_quote], [reworded]) == {(0, 0)}


def test_a_split_decision_keeps_its_id_in_the_part_most_like_it():
    both = Candidate("Licensgebyr", "Licensgebyret er 300 kr. pr. løfter pr. år, og startgebyret er 200 kr.",
                     "vedtaget", (10, 40))
    start = Candidate("Startgebyr", "Startgebyret er 200 kr. pr. stævne.", "vedtaget", (25, 40))
    assert _pairs([both], [start, LICENS]) == {(0, 1)}  # both parts lie inside its quote; the text decides


def test_two_merged_decisions_leave_one_unmatched():
    start = Candidate("Startgebyr", "Startgebyret er 200 kr. pr. stævne.", "vedtaget", (25, 40))
    both = Candidate("Licensgebyr", "Licensgebyret er 300 kr. pr. løfter pr. år, og startgebyret er 200 kr.",
                     "vedtaget", (10, 40))
    assert _pairs([LICENS, start], [both]) == {(0, 0)}


def test_decisions_sharing_one_quote_are_told_apart_by_their_text():
    # As in a rule document listing four requirements under one heading, which is the quote of all four.
    texts = [("herrer 3-kamp", "59 kg 335, 66 kg 390, 74 kg 407,5"), ("herrer bænkpres", "59 kg 85, 66 kg 97,5"),
             ("damer 3-kamp", "47 kg 190, 52 kg 215, 57 kg 240"), ("damer bænkpres", "47 kg 35, 52 kg 40")]
    old = [Candidate(f"Kvalifikationskrav master, {who}", f"Krav for masters, {who}: {kg}.", "vedtaget", (0, 8))
           for who, kg in texts]
    new = [Candidate(f"Kvalifikationskrav, {who}", f"Masterkrav, {who}: {kg}.", "vedtaget", (0, 8))
           for who, kg in reversed(texts)]
    assert _pairs(old, new) == {(0, 3), (1, 2), (2, 1), (3, 0)}


def test_a_deleted_decision_is_left_unmatched():
    assert _pairs([LICENS, KLUBSKIFTE, DOMMERE], [LICENS, DOMMERE]) == {(0, 0), (2, 1)}


def test_without_a_shared_quote_the_text_matches_only_with_the_same_outcome():
    unlocated = Candidate(LICENS.emne, LICENS.tekst, "vedtaget", None)
    assert _pairs([unlocated], [Candidate(LICENS.emne, LICENS.tekst, "vedtaget", (10, 20))]) == {(0, 0)}
    assert _pairs([unlocated], [Candidate(LICENS.emne, LICENS.tekst, "forkastet", (10, 20))]) == set()


# Real pairs from the two extractions of CoachResponsibility and refbest_151224, as text alone sees them.
HEADCOACH = ("Headcoachens ansvar for adfærd og adgang", (
    "Headcoachen er ansvarlig for coaches' og løfteres adfærd i opvarmnings- og bandageområdet. Kun én coach må "
    "følge atleterne til coachzonen, og vedkommende skal være ordentligt klædt. Headcoachen skal sikre, at hver "
    "assistentcoach får et badge med foto for adgang til opvarmnings-, bandage- og løfteområdet."))
ACCREDITATION = ("Akkreditering af assistentcoaches og træningstid", (
    "Head Coach skal sikre, at hver assistentcoach får et badge med foto for adgang til opvarmnings-, wrapping- og "
    "løfteområdet, og skal aftale en fast træningstid for holdet med arrangøren."))
ACTION_PLAN = ("Handlingsplan for akutte hændelser ved stævner", (
    "Bestyrelsen godkendte en handlingsplan for akutte hændelser ved styrkeløftstævner i tre faser (umiddelbar "
    "respons, kortvarig opfølgning, efterspil) med fordelte roller for stævneleder, tovholder fra arrangerende klub "
    "og medieudvalget/bestyrelsen. Ved mindre stævner kan roller kombineres, og ved større stævner kan der tilføjes "
    "en sikkerhedsansvarlig."))
ACTION_PLAN_AGAIN = ("Handlingsplan for akutte hændelser ved stævner", (
    "Der er indført en handlingsplan for akutte hændelser ved styrkeløftstævner i tre faser (umiddelbar respons, "
    "kortvarig opfølgning, efterspil). Stævneleder standser stævnet, tovholder fra arrangørklubben tilkalder hjælp, "
    "stævneleder udfylder rapport og informerer medieudvalget, og der evalueres efter stævnet. Ved små stævner kan "
    "roller kombineres, og ved store stævner kan der tilføjes en sikkerhedsansvarlig."))


def test_the_threshold_lies_between_the_best_wrong_pair_and_a_reworded_one():
    # One quote not located, so only the text counts: 0.18 for different decisions, 0.29 for the same one.
    wrong = ([Candidate(*HEADCOACH, "vedtaget", None)], [Candidate(*ACCREDITATION, "vedtaget", (246, 270))])
    same = ([Candidate(*ACTION_PLAN, "vedtaget", None)], [Candidate(*ACTION_PLAN_AGAIN, "vedtaget", (5, 30))])
    assert _pairs(*wrong) == set()
    assert _pairs(*same) == {(0, 0)}


def test_siblings_quoted_at_different_places_do_not_swap_ids():
    # elite10122017: one fee decision per championship, worded alike (0.46) and quoted at different places.
    junior = ("Egenbetaling EM junior/subjunior", (
        "Til EM junior/subjunior 2018 er budgettet 20.000 kr. Egenbetaling er 1000 kr. for løftere med A-krav og "
        "2000 kr. for løftere med B-krav."))
    open_ = ("Egenbetaling EM Open", (
        "Til EM Open 2018 er budgettet 30.000 kr. Egenbetaling er 1000 kr. for løftere med A-krav og 2000 kr. for "
        "løftere med B-krav."))
    assert _pairs([Candidate(*junior, "vedtaget", (345, 376))], [Candidate(*open_, "vedtaget", (443, 474))]) == set()
    # Without the second quote's place the text is all there is, and 0.46 is enough.
    assert _pairs([Candidate(*junior, "vedtaget", None)], [Candidate(*open_, "vedtaget", (443, 474))]) == {(0, 0)}


# ---------------------------------------------------------------- slug succession within a category

def _plan(previous: dict[str, set[str]], new: list[tuple[str, set[str]]], taken: set[str] = frozenset(),
          former: dict[str, FormerSlug] | None = None):
    return carry_slugs([RuleRefs(slug, frozenset(refs)) for slug, refs in previous.items()],
                       [RuleRefs(title, frozenset(refs)) for title, refs in new], set(previous) | set(taken), former)


def test_an_unchanged_rule_keeps_its_slug():
    plan = _plan({"licensgebyr": {"a#1", "b#1"}}, [("Licensgebyr", {"a#1", "b#1"})])
    assert (plan.slugs, plan.kept, plan.revived, plan.aliases, plan.orphaned) == (("licensgebyr",), 1, (), {}, ())


def test_a_renamed_rule_keeps_its_slug():
    assert _plan({"licensgebyr": {"a#1", "b#1"}}, [("Gebyr for licens", {"a#1", "b#1"})]).slugs == ("licensgebyr",)


def test_in_a_split_the_largest_part_keeps_the_slug():
    plan = _plan({"gebyrer": {"a#1", "a#2", "a#3"}}, [("Startgebyr", {"a#1"}), ("Licensgebyr", {"a#2", "a#3"})])
    assert plan.slugs == ("startgebyr", "gebyrer") and plan.kept == 1


def test_a_merged_rule_becomes_an_alias_of_the_larger_one():
    plan = _plan({"licens": {"b#1"}, "licensgebyr": {"a#1", "a#2"}}, [("Licens og gebyr", {"a#1", "a#2", "b#1"})])
    assert plan.slugs == ("licensgebyr",) and plan.aliases == {"licens": "licensgebyr"}


def test_in_an_even_merge_the_earlier_rule_keeps_its_slug():
    plan = _plan({"licensgebyr": {"a#1"}, "startgebyr": {"b#1"}}, [("Gebyrer", {"a#1", "b#1"})])
    assert plan.slugs == ("licensgebyr",) and plan.aliases == {"startgebyr": "licensgebyr"}


def test_a_new_rule_gets_a_slug_no_live_or_former_slug_has():
    plan = _plan({}, [("Licensgebyr", {"a#1"}), ("Licensgebyr", {"b#1"})], taken={"licensgebyr", "licensgebyr-2"})
    assert plan.slugs == ("licensgebyr-3", "licensgebyr-4") and plan.kept == 0


def test_a_rule_whose_decisions_all_left_the_category_is_orphaned():
    plan = _plan({"klubskifte": {"k#1"}}, [("Licensgebyr", {"a#1"})])
    assert plan.orphaned == ("klubskifte",) and plan.slugs == ("licensgebyr",)


def test_a_rule_split_out_again_takes_its_former_slug_back():
    # "tilskud" was merged into "gebyrer" once; now Claude splits it out again.
    former = {"tilskud": FormerSlug("okonomi", "Tilskud", frozenset({"a#1"}), to="gebyrer"),
              "rabat": FormerSlug("okonomi", "Rabat", frozenset({"a#1", "c#1", "d#1"}))}  # holds only a third
    previous, new = {"gebyrer": {"a#1", "b#1"}}, [("Gebyrer", {"b#1"}), ("Tilskud til klubber", {"a#1"})]
    plan = _plan(previous, new, taken=set(former), former=former)
    assert plan.slugs == ("gebyrer", "tilskud") and plan.revived == ("tilskud",)
    assert _plan(previous, new, taken=set(former)).slugs == ("gebyrer", "tilskud-til-klubber")


# ---------------------------------------------------------------- the slug history across categories

RAPPORT = "Rapportering fra internationale mesterskaber"  # a title both landshold and organisation have
LIVE = [LiveRule("licensgebyr", "okonomi", "Licensgebyr", frozenset({"a#1", "a#2", "b#9"})),
        LiveRule("masterlicens", "master", "Masterlicens", frozenset({"b#1", "a#3"})),
        LiveRule("rapportering-fra-internationale-mesterskaber", "landshold", RAPPORT, frozenset({"l#1"})),
        LiveRule("klubskifte-2", "medlemskab", "Klubskifte", frozenset({"k#9"})),
        LiveRule("rabat", "okonomi", "Rabat", frozenset({"r#9"})),
        LiveRule("tilskud", "organisation", "Tilskud", frozenset({"t#1"})),
        LiveRule("tilskud-2", "organisation", "Tilskud", frozenset({"t#2"}))]
LIVE_IDS = {"a#1", "a#2", "a#3", "b#1", "b#9", "l#1", "k#9", "r#9", "t#1", "t#2", "o#1"}


def _resolved(former: FormerSlug, live=LIVE) -> str | None:
    registry = SlugRegistry.of({"former": former}).resolved(live, LIVE_IDS)
    return registry.former()["former"].to


def test_a_former_slug_follows_its_decisions_into_another_category():
    assert _resolved(FormerSlug("okonomi", "Licens", frozenset({"b#1"}))) == "masterlicens"


def test_an_alias_follows_the_larger_part_after_a_later_split():
    assert _resolved(FormerSlug("okonomi", "Licens", frozenset({"b#1", "a#3"}), to="licensgebyr")) == "masterlicens"


def test_an_alias_keeps_its_target_when_its_decisions_got_new_ids():
    # b#8 was extracted again as b#9, still in licensgebyr: nothing holds b#8, and the link must keep working.
    assert _resolved(FormerSlug("okonomi", "Licens", frozenset({"b#8"}), to="licensgebyr")) == "licensgebyr"


def test_an_alias_whose_decisions_went_to_no_rule_is_not_kept():
    # o#1 still exists but is a one-off now (udeladt): licensgebyr holds none of it, so the pointer has no basis.
    assert _resolved(FormerSlug("okonomi", "Licens", frozenset({"o#1"}), to="licensgebyr")) is None


def test_a_former_slug_whose_decisions_got_new_ids_follows_its_title_in_its_own_category():
    assert _resolved(FormerSlug("medlemskab", "Klubskifte", frozenset({"k#1"}))) == "klubskifte-2"
    new = LiveRule("rapportering-fra-internationale-mesterskaber-3", "organisation", RAPPORT, frozenset({"o#9"}))
    assert _resolved(FormerSlug("organisation", RAPPORT, frozenset({"o#2"})), [*LIVE, new]) == new.slug


def test_a_namesake_in_another_category_is_another_rule():
    # organisation's report rule lost its decision (new id, or a one-off now); landshold's rule of that name is not it.
    assert _resolved(FormerSlug("organisation", RAPPORT, frozenset({"o#2"}))) is None
    assert _resolved(FormerSlug("organisation", RAPPORT, frozenset({"o#1"}))) is None
    # Nor does a later rule of the same name in another category take over a retired slug.
    assert _resolved(FormerSlug("master", "Rabat", frozenset({"m#1"}))) is None
    assert _resolved(FormerSlug("okonomi", "Rabat", frozenset({"m#1"}))) == "rabat"


def test_two_namesakes_in_the_category_are_no_evidence():
    assert _resolved(FormerSlug("organisation", "Tilskud", frozenset({"t#0"}))) is None


def test_a_slug_that_is_live_again_leaves_the_history():
    # A revived slug whose rule file failed to write after the history did: it must not lead to itself.
    registry = SlugRegistry.of({"licensgebyr": FormerSlug("okonomi", "Licens", frozenset({"a#1"}), to="licensgebyr"),
                                "gebyr": FormerSlug("okonomi", "Gebyr", frozenset({"a#2"}))})
    assert set(registry.resolved(LIVE, LIVE_IDS).former()) == {"gebyr"}


def test_a_former_slug_with_nothing_left_is_retired():
    registry = SlugRegistry.of({"bonus": FormerSlug("okonomi", "Bonus", frozenset({"x#1"}), to="licensgebyr")})
    resolved = registry.resolved(LIVE, LIVE_IDS | {"x#1"})
    assert resolved.aliases == {} and resolved.retired["bonus"].to is None
    assert resolved.taken() == {"bonus"}  # never handed out again


def test_a_kept_alias_follows_its_target_when_that_is_merged_away():
    # "licens" lost its decisions (new ids) and kept leading to "rabat"; now "rabat" was merged into licensgebyr.
    registry = SlugRegistry.of({
        "licens": FormerSlug("okonomi", "Licens", frozenset({"b#8"}), to="rabat"),
        "rabat": FormerSlug("okonomi", "Rabat", frozenset({"a#2"}), to="licensgebyr"),
    })
    live = [rule for rule in LIVE if rule.slug != "rabat"]
    assert registry.resolved(live, LIVE_IDS).targets() == {"licens": "licensgebyr", "rabat": "licensgebyr"}
