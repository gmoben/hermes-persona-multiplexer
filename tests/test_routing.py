"""Tests for the Hermes-independent routing core.

Fixtures use a generic demo squad (alex/sam/max) — this is a standalone,
project-agnostic plugin, so no downstream project's persona names appear here.
"""

import pytest

from hermes_persona_multiplexer import routing as r


def _cfg(**over):
    base = {
        "brain": "main",
        "personas": [
            {"id": "alex", "token_env": "TOK_ALEX", "display_name": "Alex"},
            {"id": "sam", "token_env": "TOK_SAM"},
        ],
    }
    base.update(over)
    return r.parse_config(base)


def test_parse_config_happy_path():
    cfg = _cfg()
    assert cfg.brain == "main"
    assert cfg.ids == ("alex", "sam")
    assert cfg.default_persona == "alex"  # defaults to first
    assert cfg.persona("alex").label == "Alex"
    assert cfg.persona("sam").label == "sam"  # falls back to id
    assert cfg.ignore_self is True


def test_parse_config_explicit_default_persona():
    cfg = _cfg(default_persona="sam")
    assert cfg.default_persona == "sam"


@pytest.mark.parametrize(
    "raw, msg",
    [
        ({"personas": []}, "non-empty"),
        ({"personas": [{"token_env": "X"}]}, "missing `id`"),
        ({"personas": [{"id": "a"}]}, "missing `token_env`"),
        (
            {"personas": [{"id": "a", "token_env": "X"}, {"id": "a", "token_env": "Y"}]},
            "duplicate persona id",
        ),
        (
            {"personas": [{"id": "a", "token_env": "X"}, {"id": "b", "token_env": "X"}]},
            "duplicate token_env",
        ),
        (
            {"personas": [{"id": "a", "token_env": "X"}], "default_persona": "nope"},
            "not a configured persona",
        ),
    ],
)
def test_parse_config_errors(raw, msg):
    with pytest.raises(r.ConfigError) as exc:
        r.parse_config(raw)
    assert msg in str(exc.value)


def test_is_self_authored():
    assert r.is_self_authored("42", ["1", "42"]) is True
    assert r.is_self_authored("99", ["1", "42"]) is False
    assert r.is_self_authored(None, ["1"]) is False
    # type coercion
    assert r.is_self_authored(42, [42]) is True


def test_decide_inbound_processes_known_persona():
    cfg = _cfg()
    d = r.decide_inbound(recipient_persona="alex", author_account_id="user1",
                         own_account_ids={"botAlex", "botSam"}, config=cfg)
    assert d.process is True
    assert d.persona == "alex"
    assert d.reason == "ok"


def test_decide_inbound_skips_own_account():
    cfg = _cfg()
    d = r.decide_inbound(recipient_persona="alex", author_account_id="botSam",
                         own_account_ids={"botAlex", "botSam"}, config=cfg)
    assert d.process is False
    assert d.reason == "own-account"


def test_decide_inbound_unknown_persona():
    cfg = _cfg()
    d = r.decide_inbound(recipient_persona="ghost", author_account_id="user1",
                         own_account_ids=set(), config=cfg)
    assert d.process is False
    assert d.reason.startswith("unknown-persona")


def test_decide_inbound_ignore_self_disabled():
    cfg = _cfg(ignore_self=False)
    d = r.decide_inbound(recipient_persona="alex", author_account_id="botAlex",
                         own_account_ids={"botAlex"}, config=cfg)
    assert d.process is True


def test_resolve_outbound_persona_priority():
    cfg = _cfg()
    # explicit wins
    assert r.resolve_outbound_persona(explicit="sam", inbound="alex", config=cfg) == "sam"
    # inbound next
    assert r.resolve_outbound_persona(explicit=None, inbound="alex", config=cfg) == "alex"
    # default last
    assert r.resolve_outbound_persona(explicit=None, inbound=None, config=cfg) == "alex"
    # inbound unknown -> default
    assert r.resolve_outbound_persona(explicit=None, inbound="ghost", config=cfg) == "alex"


def test_resolve_outbound_persona_unknown_explicit_raises():
    cfg = _cfg()
    with pytest.raises(KeyError):
        r.resolve_outbound_persona(explicit="ghost", inbound=None, config=cfg)


def test_persona_metadata():
    assert r.persona_metadata("alex") == {"persona": "alex"}


# ── orchestrator + shared-channel routing ──────────────────────────────────
def test_orchestrator_defaults_to_default_persona():
    assert _cfg().orchestrator == "alex"
    assert _cfg(default_persona="sam").orchestrator == "sam"


def test_orchestrator_explicit_overrides_default():
    cfg = _cfg(default_persona="alex", orchestrator="sam")
    assert cfg.orchestrator == "sam"
    assert cfg.default_persona == "alex"


def test_orchestrator_must_be_a_configured_persona():
    with pytest.raises(r.ConfigError) as exc:
        _cfg(orchestrator="ghost")
    assert "orchestrator" in str(exc.value)


def test_decide_inbound_dm_unchanged():
    cfg = _cfg()
    d = r.decide_inbound(recipient_persona="sam", author_account_id="u1",
                         own_account_ids=set(), config=cfg, is_dm=True)
    assert d.process is True and d.persona == "sam"


