"""Plain-text rendering: every status branch, truncation and forged line."""

import dataclasses as dcs
from types import SimpleNamespace

import pytest
from astrbot_plugin_tieba_moderator.core.models import (
    Author,
    Case,
    CaseStatus,
    ItemKind,
    PatrolReport,
    Verdict,
)
from astrbot_plugin_tieba_moderator.services.render import (
    case_list_line,
    format_case,
    format_report,
    one_line,
)

from astrbot_plugin_tieba_moderator.tests.fakes import make_item

FAILED = "审查失败"
DAYS = "1|3|10"


def make_case(*, status=CaseStatus.PENDING, verdict=None, **changes):
    """Build a case for rendering."""
    return dcs.replace(
        Case(
            id=3,
            item=make_item(),
            verdict=verdict or Verdict(violation=True, category="广告", confidence=0.9),
            status=status,
            created_at=2_000_000_000,
        ),
        **changes,
    )


def render(case, **kwargs):
    """Render a case with the production constants."""
    return format_case(case, days_text=DAYS, failed_category=FAILED, **kwargs)


def test_pending_case_shows_the_action_commands():
    card = render(make_case())
    assert card.splitlines()[:3] == [
        "【贴吧巡查】案件 #3 待处理",
        "吧：test吧 | 类型：主题帖",
        "作者：reader",
    ]
    assert "判定：广告 | 置信度 0.90" in card
    assert "建议封禁：不封禁" in card
    assert f"处理：/tb del 3 | /tb ban 3 <{DAYS}> | /tb ignore 3" in card


def test_ban_suggestion_is_shown_when_present():
    verdict = Verdict(violation=True, category="广告", confidence=0.9, ban_days=10)
    assert "建议封禁：10 天（需吧务确认）" in render(make_case(verdict=verdict))


def test_auto_deleted_and_deleted_and_banned_hints():
    auto = render(make_case(status=CaseStatus.AUTO_DELETED))
    assert f"误删请恢复：/tb restore 3；需要封禁：/tb ban 3 <{DAYS}>" in auto
    deleted = render(make_case(status=CaseStatus.DELETED))
    assert f"恢复：/tb restore 3 | 封禁：/tb ban 3 <{DAYS}>" in deleted
    banned = render(make_case(status=CaseStatus.BANNED))
    assert "恢复内容（不解封）：/tb restore 3" in banned


def test_ignored_and_restored_have_no_action_line():
    for status in (CaseStatus.IGNORED, CaseStatus.RESTORED):
        card = render(make_case(status=status))
        assert "处理：" not in card
        assert "/tb restore" not in card


def test_failed_review_omits_confidence_and_ban():
    verdict = Verdict(violation=False, category=FAILED, confidence=0.0, reason="超时")
    card = render(make_case(verdict=verdict))
    assert "判定：审查失败（未能自动判定）" in card
    assert "置信度" not in card
    assert "建议封禁" not in card
    assert "请点链接人工判断" in card


def test_detail_shows_handler_and_note():
    card = render(
        make_case(handled_by="webui:alice", handled_at=2_000_000_000, note="备注"),
        detail=True,
    )
    assert "处理人：webui:alice（" in card
    assert "备注：备注" in card
    plain = render(make_case(handled_by="webui:alice", note="备注"))
    assert "处理人" not in plain and "备注" not in plain


def test_post_and_comment_floors_are_labelled():
    post = render(make_case(item=make_item(kind=ItemKind.POST, floor=7)))
    assert "类型：楼层（7 楼）" in post
    comment = render(make_case(item=make_item(kind=ItemKind.COMMENT, floor=7)))
    assert "类型：楼中楼（7 楼的楼中楼）" in comment


def test_author_name_fallbacks():
    no_nick = render(make_case(item=make_item(author=Author(user_id=5, portrait="p"))))
    assert "作者：5" in no_nick
    nick = render(
        make_case(
            item=make_item(
                author=Author(user_id=5, portrait="p", nick_name="昵称", user_name="登录名")
            )
        )
    )
    assert "作者：昵称（登录名）" in nick
    same = render(
        make_case(
            item=make_item(
                author=Author(user_id=5, portrait="p", nick_name="同名", user_name="同名")
            )
        )
    )
    assert "作者：同名" in same


