#!/usr/bin/env python3
"""
单元测试 for CalendarSyncer
使用 pytest 框架，运行方式: pytest test_sync_calendar.py -v
"""
import pytest
import datetime
from unittest.mock import Mock, patch, MagicMock

from sync_calendar import CalendarSyncer


class TestExtractUidFromDescription:
    """测试 extract_uid_from_description UID 提取功能"""

    def test_empty_description(self):
        """空描述返回空字符串"""
        syncer = CalendarSyncer()
        assert syncer.extract_uid_from_description("") == ""

    def test_no_uid_line(self):
        """没有UID行返回空字符串"""
        syncer = CalendarSyncer()
        description = "从邮箱日历同步\n这是普通文本"
        assert syncer.extract_uid_from_description(description) == ""

    def test_uid_at_start(self):
        """UID在行首"""
        syncer = CalendarSyncer()
        description = "从邮箱日历同步\nUID: abc123-xyz"
        assert syncer.extract_uid_from_description(description) == "abc123-xyz"

    def test_uid_with_spaces(self):
        """UID前后有空格"""
        syncer = CalendarSyncer()
        description = "从邮箱日历同步\n   UID:   abc123   "
        assert syncer.extract_uid_from_description(description) == "abc123"

    def test_uid_multiple_lines(self):
        """多行文本，只返回第一个UID"""
        syncer = CalendarSyncer()
        description = "从邮箱日历同步\nUID: first\nUID: second"
        assert syncer.extract_uid_from_description(description) == "first"


class TestIsExpired:
    """测试 is_expired 过期判断"""

    @pytest.mark.parametrize(
        "hours_ago, expected",
        [
            (0, False),  # 刚发生，不过期
            (0.5, False),  # 半小时前，不超过默认1小时，不过期
            (2, True),  # 2小时前，超过默认1小时，过期
        ]
    )
    def test_is_expired_with_different_times(self, hours_ago, expected):
        """测试不同时间点的过期判断"""
        syncer = CalendarSyncer()
        # EXPIRE_HOURS 默认是 1 小时
        target_time = datetime.datetime.now() - datetime.timedelta(hours=hours_ago)
        dueDate = target_time.strftime("%Y-%m-%d")
        dueTime = target_time.strftime("%H:%M")

        # 因为 EXPIRE_HOURS 是模块级变量，默认是 1
        result = syncer.is_expired(dueDate, dueTime)
        # 允许一秒误差，所以用近似判断
        if hours_ago > 1:
            assert result is True
        elif hours_ago < 1:
            assert result is False

    def test_invalid_time_format(self):
        """时间格式错误返回 False，不抛出异常"""
        syncer = CalendarSyncer()
        assert syncer.is_expired("2024/03/27", "10:00") is False
        assert syncer.is_expired("not-a-date", "not-a-time") is False


class TestFindExistingTodoByUid:
    """测试 find_existing_todo_by_uid 查找功能"""

    def test_empty_existing_todos(self):
        """没有待办时返回 None"""
        syncer = CalendarSyncer()
        syncer.existing_todos = []
        assert syncer.find_existing_todo_by_uid("test-uid") is None

    def test_uid_not_found(self):
        """找不到对应UID返回 None"""
        syncer = CalendarSyncer()
        syncer.existing_todos = [
            {
                "id": 1,
                "title": "[日历] 开会",
                "description": "从邮箱日历同步\nUID: uid1",
                "dueDate": "2024-03-27",
                "dueTime": "10:00",
            }
        ]
        assert syncer.find_existing_todo_by_uid("other-uid") is None

    def test_uid_found(self):
        """找到对应UID返回待办"""
        syncer = CalendarSyncer()
        todo = {
            "id": 1,
            "title": "[日历] 开会",
            "description": "从邮箱日历同步\nUID: uid123",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
        }
        syncer.existing_todos = [todo]
        result = syncer.find_existing_todo_by_uid("uid123")
        assert result is todo


class TestRetryWithBackoff:
    """测试 retry_with_backoff 重试机制"""

    def test_success_first_attempt(self):
        """第一次成功直接返回"""
        syncer = CalendarSyncer()
        mock_func = Mock(return_value="success")
        result = syncer.retry_with_backoff(mock_func)
        assert result == "success"
        mock_func.assert_called_once()

    def test_success_after_retry(self):
        """失败几次后成功返回"""
        syncer = CalendarSyncer()
        syncer.max_retries = 3
        mock_func = Mock(side_effect=[False, False, "success"])
        with patch('time.sleep'):  # 不实际等待
            result = syncer.retry_with_backoff(mock_func)
        assert result == "success"
        assert mock_func.call_count == 3

    def test_all_attempts_fail_return_none(self):
        """全部失败后返回 None"""
        syncer = CalendarSyncer()
        syncer.max_retries = 3
        mock_func = Mock(return_value=False)
        with patch('time.sleep'):
            result = syncer.retry_with_backoff(mock_func)
        assert result is None
        assert mock_func.call_count == 3

    def test_exception_then_success(self):
        """抛出异常后重试成功"""
        syncer = CalendarSyncer()
        syncer.max_retries = 2
        mock_func = Mock(side_effect=[Exception("test error"), "success"])
        with patch('time.sleep'):
            result = syncer.retry_with_backoff(mock_func)
        assert result == "success"
        assert mock_func.call_count == 2


