import threading
import time
import unittest
from unittest.mock import Mock, patch
from app import (
    DEFAULT_DEVICE_TAGS, _clean_submission_url, _douyin_content_id, _dynamic_submission_data, _initialize_control,
    _parse_bilibili_submission, _parse_douyin_submission, _progress_with_control, _update_control,
    _parse_xhs_submission, _submission_result, _looks_like_dji_device_tag,
    _xhs_notes_from_dom, build_result, detect_platform, detect_submission_platform, excel_rows,
    extract_tags, fetch_accounts, fetch_submission, fetch_submissions, normalize_work, normalize_xhs_note, parse_like_count, submission_excel_rows,
    validate_account_url,
)


class ParserTests(unittest.TestCase):
    def setUp(self):
        # Parser tests must never write reviews into the user's real monthly ledger.
        guard = patch('app.MONTHLY_GOAL.register')
        guard.start()
        self.addCleanup(guard.stop)

    def test_like_count(self):
        self.assertEqual(parse_like_count("1.2万"), 12000)
        self.assertEqual(parse_like_count("3.4w"), 34000)
        self.assertEqual(parse_like_count("1,001"), 1001)

    def test_tags_are_unique(self):
        self.assertEqual(extract_tags("出发 #航拍 #旅行 #航拍"), ["航拍", "旅行"])

    def test_normalize(self):
        work = normalize_work({"aweme_id":"123", "create_time":1700000000, "desc":"你好 #大疆", "statistics":{"digg_count":1001}})
        self.assertIsNotNone(work)
        self.assertTrue(work.over_1000)
        self.assertEqual(work.tags, ["大疆"])

    def test_result_distinguishes_unavailable(self):
        result = build_result("https://www.douyin.com/user/MS4wLjABAAAAx", [])
        self.assertEqual(result["data_status"], "unavailable")
        self.assertFalse(result["has_updates"])

    def test_url_validation(self):
        self.assertTrue(validate_account_url("https://www.douyin.com/user/MS4wLjABAAAAx"))
        self.assertEqual(
            detect_platform("https://www.xiaohongshu.com/user/profile/668ea788000000000f034258"),
            "xiaohongshu",
        )
        with self.assertRaises(Exception):
            validate_account_url("https://example.com/user/x")

    def test_normalize_xhs_note(self):
        work = normalize_xhs_note({
            "id": "668ea788000000000f034258",
            "note_card": {
                "display_title": "航拍旅行 #大疆 #风景",
                "interact_info": {"liked_count": "1.2万"},
                "tag_list": [{"name": "旅行"}],
            },
        })
        self.assertIsNotNone(work)
        self.assertTrue(work.over_1000)
        self.assertEqual(work.tags, ["大疆", "风景", "旅行"])
        self.assertGreater(work.published_timestamp, 1_700_000_000)

    def test_xhs_result_has_platform(self):
        result = build_result(
            "https://www.xiaohongshu.com/user/profile/example", [], platform="xiaohongshu"
        )
        self.assertEqual(result["platform"], "小红书")

    def test_xhs_dom_fallback(self):
        notes = _xhs_notes_from_dom([{
            "url": "https://www.xiaohongshu.com/explore/668ea788000000000f034258",
            "text": "航拍旅行 #大疆\n水煮鱼\n1.2万",
            "title": "航拍旅行 #大疆",
        }])
        self.assertEqual(len(notes), 1)
        work = normalize_xhs_note(notes[0])
        self.assertIsNotNone(work)
        self.assertEqual(work.likes, 12000)
        self.assertEqual(work.tags, ["大疆"])

    def test_excel_hot_source_uses_tags(self):
        result = {"accounts": [{
            "data_status": "ok",
            "works": [
                {"description": "作品一", "tags": ["航拍", "大疆"], "over_1000": True},
                {"description": "作品二", "tags": ["航拍", "旅行"], "over_1000": False},
            ],
        }]}
        row = excel_rows(result)[0]
        self.assertEqual(row["本周是否有发作品"], "是")
        self.assertEqual(row["是否有爆款"], "是")
        self.assertEqual(row["爆款来源"], "#航拍、#大疆")

    def test_submission_platform_detection(self):
        self.assertEqual(detect_submission_platform("https://v.douyin.com/example/"), "douyin")
        self.assertEqual(detect_submission_platform("http://xhslink.com/o/example"), "xiaohongshu")
        self.assertEqual(detect_submission_platform("https://xhslink.cn/o/example"), "xiaohongshu")
        self.assertEqual(detect_submission_platform("https://b23.tv/example"), "bilibili")
        self.assertEqual(detect_submission_platform("https://weixin.qq.com/sph/example"), "weixin")

    def test_submission_url_cleanup_accepts_share_text_and_markdown_escapes(self):
        self.assertEqual(
            _clean_submission_url("https://v.douyin.com/d0waCCL1lgw/ 10/03 aaa:/ :7pm"),
            "https://v.douyin.com/d0waCCL1lgw/",
        )
        self.assertEqual(
            _clean_submission_url("[https://x.test/a\\_b?x=1\\&y=2](https://x.test/ignored)"),
            "https://x.test/a_b?x=1&y=2",
        )

    def test_xhs_submission_parser(self):
        note_id = "6a54eb29000000000702e3f5"
        page = (
            '<meta name="description" content="fallback">'
            '{"title":"操场出片","desc":"#大疆Pocket4[话题]##大疆校园暑期出片计划[话题]#",'
            f'"likedCount":"120","commentCount":"30","collectedCount":"40","shareCount":"11","noteId":"{note_id}"}}'
        )
        data = _parse_xhs_submission(page, f"https://www.xiaohongshu.com/discovery/item/{note_id}")
        self.assertEqual(data["title"], "操场出片")
        self.assertEqual(data["metrics"]["likes"], 120)
        self.assertEqual(sum(data["metrics"].values()), 201)
        self.assertIn("大疆Pocket4", data["tags"])

    def test_xhs_dynamic_parser_uses_target_note_and_author(self):
        target_id = "6a9ade8c000000002a005458"
        page = {"payloads": [{"items": [
            {"id": "6a984623000000002b0022d5", "noteCard": {
                "title": "推荐作品", "desc": "#其他", "interactInfo": {"likedCount": "9999"},
                "user": {"nickname": "错误博主", "redId": "wrong"},
            }},
            {"id": target_id, "noteCard": {
                "title": "目标作品", "desc": "#大疆Pocket4 #活动", "interactInfo": {
                    "likedCount": "120", "commentCount": "30", "collectedCount": "40", "shareCount": "11",
                }, "user": {"nickname": "目标博主", "redId": "xhs-id-1"},
            }},
        ]}]}
        data = _dynamic_submission_data("xiaohongshu", page, target_id)
        self.assertEqual(data["title"], "目标作品")
        self.assertEqual(data["metrics"]["likes"], 120)
        self.assertEqual(data["author_name"], "目标博主")
        self.assertEqual(data["author_id"], "xhs-id-1")
        missing = _dynamic_submission_data("xiaohongshu", page, "aaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertFalse(missing["metrics_available"])
        self.assertEqual(missing["metrics"]["likes"], 0)
        comment_only = _dynamic_submission_data("xiaohongshu", {
            "payloads": [{"comments": [{"note_id": target_id, "content": "留言", "user_info": {
                "nickname": "评论者", "user_id": "commenter",
            }}]}],
        }, target_id)
        self.assertFalse(comment_only["metrics_available"])
        self.assertEqual(comment_only["content_status"], "unavailable")

    def test_xhs_dynamic_parser_reads_open_detail_dom(self):
        page = {"detailData": {
            "title": "女大翻唱", "description": "#大疆pocket3 #用大疆打开我的新学期",
            "authorName": "利利顺顺小王", "authorHref": "/user/profile/5a84185711be1019e878af70?xsec_token=x",
            "likes": "52", "collects": "12", "comments": "13", "shares": "",
        }, "payloads": []}
        data = _dynamic_submission_data("xiaohongshu", page, "6a9ade8c000000002a005458")
        self.assertEqual(data["author_name"], "利利顺顺小王")
        self.assertEqual(data["author_id"], "5a84185711be1019e878af70")
        self.assertEqual(data["metrics"]["likes"], 52)
        self.assertEqual(data["metrics"]["collects"], 12)
        self.assertEqual(data["metrics"]["comments"], 13)
        self.assertTrue(data["metrics_available"])
        self.assertEqual(data["content_status"], "available")

    @patch("app.XHS_COLLECTOR.collect_submission_page")
    @patch("app.requests.Session")
    def test_xhs_http_failure_uses_xhs_login_collector(self, session_class, collect_page):
        note_id = "6a9ade8c000000002a005458"
        url = f"https://www.xiaohongshu.com/discovery/item/{note_id}?xsec_token=token%3D"
        response = Mock(ok=False, status_code=403, url=url, history=[])
        session_class.return_value.get.return_value = response
        collect_page.return_value = {"url": url, "payloads": [{"data": {"id": note_id, "noteCard": {
            "title": "目标作品", "desc": "#大疆Pocket4 #活动", "interactInfo": {
                "likedCount": "120", "commentCount": "30", "collectedCount": "40", "shareCount": "11",
            }, "user": {"nickname": "博主乙", "redId": "xhs-002"},
        }}}]}
        result = fetch_submission(url, ["活动"], DEFAULT_DEVICE_TAGS)
        collect_page.assert_called_once_with(url)
        self.assertEqual(result["content_status"], "available")
        self.assertEqual(result["author_name"], "博主乙")
        self.assertEqual(result["interaction_count"], 201)

    @patch("app.DOUYIN_COLLECTOR.collect_submission_page")
    @patch("app.requests.Session")
    def test_douyin_short_link_deleted_page_is_preserved(self, session_class, collect_page):
        short_url = "https://v.douyin.com/kqrE4nEIRw8/"
        resolved = "https://www.douyin.com/?modal_id=7999999999999999999"
        response = Mock(ok=True, status_code=200, url=resolved, history=[], text="")
        session_class.return_value.get.return_value = response
        collect_page.return_value = {
            "url": "https://www.douyin.com/", "title": "作品不存在", "text": "视频不见了", "payloads": [],
        }
        result = fetch_submission(short_url, ["活动"], DEFAULT_DEVICE_TAGS)
        self.assertEqual(result["content_status"], "deleted")
        self.assertEqual(result["submission_status"], "已删除")
        self.assertEqual(result["original_url"], short_url)

    def test_bilibili_submission_parser_and_formula(self):
        state = (
            '{"videoData":{"title":"投稿","desc":"#大疆校园暑期出片计划",'
            '"stat":{"like":100,"coin":20,"reply":30,"favorite":40,"share":11},'
            '"owner":{"name":"B站博主","mid":12345}},'
            '"tags":[{"tag_name":"大疆Pocket4"}]}'
        )
        data = _parse_bilibili_submission(f"<script>window.__INITIAL_STATE__={state};(function(){{}})()</script>")
        result = _submission_result(
            "https://b23.tv/example", "https://www.bilibili.com/video/example", "bilibili",
            data, "#大疆校园暑期出片计划", DEFAULT_DEVICE_TAGS,
        )
        self.assertEqual(result["interaction_count"], 201)
        self.assertTrue(result["over_200"])
        self.assertTrue(result["tag_ok"])
        self.assertEqual(result["author_name"], "B站博主")
        self.assertEqual(result["author_id"], "12345")
        self.assertEqual(result["interaction_formula"], "点赞 + 投币 + 评论 + 收藏 + 分享")

    def test_submission_requires_strictly_more_than_200(self):
        data = {
            "description": "#大疆Pocket4 #大疆校园暑期出片计划",
            "tags": ["大疆Pocket4", "大疆校园暑期出片计划"],
            "metrics": {"likes": 100, "comments": 50, "collects": 40, "shares": 10},
            "metrics_available": True,
        }
        result = _submission_result("u", "u", "douyin", data, "大疆校园暑期出片计划", DEFAULT_DEVICE_TAGS)
        self.assertFalse(result["over_200"])
        self.assertEqual(result["submission_status"], "满足投稿条件")

    def test_deleted_submission_has_explicit_status(self):
        data = {
            "title": "原作品已删除或不可访问", "description": "", "tags": [],
            "metrics": {}, "metrics_available": False, "content_status": "deleted",
        }
        result = _submission_result("u", "u", "douyin", data, ["活动"], DEFAULT_DEVICE_TAGS)
        self.assertEqual(result["content_status"], "deleted")
        self.assertEqual(result["submission_status"], "已删除")
        self.assertIsNone(result['over_10000'])

    def test_submission_ten_thousand_threshold_uses_total_and_strict_boundary(self):
        for total, expected in ((9999, False), (10000, False), (10001, True)):
            with self.subTest(total=total):
                data = {'description': '#大疆Pocket4 #活动', 'tags': ['大疆Pocket4', '活动'],
                        'metrics': {'likes': 9000, 'comments': total-9000}, 'metrics_available': True}
                result = _submission_result('u', 'u', 'douyin', data, ['活动'], DEFAULT_DEVICE_TAGS)
                self.assertEqual(result['interaction_count'], total)
                self.assertEqual(result['over_10000'], expected)
                self.assertTrue(result['over_200'])
        unknown = _submission_result('u', 'u', 'douyin', {'metrics_available': False}, ['活动'], DEFAULT_DEVICE_TAGS)
        self.assertIsNone(unknown['over_10000'])

    def test_ten_thousand_batch_statistics_and_excel_values(self):
        with patch('app.fetch_submission', side_effect=[
            {'tag_ok': True, 'over_200': True, 'over_10000': True},
            {'tag_ok': True, 'over_200': True, 'over_10000': False},
            {'tag_ok': False, 'over_200': None, 'over_10000': None},
        ]):
            result = fetch_submissions(['a', 'b', 'c'], ['活动'], DEFAULT_DEVICE_TAGS)
        self.assertEqual(result['interaction_passed'], 2)
        self.assertEqual(result['interaction_10000_passed'], 1)
        rows = submission_excel_rows({'submissions': [
            {'over_10000': True}, {'over_10000': False}, {'over_10000': None},
            {'content_status': 'deleted', 'over_10000': True},
        ]})
        self.assertEqual([row['是否大于1万互动量'] for row in rows], ['是', '否', '无法确认', '已删除'])

    def test_multiple_campaign_tags_accept_any_match(self):
        data = {
            "description": "#大疆Pocket4 #第二期校园计划",
            "tags": ["大疆Pocket4", "第二期校园计划"],
            "metrics": {"likes": 20, "comments": 2, "collects": 1, "shares": 0},
            "metrics_available": True,
        }
        result = _submission_result(
            "u", "u", "xiaohongshu", data,
            ["第一期校园计划", "第二期校园计划"], DEFAULT_DEVICE_TAGS,
        )
        self.assertTrue(result["campaign_tag_ok"])
        self.assertTrue(result["tag_ok"])
        self.assertEqual(result["matched_campaign_tag"], "第二期校园计划")

    def test_submission_progress_reports_each_item_and_completion(self):
        updates = []
        result = fetch_submissions(
            ["https://example.com/one", "https://example.com/two"],
            ["活动一", "活动二"], DEFAULT_DEVICE_TAGS,
            progress_callback=lambda current, total, message, done: updates.append(
                (current, total, message, done)
            ),
        )
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["campaign_tags"], ["活动一", "活动二"])
        self.assertEqual(updates[-1][:2], (2, 2))
        self.assertTrue(updates[-1][3])

    def test_submission_batch_has_no_fifty_item_limit(self):
        urls = [f"https://example.com/submission-{index}" for index in range(55)]
        result = fetch_submissions(urls, ["活动"], DEFAULT_DEVICE_TAGS)
        self.assertEqual(result["total"], 55)

    def test_submission_batch_keeps_duplicate_input_rows(self):
        url = "https://example.com/same-submission"
        result = fetch_submissions([url, url], ["活动"], DEFAULT_DEVICE_TAGS)
        self.assertEqual(result["requested_total"], 2)
        self.assertEqual(result["total"], 2)

    def test_submission_cancel_keeps_completed_results(self):
        result = fetch_submissions(
            [f"https://example.com/submission-{index}" for index in range(5)],
            ["活动"], DEFAULT_DEVICE_TAGS,
            progress_callback=lambda current, total, message, done: not done and current >= 2,
        )
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["requested_total"], 5)
        self.assertEqual(result["total"], 2)

    def test_weekly_cancel_keeps_completed_results(self):
        result = fetch_accounts(
            [f"https://example.com/account-{index}" for index in range(3)],
            progress_callback=lambda current, total, message, done: not done and current >= 1,
        )
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["requested_total"], 3)
        self.assertEqual(result["total"], 1)

    def test_pause_resume_and_cancel_control(self):
        token = "test-pause-resume-control"
        _initialize_control(token)
        _update_control(token, "pause")
        returned = []
        worker = threading.Thread(
            target=lambda: returned.append(_progress_with_control(token, 1, 3, "读取中")),
            daemon=True,
        )
        worker.start()
        time.sleep(0.05)
        self.assertTrue(worker.is_alive())
        _update_control(token, "resume")
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(returned, [False])
        _update_control(token, "cancel")
        self.assertTrue(_progress_with_control(token, 1, 3, "读取中"))

    def test_douyin_public_fallback_keeps_tags(self):
        page = '<meta name="description" content="一片云朵 #大疆Pocket4 #大疆校园暑期出片计划 - 作者于20260901发布在抖音">'
        data = _parse_douyin_submission(page, "https://www.douyin.com/video/123")
        self.assertIn("大疆Pocket4", data["tags"])
        self.assertIn("大疆校园暑期出片计划", data["tags"])
        self.assertFalse(data["metrics_available"])

    def test_unlisted_dji_model_tag_is_accepted(self):
        self.assertTrue(_looks_like_dji_device_tag("#大疆pocket4p", "#大疆校园暑期出片计划"))
        self.assertTrue(_looks_like_dji_device_tag("#大疆御4pro", "#大疆校园暑期出片计划"))
        self.assertFalse(_looks_like_dji_device_tag("#大疆校园暑期出片计划", "#大疆校园暑期出片计划"))
        data = {
            "description": "#大疆pocket4p #大疆校园暑期出片计划",
            "tags": ["大疆pocket4p", "大疆校园暑期出片计划"],
            "metrics": {"likes": 41, "comments": 5, "collects": 4, "shares": 0},
            "metrics_available": True,
        }
        result = _submission_result("u", "u", "xiaohongshu", data, "大疆校园暑期出片计划", DEFAULT_DEVICE_TAGS)
        self.assertTrue(result["device_tag_ok"])
        self.assertTrue(result["tag_ok"])
        self.assertEqual(result["matched_device_tag"], "大疆pocket4p")

    def test_weixin_fmt_metrics_and_quality_status(self):
        page = {"title": "视频号", "text": "", "payloads": [{"data": {"feedInfo": {
            "description": "#大疆Mini5pro #大疆校园暑期出片计划",
            "likeCountFmt": "120", "commentCountFmt": "41", "favCountFmt": "40",
            "forwardCountFmt": "999",
        }}}]}
        data = _dynamic_submission_data("weixin", page)
        self.assertEqual(data["metrics"]["likes"], 120)
        self.assertEqual(data["metrics"]["comments"], 41)
        self.assertEqual(data["metrics"]["hearts"], 40)
        result = _submission_result("u", "u", "weixin", data, "大疆校园暑期出片计划", DEFAULT_DEVICE_TAGS)
        self.assertEqual(result["interaction_count"], 201)
        self.assertEqual(result["submission_status"], "优质作品")

    def test_douyin_target_id_supports_note_and_rejects_other_payload(self):
        self.assertEqual(_douyin_content_id("https://www.douyin.com/note/7667505643712448251"), "7667505643712448251")
        page = {
            "url": "https://www.douyin.com/video/999999",
            "title": "其他视频", "text": "", "description": "#其他作品",
            "payloads": [{"aweme_list": [{
                "aweme_id": "999999", "create_time": 1785000000, "desc": "#其他作品",
                "statistics": {"digg_count": 9999, "comment_count": 999},
            }]}],
        }
        data = _dynamic_submission_data("douyin", page, "7667505643712448251")
        self.assertFalse(data["metrics_available"])
        self.assertEqual(data["metrics"]["likes"], 0)

    def test_submission_excel_includes_author_link_and_deleted_status(self):
        rows = submission_excel_rows({"submissions": [
            {"platform": "抖音", "author_name": "博主甲", "author_id": "douyin-1", "original_url": "https://v.douyin.com/a/",
             "content_status": "deleted", "tag_data_available": False, "tag_ok": False, "over_200": None,
             "warnings": ["原作品已删除"]},
            {"tag_data_available": True, "tag_ok": True, "over_200": True, "content_status": "available"},
            {"tag_data_available": True, "tag_ok": False, "over_200": False},
            {"tag_data_available": False, "tag_ok": False, "over_200": None},
        ]})
        self.assertEqual(list(rows[0]), ["平台", "博主名字", "博主ID", "作品链接", "作品状态", "是否符合投稿要求", "是否大于200互动量", "是否大于1万互动量", "说明"])
        self.assertEqual(rows[0]["作品状态"], "已删除")
        self.assertEqual(rows[0]["是否符合投稿要求"], "已删除")
        self.assertEqual(rows[0]["是否大于200互动量"], "已删除")
        self.assertEqual(rows[0]['是否大于1万互动量'], '已删除')
        self.assertEqual(rows[0]["博主名字"], "博主甲")
        self.assertEqual(rows[0]["博主ID"], "douyin-1")
        self.assertEqual(rows[1]["是否符合投稿要求"], "是")
        self.assertEqual(rows[2]["是否大于200互动量"], "否")
        self.assertEqual(rows[3]["作品状态"], "无法读取")


if __name__ == "__main__":
    unittest.main()