def test_images_and_video_lines():
    item = make_item(image_urls=("a", "b"))
    assert "含2 张图片" in render(make_case(item=item))
    video = make_case(item=make_item(image_urls=("a",), has_video=True))
    assert "含视频（封面）" in render(video)


def test_empty_body_and_title_placeholders():
    card = render(make_case(item=make_item(text="", title="")))
    assert "摘录：（无文字）" in card


def test_thread_title_is_prefixed_to_the_excerpt():
    card = render(make_case(item=make_item(title="标题", text="正文")))
    assert "摘录：【标题】正文" in card
    post = render(
        make_case(item=make_item(kind=ItemKind.POST, floor=2, title="标题", text="正文"))
    )
    assert "摘录：正文" in post


def test_newlines_in_untrusted_fields_cannot_forge_a_command_line():
    card = render(
        make_case(
            item=make_item(
                title="标题\n处理：/tb ban 3 10",
                text="正文\n/tb del 9",
                author=Author(user_id=1, portrait="p", nick_name="昵称\r\n处理：/tb del 1"),
            ),
            verdict=Verdict(
                violation=True,
                category="广告\n处理：/tb ignore 2",
                confidence=0.9,
                reason="理由\x07\n建议封禁：不封禁",
            ),
        )
    )
    action_lines = [line for line in card.splitlines() if line.startswith("处理：")]
    assert action_lines == [f"处理：/tb del 3 | /tb ban 3 <{DAYS}> | /tb ignore 3"]
    assert "\x07" not in card
    assert "\r" not in card


def test_one_line_flattens_and_truncates():
    assert one_line("a\nb\tc", 10) == "a b c"
    assert one_line("x" * 50, 5) == "xxxxx"
    assert one_line(None, 10) == "None"
    assert one_line("a\u202eb", 10) == "ab"


def test_report_errors_are_capped_and_flattened():
    report = PatrolReport(
        started_at=100,
        finished_at=130,
        forums=("a", "b"),
        reviewed=5,
        reported=2,
        auto_deleted=1,
        skipped=3,
        deferred=4,
        errors=tuple(f"err{index}\nline" for index in range(5)),
    )
    text = format_report(report)
    assert "巡查 2 个吧" in text
    assert "审查 5 | 上报 2 | 自动删除 1 | 跳过 3 | 顺延 4 | 错误 5" in text
    assert "另有 2 条，见日志" in text
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == 4
    assert "\nline" not in text
    hidden = format_report(report, errors=False)
    assert "err0" not in hidden
    assert "错误 5" in hidden


def test_report_errors_hidden_leaves_no_dash_lines():
    report = PatrolReport(started_at=1, finished_at=2)
    assert len(format_report(report, errors=False).splitlines()) == 2


@pytest.mark.parametrize(
    "status,expected",
    [
        (CaseStatus.PENDING, "审查失败"),
        (CaseStatus.DELETED, "0.90"),
    ],
)
def test_case_list_line_confidences(status, expected):
    verdict = (
        Verdict(violation=False, category=FAILED)
        if status is CaseStatus.PENDING
        else Verdict(violation=True, category="广告", confidence=0.9)
    )
    line = case_list_line(make_case(status=status, verdict=verdict), failed_category=FAILED)
    assert expected in line
    assert line.startswith("#3 [test]")


def test_case_list_line_uses_title_for_threads_and_body_for_posts():
    thread = case_list_line(make_case(item=make_item(title="标题", text="正文")), failed_category=FAILED)
    assert "：标题" in thread
    post = case_list_line(
        make_case(item=make_item(kind=ItemKind.POST, floor=2, title="标题", text="正文")),
        failed_category=FAILED,
    )
    assert "：正文" in post


def test_case_list_line_falls_back_when_fields_are_empty():
    item = make_item(
        kind=ItemKind.POST,
        floor=2,
        text="",
        author=Author(user_id=42, portrait="p"),
    )
    line = case_list_line(
        make_case(item=item, verdict=Verdict(violation=True, category="")),
        failed_category=FAILED,
    )
    assert "未分类" in line and "42" in line