def test_decide_inbound_channel_only_orchestrator_intakes():
    cfg = _cfg()  # orchestrator defaults to alex
    intake = r.decide_inbound(recipient_persona="alex", author_account_id="u1",
                              own_account_ids=set(), config=cfg,
                              is_dm=False, channel_id="C1", home_channel_id="C1")
    assert intake.process is True and intake.reason == "ok-channel"
    other = r.decide_inbound(recipient_persona="sam", author_account_id="u1",
                             own_account_ids=set(), config=cfg,
                             is_dm=False, channel_id="C1", home_channel_id="C1")
    assert other.process is False and other.reason == "not-orchestrator"


def test_decide_inbound_channel_confined_to_home():
    cfg = _cfg()
    outside = r.decide_inbound(recipient_persona="alex", author_account_id="u1",
                               own_account_ids=set(), config=cfg,
                               is_dm=False, channel_id="C2", home_channel_id="C1")
    assert outside.process is False and outside.reason == "outside-home-channel"
    # no home channel configured -> channels are ignored entirely
    none_cfg = r.decide_inbound(recipient_persona="alex", author_account_id="u1",
                                own_account_ids=set(), config=cfg,
                                is_dm=False, channel_id="C1", home_channel_id=None)
    assert none_cfg.process is False and none_cfg.reason == "outside-home-channel"


def test_decide_inbound_channel_still_skips_own_accounts():
    cfg = _cfg()
    d = r.decide_inbound(recipient_persona="alex", author_account_id="botSam",
                         own_account_ids={"botSam"}, config=cfg,
                         is_dm=False, channel_id="C1", home_channel_id="C1")
    assert d.process is False and d.reason == "own-account"


@pytest.mark.parametrize(
    "persona, channel, parent, home, expected",
    [
        ("alex", "C1", None, "C1", True),    # orchestrator, home channel itself
        ("alex", "T1", "C1", "C1", True),    # orchestrator, thread whose parent is home
        ("sam", "C1", None, "C1", False),    # not the orchestrator -> skip (no dup thread)
        ("alex", "C2", None, "C1", False),   # wrong channel
        ("alex", "T1", "C2", "C1", False),   # thread under the wrong parent
        ("alex", "C1", None, None, False),   # no home channel configured
    ],
)
def test_should_intake_shared_channel(persona, channel, parent, home, expected):
    assert r.should_intake_shared_channel(
        persona, channel, parent, config=_cfg(), home_channel_id=home) is expected


@pytest.mark.parametrize(
    "content, expected_persona, expected_text",
    [
        ("[persona:sam] hello", "sam", "hello"),
        ("[persona:Alex]\nhey there", "alex", "hey there"),
        ("  [persona: sam ]  hi", "sam", "hi"),
        ("just a normal reply", None, "just a normal reply"),
        ("[persona:ghost] unknown stays put", None, "[persona:ghost] unknown stays put"),
        ("text then [persona:sam]", None, "text then [persona:sam]"),
        ("", None, ""),
    ],
)
def test_extract_reply_persona(content, expected_persona, expected_text):
    persona, text = r.extract_reply_persona(content, ("alex", "sam"))
    assert persona == expected_persona
    assert text == expected_text


@pytest.mark.parametrize(
    "content, expected_persona, expected_text",
    [
        ("[next:sam] On it — looking…", "sam", "On it — looking…"),
        ("[next:Alex] one sec", "alex", "one sec"),
        ("no hint here", None, "no hint here"),
        ("[next:ghost] unknown stays", None, "[next:ghost] unknown stays"),
        ("[persona:sam] not a next tag", None, "[persona:sam] not a next tag"),
    ],
)
def test_extract_next_persona(content, expected_persona, expected_text):
    persona, text = r.extract_next_persona(content, ("alex", "sam"))
    assert persona == expected_persona
    assert text == expected_text


@pytest.mark.parametrize(
    "content, persona, before, after",
    [
        # leading tag: nothing before, reply follows
        ("[persona:sam] hello", "sam", "", "hello"),
        # case-insensitive id resolves to canonical; surrounding whitespace tolerated
        ("[persona:Alex] hi", "alex", "", "hi"),
        ("[persona: sam ] hi", "sam", "", "hi"),
        # mid-message: the brain bundled an orchestrator ack ahead of the tagged reply,
        # so `before` carries the ack and `after` the persona's reply (tag stripped)
        ("[next:sam] On it\n\n[persona:sam] here you go", "sam",
         "[next:sam] On it\n\n", "here you go"),
        # no tag -> all text is `before`, `after` empty
        ("just a normal reply", None, "just a normal reply", ""),
        # unknown persona is left untouched
        ("[persona:ghost] nope", None, "[persona:ghost] nope", ""),
        ("", None, "", ""),
        # first KNOWN persona wins; an unknown tag ahead of it stays in `before`
        ("[persona:ghost] x [persona:sam] y", "sam", "[persona:ghost] x ", "y"),
    ],
)
def test_split_reply_persona(content, persona, before, after):
    assert r.split_reply_persona(content, ("alex", "sam")) == (persona, before, after)
