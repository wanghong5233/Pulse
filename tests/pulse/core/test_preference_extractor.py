from __future__ import annotations

from pulse.core.learning import PreferenceExtractor


class _JobGoalLLM:
    def __init__(self) -> None:
        self.prompt = ""
        self.route = ""

    def invoke_text(self, prompt: str, *, route: str = "default") -> str:
        self.prompt = prompt
        self.route = route
        return """
        {
          "core_prefs": {},
          "soul_updates": {},
          "domain_prefs": [
            {
              "domain": "job",
              "op": "memory.record",
              "args": {
                "item": {
                  "type": "favor_trait",
                  "target": null,
                  "content": "偏好互联网垂直实习，业务要合适、垂直匹配、含金量高，秋招叙事顺畅",
                  "valid_until": null
                }
              },
              "evidence": "我的核心目的是找一段有含金量的互联网的垂直实习",
              "confidence": 0.93
            }
          ],
          "evidences": ["job_goal"]
        }
        """


def test_preference_extractor_extracts_preference_and_style() -> None:
    extractor = PreferenceExtractor()
    result = extractor.extract("以后默认用杭州，我不喜欢外包岗，回答尽量简短")
    assert result.prefs_updates["default_location"] == "杭州"
    assert "外包岗" in result.prefs_updates["dislike"]
    assert result.soul_updates["tone"] == "concise"
    assert "style_concise" in result.evidences


def test_preference_extractor_extracts_preferred_name() -> None:
    extractor = PreferenceExtractor()
    result = extractor.extract("以后称呼我 老王")
    assert result.prefs_updates["preferred_name"] == "老王"


def test_preference_extractor_routes_abstract_job_goal_to_domain_memory() -> None:
    llm = _JobGoalLLM()
    extractor = PreferenceExtractor(llm_router=llm)

    result = extractor.extract(
        "我的核心目的是找一段有含金量的互联网的垂直实习，"
        "第一目标是业务要合适，垂直匹配，含金量要高，秋招叙事顺畅。"
    )

    assert llm.route == "classification"
    assert "抽象择业目标" in llm.prompt
    assert result.core_prefs == {}
    assert len(result.domain_prefs) == 1
    pref = result.domain_prefs[0]
    assert pref.domain == "job"
    assert pref.op == "memory.record"
    assert pref.args["item"]["type"] == "favor_trait"
    assert "垂直匹配" in pref.args["item"]["content"]


def test_preference_extractor_passes_full_user_text_to_llm() -> None:
    llm = _JobGoalLLM()
    extractor = PreferenceExtractor(llm_router=llm)
    tail_goal = "尾部目标：最低薪资不要低于300每天，优先杭州上海，已联系过不要重复投递"
    long_text = "背景信息" * 700 + tail_goal

    extractor.extract(long_text)

    assert tail_goal in llm.prompt


class _ProbeLLM:
    """Captures the prompt that PreferenceExtractor asks the LLM to follow,
    returns an empty extraction. Used only to inspect the *prompt contract*
    — what guidance we are giving the LLM. The LLM's actual extraction
    quality is the model vendor's responsibility, not ours.
    """
    def __init__(self) -> None:
        self.prompt = ""
        self.route = ""

    def invoke_text(self, prompt: str, *, route: str = "default") -> str:
        self.prompt = prompt
        self.route = route
        return '{"core_prefs":{},"soul_updates":{},"domain_prefs":[],"evidences":[]}'


def test_extractor_prompt_directs_llm_to_treat_collective_nouns_as_avoid_trait() -> None:
    """Pin the prompt contract that prevents the 2026-04-28 regression
    (post-mortem trace_753fecf70cc5).

    User said "暂时战略性放弃大厂暑期实习". The LLM must extract this as
    ``avoid_trait`` with target=大厂 — NOT as ``avoid_company`` (because
    "大厂" is a class, not a specific company), NOT as a constraint_note
    (because the matcher LLM key off type=avoid_trait to apply the
    world-knowledge expansion path).

    We deliberately don't assert on what the LLM actually outputs (that
    would mock LLM behavior, a same-source rehearsal test). We assert on
    what the prompt instructs:

      1. Mass-noun phrasing is named explicitly ("不要 X / 战略性放弃 X / ...").
      2. The canonical 大厂 example is present.
      3. The avoid_company branch warns *against* expanding mass nouns
         into company lists at extraction time — that's the matcher's job,
         not the extractor's.
    """
    llm = _ProbeLLM()
    extractor = PreferenceExtractor(llm_router=llm)
    extractor.extract(
        "我正在找大模型应用开发 agent 实习, 但是我目前实力不够 "
        "暂时战略性放弃大厂暑期实习, 我希望积累一段小厂或者初创的深度垂直实习 Agent 机会"
    )

    assert llm.prompt, "extractor must invoke the LLM"
    p = llm.prompt

    assert "avoid_trait" in p
    assert "战略性放弃" in p
    assert "大厂" in p, "canonical mass-noun example missing from prompt"
    assert "外包" in p or "国企" in p, (
        "prompt should generalize beyond 大厂 — at least one other "
        "mass-noun trait example is required so the LLM doesn't think "
        "this rule only fires on the literal word '大厂'."
    )
    assert (
        "集体名词" in p or "类别标签" in p
        or "do not expand" in p.lower() or "不要" in p and "展开" in p
    ), (
        "prompt must instruct the LLM not to pre-expand mass nouns into "
        "concrete company lists — that's the downstream matcher's job."
    )