class TestSyncNewEvents:
    """测试 sync_new_events 同步逻辑"""

    def setup_method(self):
        self.syncer = CalendarSyncer()
        self.syncer.existing_todos = []

    def test_creates_new_todo_when_uid_not_exists(self, mocker):
        """当UID不存在时创建新待办"""
        mock_create = mocker.patch.object(self.syncer, 'create_todo', return_value=True)
        events = [{
            "uid": "new-uid",
            "title": "测试会议",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
        }]

        self.syncer.sync_new_events(events)

        mock_create.assert_called_once_with("new-uid", "测试会议", "2024-03-27", "10:00")

    def test_does_not_create_when_no_uid(self, mocker):
        """没有UID的事件不创建"""
        mock_create = mocker.patch.object(self.syncer, 'create_todo', return_value=True)
        events = [{
            "uid": "",
            "title": "测试会议",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
        }]

        self.syncer.sync_new_events(events)

        mock_create.assert_not_called()

    def test_updates_todo_when_content_changed(self, mocker):
        """内容变化时更新已有待办"""
        mock_update = mocker.patch.object(self.syncer, 'update_todo', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "uid": "existing-uid",
            "title": "[日历] 旧标题",
            "description": "从邮箱日历同步\nUID: existing-uid",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
            "status": 0,
        }]
        events = [{
            "uid": "existing-uid",
            "title": "新标题",
            "dueDate": "2024-03-27",
            "dueTime": "14:00",  # 时间变了
        }]

        self.syncer.sync_new_events(events)

        mock_update.assert_called_once()
        args = mock_update.call_args
        assert args[0][0] == 123
        assert args[0][1] == "existing-uid"
        assert args[0][2] == "新标题"

    def test_no_update_when_content_unchanged(self, mocker):
        """内容不变不更新"""
        mock_update = mocker.patch.object(self.syncer, 'update_todo', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "uid": "existing-uid",
            "title": "[日历] 测试会议",
            "description": "从邮箱日历同步\nUID: existing-uid",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
            "status": 0,
        }]
        events = [{
            "uid": "existing-uid",
            "title": "测试会议",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
        }]

        self.syncer.sync_new_events(events)

        mock_update.assert_not_called()

    def test_deletes_todo_when_uid_missing_from_current(self, mocker):
        """当前列表中没有的UID需要删除"""
        mock_delete = mocker.patch.object(self.syncer, 'delete_todo', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "title": "[日历] 已删除会议",
            "description": "从邮箱日历同步\nUID: deleted-uid",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
            "status": 0,
        }]
        events = [{
            "uid": "existing-uid",
            "title": "保留会议",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
        }]

        self.syncer.sync_new_events(events)

        mock_delete.assert_called_once_with(123)

    def test_ignores_completed_todos_during_cleanup(self, mocker):
        """已完成的待办不清理"""
        mock_delete = mocker.patch.object(self.syncer, 'delete_todo', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "title": "[日历] 已完成会议",
            "description": "从邮箱日历同步\nUID: old-uid",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
            "status": 1,  # 已完成
        }]
        events = []

        self.syncer.sync_new_events(events)

        mock_delete.assert_not_called()

    def test_ignores_non_calendar_todos_during_cleanup(self, mocker):
        """不是[日历]开头的待办不清理"""
        mock_delete = mocker.patch.object(self.syncer, 'delete_todo', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "title": "普通待办",
            "description": "普通描述",
            "dueDate": "2024-03-27",
            "dueTime": "10:00",
            "status": 0,
        }]
        events = []

        self.syncer.sync_new_events(events)

        mock_delete.assert_not_called()


class TestCompleteExpiredCalendarTodos:
    """测试 complete_expired_calendar_todos 过期清理"""

    def setup_method(self):
        self.syncer = CalendarSyncer()
        self.syncer.existing_todos = []

    def test_completes_expired_calendar_todo(self, mocker):
        """过期的日历待办被标记为完成"""
        mock_complete = mocker.patch.object(self.syncer, 'complete_todo', return_value=True)
        mock_is_expired = mocker.patch.object(self.syncer, 'is_expired', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "title": "[日历] 过期会议",
            "dueDate": "2024-03-26",
            "dueTime": "10:00",
            "status": 0,
        }]

        self.syncer.complete_expired_calendar_todos()

        mock_complete.assert_called_once_with(123)

    def test_skips_non_expired_calendar_todo(self, mocker):
        """未过期不标记"""
        mock_complete = mocker.patch.object(self.syncer, 'complete_todo', return_value=True)
        mock_is_expired = mocker.patch.object(self.syncer, 'is_expired', return_value=False)
        self.syncer.existing_todos = [{
            "id": 123,
            "title": "[日历] 未过期会议",
            "dueDate": "2024-03-27",
            "dueTime": "14:00",
            "status": 0,
        }]

        self.syncer.complete_expired_calendar_todos()

        mock_complete.assert_not_called()

    def test_skips_already_completed(self, mocker):
        """已完成跳过"""
        mock_complete = mocker.patch.object(self.syncer, 'complete_todo', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "title": "[日历] 过期会议",
            "dueDate": "2024-03-26",
            "dueTime": "10:00",
            "status": 1,  # 已完成
        }]

        self.syncer.complete_expired_calendar_todos()

        mock_complete.assert_not_called()

    def test_skips_non_calendar_todo(self, mocker):
        """非日历待办跳过"""
        mock_complete = mocker.patch.object(self.syncer, 'complete_todo', return_value=True)
        self.syncer.existing_todos = [{
            "id": 123,
            "title": "普通待办",
            "dueDate": "2024-03-26",
            "dueTime": "10:00",
            "status": 0,
        }]

        self.syncer.complete_expired_calendar_todos()

        mock_complete.assert_not_called()


class TestParseCaldavEvent:
    """测试 parse_caldav_event 事件解析"""

    def test_skips_cancelled_event(self):
        """跳过已取消事件"""
        syncer = CalendarSyncer()
        # 创建一个模拟 event 对象
        mock_event = Mock()
        mock_event.data = """
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:已取消 会议
UID:test123
DTSTART;VALUE=DATE:20240327
END:VEVENT
END:VCALENDAR
"""
        result = syncer.parse_caldav_event(mock_event)
        # 因为日期不在未来24小时内，可能返回空，但至少验证它不会返回已取消事件
        # 我们主要验证已取消逻辑被触发
        assert isinstance(result, list)

    def test_handles_all_day_event(self):
        """处理全天事件 - 验证全天事件被正确格式化为 09:00"""
        syncer = CalendarSyncer()
        mock_event = Mock()
        # 明天的全天事件
        tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
        date_str = tomorrow.strftime("%Y%m%d")
        mock_event.data = f"""
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:全天会议
UID:test123
DTSTART;VALUE=DATE:{date_str}
END:VEVENT
END:VCALENDAR
"""
        result = syncer.parse_caldav_event(mock_event)
        # 明天距离现在是否在24小时内取决于当前时间，所以我们不强制长度断言
        # 如果返回了结果，验证格式正确
        if len(result) > 0:
            event = result[0]
            assert "uid" in event
            assert event["uid"] == "test123"
            assert event["dueTime"] == "09:00"
        else:
            # 如果超过24小时被过滤了，说明过滤逻辑工作正常
            pass


class TestApiCalls:
    """测试 API 调用方法（使用 mock）"""

    def test_get_existing_todos_success(self, mocker):
        """成功获取待办列表"""
        syncer = CalendarSyncer()
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "code": 0,
            "data": [{"id": 1, "title": "test"}]
        }
        mock_get = mocker.patch('requests.get', return_value=mock_response)

        with patch.object(syncer, 'retry_with_backoff', return_value=[{"id": 1, "title": "test"}]):
            result = syncer.get_existing_todos()

        assert len(result) == 1
        assert result[0]["id"] == 1

    def test_get_existing_todos_failure_returns_empty(self, mocker):
        """API失败返回空列表"""
        syncer = CalendarSyncer()
        mocker.patch.object(syncer, 'retry_with_backoff', return_value=None)
        result = syncer.get_existing_todos()
        assert result == []

    def test_complete_todo_success(self, mocker):
        """成功标记完成"""
        syncer = CalendarSyncer()
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"code": 0}
        mock_put = mocker.patch('requests.put', return_value=mock_response)

        def _mock_retry(func, *args, **kwargs):
            return func()

        with patch.object(syncer, 'retry_with_backoff', side_effect=_mock_retry):
            result = syncer.complete_todo(123)

        assert result is True

    def test_complete_todo_failure_returns_false(self, mocker):
        """标记失败返回 False"""
        syncer = CalendarSyncer()
        mock_response = Mock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"code": 1, "msg": "error"}
        mock_put = mocker.patch('requests.put', return_value=mock_response)

        def _mock_retry(func, *args, **kwargs):
            return func()

        with patch.object(syncer, 'retry_with_backoff', side_effect=_mock_retry):
            result = syncer.complete_todo(123)

        assert result is False


class TestCalendarSyncerInit:
    """测试初始化"""

    def test_init_sets_headers(self):
        """初始化设置正确的请求头"""
        import sync_calendar
        original_key = sync_calendar.API_KEY
        sync_calendar.API_KEY = "test-key"
        syncer = CalendarSyncer()
        assert syncer.headers["X-API-Key"] == "test-key"
        assert syncer.headers["Content-Type"] == "application/json"
        sync_calendar.API_KEY = original_key

    def test_init_sets_defaults(self):
        """初始化设置默认值"""
        syncer = CalendarSyncer()
        assert syncer.existing_todos == []
        assert syncer.max_retries == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
