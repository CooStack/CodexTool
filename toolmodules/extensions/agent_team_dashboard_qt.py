from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_team_dashboard_runtime import (
    DEFAULT_POLL_INTERVAL_MS,
    DEFAULT_WINDOW_TITLE,
    _DEFAULT_THEME as _THEME,
    _compact_inline_text,
    _render_markdown_as_text,
    _render_role_activity_as_text,
    append_dashboard_event,
    append_role_stream_chunk,
    collect_dashboard_snapshot,
    commit_role_document_draft,
    mark_run_completed,
    write_role_document_draft,
)


def _load_qt() -> tuple[Any, ...]:
    from PySide6.QtCore import QTimer, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (
        QApplication,
        QFrame,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSplitter,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    return QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QTabWidget, QTimer, QVBoxLayout, QWidget, Qt, QFont


_QT = _load_qt()
(
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTimer,
    QVBoxLayout,
    QWidget,
    Qt,
    QFont,
) = _QT


_STATUS_COLORS = {
    "pending": "#5B6B82",
    "streaming": _THEME["accent_secondary"],
    "editing": "#F59E0B",
    "reawakened": "#8B5CF6",
    "active": _THEME["accent_secondary"],
    "in_progress": _THEME["accent_secondary"],
    "completed": _THEME["success"],
    "approved": _THEME["success"],
    "done": _THEME["success"],
    "offline": _THEME["muted"],
    "blocked": _THEME["danger"],
    "failed": _THEME["danger"],
    "error": _THEME["danger"],
    "suspected_disconnect": _THEME["warning"],
}

_EVENT_LABELS = {
    "agent_spawned": "启动",
    "agent_reawakened": "恢复",
    "stream_chunk": "流式",
    "draft_replaced": "草稿",
    "draft_committed": "提交",
    "agent_status_changed": "状态",
    "agent_closed": "离线",
    "run_completed": "完成",
    "plan_step_upsert": "计划",
}


class AgentTeamDashboardQtWindow(QMainWindow):
    def __init__(
        self,
        state_path: Path,
        *,
        title: str | None = None,
        topmost: bool = True,
        bring_to_front: bool = True,
        poll_interval_ms: int | None = None,
    ) -> None:
        super().__init__()
        self.state_path = Path(state_path).expanduser().resolve()
        self.override_title = title.strip() if isinstance(title, str) and title.strip() else ""
        self.topmost = topmost
        self.bring_to_front = bring_to_front
        self.poll_interval_override = poll_interval_ms
        self.snapshot: dict[str, Any] | None = None
        self.selected_role_id = ""
        self._draft_dirty = False
        self._draft_binding_suspended = False
        self._loaded_role_id = ""
        self._last_signatures: dict[str, Any] = {}
        self._role_card_signatures: dict[str, tuple[Any, ...]] = {}
        self._timeline_card_signatures: dict[str, tuple[Any, ...]] = {}

        self.setWindowTitle(DEFAULT_WINDOW_TITLE)
        self.resize(1680, 980)
        self.setMinimumSize(1280, 760)
        if topmost:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self._apply_style_sheet()
        self._build_ui()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh)
        self._refresh(force=True)
        interval = self.poll_interval_override or DEFAULT_POLL_INTERVAL_MS
        self.timer.start(interval)

        if self.bring_to_front:
            self.raise_()
            self.activateWindow()

    def _apply_style_sheet(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow {{
                background: {_THEME['background']};
            }}
            QWidget {{
                color: {_THEME['text']};
                font-family: 'Segoe UI', 'Microsoft YaHei UI', sans-serif;
                font-size: 13px;
            }}
            QFrame#card {{
                background: {_THEME['surface']};
                border: 1px solid {_THEME['border']};
                border-radius: 18px;
            }}
            QFrame#threadShell {{
                background: {_THEME['surface_alt']};
                border: 1px solid {_THEME['border']};
                border-radius: 20px;
            }}
            QFrame#messageBubble {{
                background: {_THEME['card_alt']};
                border: 1px solid {_THEME['border']};
                border-radius: 18px;
            }}
            QLabel#eyebrow {{
                color: {_THEME['muted']};
                text-transform: uppercase;
                font-size: 11px;
                letter-spacing: 1px;
            }}
            QLabel#heroTitle {{
                font-size: 28px;
                font-weight: 700;
            }}
            QLabel#subtle {{
                color: {_THEME['muted']};
            }}
            QLabel#statusPill {{
                padding: 6px 10px;
                border-radius: 999px;
                font-weight: 700;
                background: {_THEME['accent_soft']};
                color: {_THEME['text']};
            }}
            QListWidget {{
                background: transparent;
                border: none;
                outline: none;
            }}
            QListWidget::item {{
                border: none;
                margin: 0px;
                padding: 0px;
            }}
            QTabWidget::pane {{
                border: 1px solid {_THEME['border']};
                border-radius: 14px;
                background: {_THEME['surface_alt']};
                top: -1px;
            }}
            QTabBar::tab {{
                background: {_THEME['surface']};
                color: {_THEME['muted']};
                padding: 10px 16px;
                margin-right: 4px;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }}
            QTabBar::tab:selected {{
                color: {_THEME['text']};
                background: {_THEME['surface_alt']};
            }}
            QPlainTextEdit, QTextBrowser, QLineEdit {{
                background: {_THEME['surface_alt']};
                border: 1px solid {_THEME['border']};
                border-radius: 14px;
                padding: 12px;
                selection-background-color: {_THEME['accent_soft']};
            }}
            QPushButton {{
                background: {_THEME['accent_soft']};
                border: 1px solid {_THEME['border']};
                border-radius: 12px;
                padding: 10px 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                border-color: {_THEME['accent']};
                background: #133D4A;
            }}
            QProgressBar {{
                background: {_THEME['surface_alt']};
                border: 1px solid {_THEME['border']};
                border-radius: 8px;
                text-align: center;
                min-height: 16px;
            }}
            QProgressBar::chunk {{
                border-radius: 7px;
                background: {_THEME['accent']};
            }}
            """
        )

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        header = QFrame(objectName="card")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 18, 20, 18)
        header_layout.setSpacing(10)
        self.eyebrow_label = QLabel("多 Agent 协作面板", objectName="eyebrow")
        self.title_label = QLabel(DEFAULT_WINDOW_TITLE, objectName="heroTitle")
        self.subtitle_label = QLabel("正在监听 docs/agent-team", objectName="subtle")
        self.activity_label = QLabel("等待事件...", objectName="subtle")
        self.summary_label = QLabel("已完成 0%")
        self.detail_label = QLabel("已完成 0/0", objectName="subtle")
        self.close_label = QLabel("", objectName="subtle")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        row = QHBoxLayout()
        row.addWidget(self.summary_label)
        row.addStretch(1)
        row.addWidget(self.detail_label)
        header_layout.addWidget(self.eyebrow_label)
        header_layout.addWidget(self.title_label)
        header_layout.addWidget(self.subtitle_label)
        header_layout.addWidget(self.activity_label)
        header_layout.addLayout(row)
        header_layout.addWidget(self.progress_bar)
        header_layout.addWidget(self.close_label)
        layout.addWidget(header)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        self.role_list = QListWidget()
        self.role_list.setSpacing(10)
        self.role_list.itemSelectionChanged.connect(self._on_role_selected)
        left_panel = self._panel_shell("活跃 Agents", self.role_list)
        splitter.addWidget(left_panel)

        self.timeline_list = QListWidget()
        self.timeline_list.setSpacing(12)
        center_panel = self._panel_shell("对话流", self.timeline_list)
        splitter.addWidget(center_panel)

        right_panel = QFrame(objectName="card")
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(18, 18, 18, 18)
        right_layout.setSpacing(12)
        self.role_title_label = QLabel("未选择 Agent")
        self.role_title_label.setStyleSheet("font-size: 20px; font-weight: 700;")
        self.role_meta_label = QLabel("", objectName="subtle")
        self.role_status_label = QLabel("待处理", objectName="statusPill")
        self.role_message_label = QLabel("", objectName="subtle")
        self.role_message_label.setWordWrap(True)
        meta_row = QHBoxLayout()
        meta_row.addWidget(self.role_meta_label, 1)
        meta_row.addWidget(self.role_status_label, 0, Qt.AlignRight)
        right_layout.addWidget(self.role_title_label)
        right_layout.addLayout(meta_row)
        right_layout.addWidget(self.role_message_label)

        self.tabs = QTabWidget()
        right_layout.addWidget(self.tabs, 1)

        draft_page = QWidget()
        draft_layout = QVBoxLayout(draft_page)
        draft_layout.setContentsMargins(12, 12, 12, 12)
        draft_layout.setSpacing(10)
        self.draft_editor = QPlainTextEdit()
        self.draft_editor.textChanged.connect(self._on_draft_changed)
        self.stream_input = QLineEdit()
        self.stream_input.setPlaceholderText("向当前 Agent 追加一段流式内容")
        button_row = QHBoxLayout()
        self.save_button = QPushButton("保存草稿")
        self.save_button.clicked.connect(self._save_draft)
        self.stream_button = QPushButton("追加流式内容")
        self.stream_button.clicked.connect(self._stream_snippet)
        self.commit_button = QPushButton("提交草稿")
        self.commit_button.clicked.connect(self._commit_draft)
        self.offline_button = QPushButton("标记离线")
        self.offline_button.clicked.connect(self._mark_offline)
        self.reawaken_button = QPushButton("重新唤醒")
        self.reawaken_button.clicked.connect(self._reawaken)
        self.finish_button = QPushButton("结束本轮")
        self.finish_button.clicked.connect(self._finish_run)
        for button in (
            self.save_button,
            self.stream_button,
            self.commit_button,
            self.offline_button,
            self.reawaken_button,
            self.finish_button,
        ):
            button_row.addWidget(button)
        draft_layout.addWidget(self.draft_editor, 1)
        draft_layout.addWidget(self.stream_input)
        draft_layout.addLayout(button_row)

        self.stream_view = self._build_readonly_view()
        self.activity_view = self._build_thread_list_view()
        self.committed_view = self._build_readonly_view()
        self.plan_view = self._build_readonly_view()
        self.interfaces_view = self._build_readonly_view()
        self.review_view = self._build_readonly_view()
        self.tabs.addTab(draft_page, "草稿编辑")
        self.tabs.addTab(self.activity_view, "活动输出")
        self.tabs.addTab(self.committed_view, "已提交内容")
        self.tabs.addTab(self.plan_view, "计划")
        self.tabs.addTab(self.interfaces_view, "接口")
        self.tabs.addTab(self.review_view, "审阅")
        splitter.addWidget(right_panel)
        splitter.setSizes([320, 540, 720])

    def _build_readonly_view(self) -> QPlainTextEdit:
        widget = QPlainTextEdit()
        widget.setReadOnly(True)
        widget.setUndoRedoEnabled(False)
        widget.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        return widget

    def _build_thread_list_view(self) -> QListWidget:
        widget = QListWidget()
        widget.setSpacing(10)
        widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        return widget

    def _panel_shell(self, title: str, body_widget: QWidget) -> QFrame:
        shell = QFrame(objectName="card")
        layout = QVBoxLayout(shell)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        eyebrow = QLabel(title, objectName="eyebrow")
        layout.addWidget(eyebrow)
        layout.addWidget(body_widget, 1)
        return shell

    def _on_draft_changed(self) -> None:
        if not self._draft_binding_suspended:
            self._draft_dirty = True

    def _selected_role_snapshot(self) -> dict[str, Any] | None:
        if self.snapshot is None:
            return None
        for role in self.snapshot.get("roles", []):
            if str(role.get("role_id", "")) == self.selected_role_id:
                return role
        return None

    def _on_role_selected(self) -> None:
        item = self.role_list.currentItem()
        if item is None:
            return
        role_id = str(item.data(Qt.UserRole) or "")
        if role_id and role_id != self.selected_role_id:
            self.selected_role_id = role_id
            self._draft_dirty = False
            self._loaded_role_id = ""
            self._render_selected_role(force=True)

    def _refresh(self, force: bool = False) -> None:
        try:
            snapshot = collect_dashboard_snapshot(self.state_path)
        except Exception as exc:
            self.close_label.setText(f"面板刷新失败：{type(exc).__name__}: {exc}")
            return

        self.snapshot = snapshot
        state = snapshot.get("state", {})
        progress = snapshot.get("progress", {})
        roles = list(snapshot.get("roles", []))
        timeline = list(snapshot.get("events", []))
        if roles and self.selected_role_id not in {str(role.get('role_id', '')) for role in roles}:
            self.selected_role_id = str(roles[0].get("role_id", ""))
            self._draft_dirty = False
            self._loaded_role_id = ""

        window_title = self.override_title or str(state.get("window_title") or state.get("title") or DEFAULT_WINDOW_TITLE)
        self.setWindowTitle(window_title)
        self.title_label.setText(window_title)
        self.subtitle_label.setText(f"正在监听 {state.get('docs_root', '')}")
        self.activity_label.setText(_conversation_status_text(roles))
        self.summary_label.setText(f"已完成 {progress.get('percent', 0)}%")
        self.detail_label.setText(
            f"已完成 {progress.get('completed', 0)}/{progress.get('total', 0)} | 来源：{progress.get('source', 'n/a')}"
        )
        self.progress_bar.setValue(int(progress.get("percent", 0)))
        self._render_auto_close(snapshot.get("auto_close", {}))

        roles_signature = tuple(
            (
                str(role.get("role_id", "")),
                str(role.get("status", "")),
                str(role.get("latest_message", "")),
                str(role.get("last_event_at", "")),
            )
            for role in roles
        )
        if force or self._last_signatures.get("roles") != roles_signature:
            self._render_role_cards(roles)
            self._last_signatures["roles"] = roles_signature

        timeline_signature = tuple(str(event.get("id", "")) for event in timeline)
        if force or self._last_signatures.get("timeline") != timeline_signature:
            self._render_timeline(timeline)
            self._last_signatures["timeline"] = timeline_signature

        docs_signature = (
            _text_signature(snapshot.get("shared_docs", {}).get("plan_markdown", "")),
            _text_signature(snapshot.get("shared_docs", {}).get("interfaces_markdown", "")),
            _text_signature(snapshot.get("shared_docs", {}).get("review_log_markdown", "")),
        )
        if force or self._last_signatures.get("docs") != docs_signature:
            self._render_shared_docs(snapshot)
            self._last_signatures["docs"] = docs_signature

        self._render_selected_role(force=force)

        interval = self.poll_interval_override or int(state.get("poll_interval_ms", DEFAULT_POLL_INTERVAL_MS))
        if self.timer.interval() != interval:
            self.timer.start(interval)

        auto_close = snapshot.get("auto_close", {})
        if auto_close.get("should_close") and auto_close.get("deadline_passed"):
            self.close()

    def _render_role_cards(self, roles: list[dict[str, Any]]) -> None:
        desired_ids = [str(role.get("role_id", "")) for role in roles]
        current_ids = [str(self.role_list.item(index).data(Qt.UserRole) or "") for index in range(self.role_list.count())]
        if current_ids != desired_ids:
            self.role_list.clear()
            self._role_card_signatures.clear()
            for role in roles:
                self._append_role_card(role)
        else:
            for index, role in enumerate(roles):
                role_id = str(role.get("role_id", ""))
                signature = _role_card_signature(role)
                if self._role_card_signatures.get(role_id) == signature:
                    continue
                item = self.role_list.item(index)
                if item is None:
                    continue
                card = self._role_card_widget(role)
                item.setSizeHint(card.sizeHint())
                self.role_list.setItemWidget(item, card)
                self._role_card_signatures[role_id] = signature

        for index in range(self.role_list.count()):
            item = self.role_list.item(index)
            if item is not None and str(item.data(Qt.UserRole) or "") == self.selected_role_id:
                self.role_list.setCurrentItem(item)
                break

    def _render_timeline(self, events: list[dict[str, Any]]) -> None:
        visible_events = list(events[-160:])
        desired_ids = [str(event.get("id", "")) for event in visible_events]
        current_ids = [str(self.timeline_list.item(index).data(Qt.UserRole) or "") for index in range(self.timeline_list.count())]

        if current_ids == desired_ids:
            self._refresh_timeline_cards_in_place(visible_events)
            return

        self.timeline_list.clear()
        self._timeline_card_signatures.clear()
        for event in visible_events:
            self._append_timeline_event(event)
        self.timeline_list.scrollToBottom()

    def _append_role_card(self, role: dict[str, Any]) -> None:
        role_id = str(role.get("role_id", ""))
        item = QListWidgetItem()
        item.setData(Qt.UserRole, role_id)
        card = self._role_card_widget(role)
        item.setSizeHint(card.sizeHint())
        self.role_list.addItem(item)
        self.role_list.setItemWidget(item, card)
        self._role_card_signatures[role_id] = _role_card_signature(role)

    def _append_timeline_event(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("id", ""))
        item = QListWidgetItem()
        item.setData(Qt.UserRole, event_id)
        card = self._event_card_widget(event)
        item.setSizeHint(card.sizeHint())
        self.timeline_list.addItem(item)
        self.timeline_list.setItemWidget(item, card)
        self._timeline_card_signatures[event_id] = _event_card_signature(event)

    def _prepend_timeline_event(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("id", ""))
        item = QListWidgetItem()
        item.setData(Qt.UserRole, event_id)
        card = self._event_card_widget(event)
        item.setSizeHint(card.sizeHint())
        self.timeline_list.insertItem(0, item)
        self.timeline_list.setItemWidget(item, card)
        self._timeline_card_signatures[event_id] = _event_card_signature(event)

    def _refresh_timeline_cards_in_place(self, events: list[dict[str, Any]]) -> None:
        for index, event in enumerate(events):
            event_id = str(event.get("id", ""))
            signature = _event_card_signature(event)
            if self._timeline_card_signatures.get(event_id) == signature:
                continue
            item = self.timeline_list.item(index)
            if item is None:
                continue
            card = self._event_card_widget(event)
            item.setSizeHint(card.sizeHint())
            self.timeline_list.setItemWidget(item, card)
            self._timeline_card_signatures[event_id] = signature

    def _render_shared_docs(self, snapshot: dict[str, Any]) -> None:
        docs = snapshot.get("shared_docs", {})
        _set_text_view_content(self.plan_view, _render_document_text("计划", docs.get("plan_markdown") or "暂无计划内容。"))
        _set_text_view_content(self.interfaces_view, _render_document_text("接口", docs.get("interfaces_markdown") or "暂无接口内容。"))
        _set_text_view_content(self.review_view, _render_document_text("审阅", docs.get("review_log_markdown") or "暂无审阅内容。"))

    def _render_selected_role(self, *, force: bool = False) -> None:
        role = self._selected_role_snapshot()
        if role is None:
            self.role_title_label.setText("未选择 Agent")
            self.role_meta_label.setText("")
            self.role_message_label.setText("")
            self.role_status_label.setText("待处理")
            self._render_role_activity_cards([])
            _set_text_view_content(self.committed_view, "Agent 输出\n==========\n\n当前还没有可显示的 Agent。\n")
            return

        role_signature = (
            str(role.get("role_id", "")),
            str(role.get("status", "")),
            _text_signature(role.get("latest_message", "")),
            str(role.get("last_event_at", "")),
            bool(((role.get("inactivity") or {}) if isinstance(role.get("inactivity"), dict) else {}).get("suspected_disconnect")),
            int((((role.get("inactivity") or {}) if isinstance(role.get("inactivity"), dict) else {}).get("idle_ms") or 0) // 60000),
            len(role.get("activity", []) if isinstance(role.get("activity"), list) else []),
            str((role.get("activity") or [{}])[-1].get("id", "")) if isinstance(role.get("activity"), list) and role.get("activity") else "",
            _text_signature(_role_document(role, "handover").get("draft_markdown", "") if _role_document(role, "handover") else ""),
            _text_signature(role.get("handover_markdown", "")),
        )
        if not force and self._last_signatures.get("selected_role") == role_signature:
            return
        self._last_signatures["selected_role"] = role_signature

        status = str(role.get("status", "pending"))
        inactivity = role.get("inactivity") if isinstance(role.get("inactivity"), dict) else {}
        self.role_title_label.setText(str(role.get("title", "")))
        self.role_meta_label.setText(
            f"{role.get('output_prefix', '')} | {role.get('persona_hint', '') or '未设置角色提示'} | 最近事件：{_friendly_timestamp(str(role.get('last_event_at', 'n/a')))}"
        )
        message_text = _compact_inline_text(str(role.get("latest_message", "暂无更新。")), limit=320)
        inactivity_hint = _role_inactivity_warning(inactivity)
        if inactivity_hint:
            message_text = f"{message_text}\n\n{inactivity_hint}"
        self.role_message_label.setText(message_text)
        status_key = "suspected_disconnect" if bool(inactivity.get("suspected_disconnect")) else status
        self.role_status_label.setText(_status_text(status_key))
        self.role_status_label.setStyleSheet(
            f"padding: 6px 10px; border-radius: 999px; font-weight: 700; background: {_status_background(status_key)}; color: {_THEME['text']};"
        )
        self._render_role_activity_cards(list(role.get("activity", []) if isinstance(role.get("activity"), list) else []))
        _set_text_view_content(self.committed_view, _render_document_text("Agent 输出", role.get("handover_markdown") or "当前还没有已提交内容。"))

        handover_document = _role_document(role, "handover")
        if force or not self._draft_dirty or self._loaded_role_id != self.selected_role_id:
            self._draft_binding_suspended = True
            self.draft_editor.setPlainText(str((handover_document or {}).get("draft_markdown", "")))
            self._draft_binding_suspended = False
            self._draft_dirty = False
            self._loaded_role_id = self.selected_role_id

    def _render_auto_close(self, auto_close: dict[str, Any]) -> None:
        if not auto_close.get("should_close"):
            self.close_label.setText("当所有可见 Agent 进入终态后，面板将自动关闭。")
            return
        ready_at = _parse_ready_text(str(auto_close.get("ready_at", "")))
        if ready_at is None:
            self.close_label.setText("自动关闭等待中。")
            return
        remaining_ms = max(int((ready_at - datetime.now(tz=UTC)).total_seconds() * 1000), 0)
        if remaining_ms <= 0:
            self.close_label.setText("本轮已完成，正在关闭面板……")
        else:
            self.close_label.setText(
                f"本轮已完成，{remaining_ms / 1000:.1f} 秒后自动关闭 | 原因：{auto_close.get('reason', 'complete')}"
            )

    def _role_card_widget(self, role: dict[str, Any]) -> QWidget:
        card = QFrame()
        card.setStyleSheet(
            f"background: {_THEME['surface_alt']}; border: 1px solid {_THEME['border']}; border-radius: 18px;"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        top = QHBoxLayout()
        title = QLabel(str(role.get("title", "")))
        title.setStyleSheet("font-size: 15px; font-weight: 700;")
        inactivity = role.get("inactivity") if isinstance(role.get("inactivity"), dict) else {}
        status_key = "suspected_disconnect" if bool(inactivity.get("suspected_disconnect")) else str(role.get("status", "pending"))
        pill = QLabel(_status_text(status_key))
        pill.setStyleSheet(
            f"padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; background: {_status_background(status_key)};"
        )
        top.addWidget(title, 1)
        top.addWidget(pill, 0, Qt.AlignRight)
        layout.addLayout(top)
        meta = QLabel(f"{role.get('output_prefix', '')} | {role.get('persona_hint', '')}")
        meta.setObjectName("subtle")
        meta.setWordWrap(True)
        latest = QLabel(_compact_inline_text(str(role.get("latest_message", "等待该 Agent 首次输出。")), limit=180))
        latest.setWordWrap(True)
        latest.setStyleSheet(f"color: {_THEME['text']}; line-height: 1.35;")
        stamp = QLabel(f"最近事件：{_friendly_timestamp(str(role.get('last_event_at') or role.get('last_updated_at') or ''))}")
        stamp.setStyleSheet(f"color: {_THEME['muted']}; font-size: 11px;")
        layout.addWidget(meta)
        layout.addWidget(latest)
        inactivity_hint = _role_inactivity_warning(inactivity)
        if inactivity_hint:
            warning = QLabel(inactivity_hint)
            warning.setWordWrap(True)
            warning.setStyleSheet(f"color: {_THEME['warning']}; font-size: 11px; font-weight: 700;")
            layout.addWidget(warning)
        layout.addWidget(stamp)
        return card

    def _event_card_widget(self, event: dict[str, Any]) -> QWidget:
        card = QFrame(objectName="threadShell")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        top = QHBoxLayout()
        event_type = str(event.get("type", "message"))
        role_id = str(event.get("role_id", "system")) or "system"
        avatar = QLabel(_avatar_text(role_id))
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(30, 30)
        avatar.setStyleSheet(
            f"background: {_THEME['accent_soft']}; color: {_THEME['text']}; border: 1px solid {_THEME['border']}; border-radius: 15px; font-weight: 700;"
        )
        pill = QLabel(_EVENT_LABELS.get(event_type, event_type.upper()))
        pill.setStyleSheet(
            f"padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; background: {_status_background(str(event.get('status', 'pending')))};"
        )
        who = QLabel(role_id)
        who.setStyleSheet("font-weight: 700; font-size: 13px;")
        timestamp = QLabel(_friendly_timestamp(str(event.get("created_at") or event.get("ts") or "")))
        timestamp.setStyleSheet(f"color: {_THEME['muted']}; font-size: 11px;")
        meta_col = QVBoxLayout()
        meta_col.setSpacing(2)
        meta_col.addWidget(who)
        meta_col.addWidget(timestamp)
        top.addWidget(avatar, 0, Qt.AlignTop)
        top.addLayout(meta_col, 1)
        top.addStretch(1)
        top.addWidget(pill)
        bubble = QFrame(objectName="messageBubble")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(14, 12, 14, 12)
        bubble_layout.setSpacing(6)
        status = QLabel(_status_text(str(event.get("status", ""))))
        status.setStyleSheet(f"color: {_THEME['muted']}; font-size: 11px;")
        message = QLabel(_compact_inline_text(str(event.get("message") or event.get("text") or ""), limit=420))
        message.setWordWrap(True)
        message.setStyleSheet(f"color: {_THEME['text']}; font-size: 13px; line-height: 1.45;")
        layout.addLayout(top)
        bubble_layout.addWidget(status)
        bubble_layout.addWidget(message)
        layout.addWidget(bubble)
        return card

    def _save_draft(self) -> None:
        role = self._selected_role_snapshot()
        if role is None:
            return
        write_role_document_draft(
            self.state_path,
            str(role.get("role_id", "")),
            "handover",
            self.draft_editor.toPlainText(),
            message="通过面板更新草稿",
        )
        self._draft_dirty = False
        self._refresh(force=True)

    def _stream_snippet(self) -> None:
        role = self._selected_role_snapshot()
        snippet = self.stream_input.text().strip()
        if role is None or not snippet:
            return
        append_role_stream_chunk(
            self.state_path,
            str(role.get("role_id", "")),
            snippet,
            document_key="handover",
            message="通过面板追加流式内容",
        )
        self.stream_input.clear()
        self._draft_dirty = False
        self._refresh(force=True)

    def _commit_draft(self) -> None:
        role = self._selected_role_snapshot()
        if role is None:
            return
        if self._draft_dirty:
            self._save_draft()
        commit_role_document_draft(
            self.state_path,
            str(role.get("role_id", "")),
            "handover",
            status="completed",
            message="通过面板提交草稿",
        )
        self._refresh(force=True)

    def _mark_offline(self) -> None:
        role = self._selected_role_snapshot()
        if role is None:
            return
        append_dashboard_event(
            self.state_path,
            {
                "type": "agent_closed",
                "role_id": str(role.get("role_id", "")),
                "status": "offline",
                "message": "已标记为离线",
            },
        )
        self._refresh(force=True)

    def _reawaken(self) -> None:
        role = self._selected_role_snapshot()
        if role is None:
            return
        append_dashboard_event(
            self.state_path,
            {
                "type": "agent_reawakened",
                "role_id": str(role.get("role_id", "")),
                "status": "reawakened",
                "message": "已重新唤醒",
            },
        )
        self._refresh(force=True)

    def _finish_run(self) -> None:
        mark_run_completed(self.state_path, message="通过面板结束本轮")
        self._refresh(force=True)

    def _render_role_activity_cards(self, activity: list[dict[str, Any]]) -> None:
        view = self.activity_view
        current_ids = [str(view.item(index).data(Qt.UserRole) or "") for index in range(view.count())]
        desired_events = list(activity[-60:])
        if not desired_events:
            desired_events = [
                {
                    "id": "placeholder",
                    "type": "status",
                    "status": "pending",
                    "role_id": "system",
                    "message": "当前还没有可显示的输入、输出或事件。",
                    "created_at": "",
                }
            ]
        desired_ids = [str(event.get("id", "")) for event in desired_events]
        if current_ids != desired_ids:
            view.clear()
            for event in desired_events:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, str(event.get("id", "")))
                card = self._activity_entry_widget(event)
                item.setSizeHint(card.sizeHint())
                view.addItem(item)
                view.setItemWidget(item, card)
        else:
            for index, event in enumerate(desired_events):
                item = view.item(index)
                if item is None:
                    continue
                card = self._activity_entry_widget(event)
                item.setSizeHint(card.sizeHint())
                view.setItemWidget(item, card)
        view.scrollToBottom()

    def _activity_entry_widget(self, event: dict[str, Any]) -> QWidget:
        card = QFrame(objectName="threadShell")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        kind = _activity_kind(event)
        top = QHBoxLayout()
        badge = QLabel(_activity_badge_text(kind))
        badge.setStyleSheet(
            f"padding: 4px 8px; border-radius: 999px; font-size: 11px; font-weight: 700; background: {_activity_badge_color(kind)};"
        )
        detail = QLabel(
            f"{_EVENT_LABELS.get(str(event.get('type', '')).strip(), str(event.get('type', '')).strip() or '消息')} · "
            f"{_friendly_timestamp(str(event.get('created_at') or event.get('ts') or ''))}"
        )
        detail.setStyleSheet(f"color: {_THEME['muted']}; font-size: 11px;")
        top.addWidget(badge, 0, Qt.AlignLeft)
        top.addWidget(detail, 1, Qt.AlignLeft)
        top.addStretch(1)

        bubble = QFrame(objectName="messageBubble")
        bubble_layout = QVBoxLayout(bubble)
        bubble_layout.setContentsMargins(12, 10, 12, 10)
        bubble_layout.setSpacing(6)
        title = QLabel(_activity_title(event, kind))
        title.setStyleSheet("font-weight: 700; font-size: 12px;")
        content = QLabel(_compact_inline_text(str(event.get("message") or event.get("text") or ""), limit=600))
        content.setWordWrap(True)
        content.setStyleSheet(f"color: {_THEME['text']}; line-height: 1.45;")
        bubble_layout.addWidget(title)
        bubble_layout.addWidget(content)

        layout.addLayout(top)
        layout.addWidget(bubble)
        return card

    def closeEvent(self, event: Any) -> None:
        self.timer.stop()
        super().closeEvent(event)



def _status_background(status: str) -> str:
    return _STATUS_COLORS.get(str(status).strip().lower(), _THEME["border"])



def _status_text(status: str) -> str:
    labels = {
        "pending": "待处理",
        "streaming": "流式中",
        "editing": "编辑中",
        "reawakened": "已唤醒",
        "active": "进行中",
        "in_progress": "进行中",
        "completed": "已完成",
        "approved": "已通过",
        "done": "已完成",
        "offline": "离线",
        "blocked": "阻塞",
        "failed": "失败",
        "error": "错误",
        "committed": "已提交",
        "suspected_disconnect": "疑似断连",
    }
    return labels.get(str(status).strip().lower(), str(status).strip() or "待处理")

def _friendly_timestamp(value: str) -> str:
    parsed = _parse_ready_text(value)
    if parsed is None:
        return value or ""
    utc_value = parsed.astimezone(UTC)
    if utc_value.microsecond:
        return utc_value.strftime("%Y-%m-%d %H:%M:%S.") + f"{utc_value.microsecond // 1000:03d} UTC"
    return utc_value.strftime("%Y-%m-%d %H:%M:%S UTC")



def _parse_ready_text(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _build_role_activity_text(role: dict[str, Any]) -> str:
    return _render_role_activity_as_text(role)


def _conversation_status_text(roles: list[dict[str, Any]]) -> str:
    suspected_disconnect_roles = [
        str(role.get("title") or role.get("role_id") or "Agent")
        for role in roles
        if isinstance(role.get("inactivity"), dict) and bool((role.get("inactivity") or {}).get("suspected_disconnect"))
    ]
    if suspected_disconnect_roles:
        if len(suspected_disconnect_roles) == 1:
            return f"疑似断连 · {suspected_disconnect_roles[0]} 长时间无响应"
        return f"疑似断连 · {len(suspected_disconnect_roles)} 个 Agent 长时间无响应"
    active_roles = [str(role.get("title") or role.get("role_id") or "Agent") for role in roles if str(role.get("status") or "").strip() in {"active", "in_progress", "running", "working", "streaming", "editing", "reawakened"}]
    if active_roles:
        if len(active_roles) == 1:
            return f"正在生成响应 · {active_roles[0]}"
        return f"正在生成响应 · {len(active_roles)} 个 Agent 活跃中"
    completed_roles = [role for role in roles if str(role.get("status") or "").strip() in {"completed", "approved", "done", "committed"}]
    if completed_roles:
        return f"本轮已沉淀 {len(completed_roles)} 条已完成输出"
    return "等待新事件..."


def _role_inactivity_warning(inactivity: dict[str, Any] | None) -> str:
    if not isinstance(inactivity, dict) or not bool(inactivity.get("suspected_disconnect")):
        return ""
    last_signal_at = _friendly_timestamp(str(inactivity.get("last_signal_at") or ""))
    idle_ms = max(int(inactivity.get("idle_ms") or 0), 0)
    if idle_ms >= 3600000:
        idle_text = f"{idle_ms // 3600000} 小时"
    elif idle_ms >= 60000:
        idle_text = f"{idle_ms // 60000} 分钟"
    else:
        idle_text = f"{max(idle_ms // 1000, 1)} 秒"
    if last_signal_at:
        return f"疑似断连：已 {idle_text} 没有输出或状态变化，最后信号 {last_signal_at}"
    return f"疑似断连：已 {idle_text} 没有输出或状态变化"


def _avatar_text(role_id: str) -> str:
    tokens = [token for token in str(role_id or "agent").replace("[", "").replace("]", "").replace(":", " ").replace("_", " ").split() if token]
    if not tokens:
        return "A"
    if len(tokens) == 1:
        return tokens[0][:2].upper()
    return (tokens[0][:1] + tokens[-1][:1]).upper()


def _activity_kind(event: dict[str, Any]) -> str:
    event_type = str(event.get("type", "")).strip()
    if event_type == "draft_replaced":
        return "input"
    if event_type == "stream_chunk":
        return "output"
    if event_type == "status" and str(event.get("role_id", "")).strip() == "system":
        return "event"
    return "event"


def _activity_badge_text(kind: str) -> str:
    return {
        "input": "输入",
        "output": "输出",
        "event": "事件",
    }.get(kind, "事件")


def _activity_badge_color(kind: str) -> str:
    return {
        "input": _THEME["warning"],
        "output": _THEME["accent_secondary"],
        "event": _THEME["accent_soft"],
    }.get(kind, _THEME["border"])


def _activity_title(event: dict[str, Any], kind: str) -> str:
    if kind == "input":
        return "向 Agent 输入了新内容"
    if kind == "output":
        return "Agent 产出了新的流式内容"
    return _status_text(str(event.get("status", ""))) or "事件更新"



def run_dashboard_qt(
    state_path: Path,
    *,
    title: str | None = None,
    topmost: bool = True,
    bring_to_front: bool = True,
    poll_interval_ms: int | None = None,
) -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("多 Agent 协作面板")
    font = QFont("Segoe UI")
    font.setPointSize(10)
    app.setFont(font)
    window = AgentTeamDashboardQtWindow(
        state_path,
        title=title,
        topmost=topmost,
        bring_to_front=bring_to_front,
        poll_interval_ms=poll_interval_ms,
    )
    window.show()
    return int(app.exec())


def _render_document_text(title: str, markdown_text: str) -> str:
    return _render_markdown_as_text(markdown_text, fallback_title=title)


def _set_text_view_content(widget: QPlainTextEdit, text: str) -> None:
    rendered = str(text or "")
    if widget.toPlainText() == rendered:
        return
    vertical_bar = widget.verticalScrollBar()
    was_near_bottom = vertical_bar.value() >= max(vertical_bar.maximum() - 8, 0)
    widget.setPlainText(rendered)
    if was_near_bottom:
        vertical_bar.setValue(vertical_bar.maximum())
    else:
        vertical_bar.setValue(min(vertical_bar.value(), vertical_bar.maximum()))


def _text_signature(value: Any) -> tuple[int, str, str]:
    text = str(value or "")
    if len(text) <= 512:
        return (len(text), text, "")
    return (len(text), text[:256], text[-256:])


def _role_card_signature(role: dict[str, Any]) -> tuple[str, str, str, str]:
    inactivity = role.get("inactivity") if isinstance(role.get("inactivity"), dict) else {}
    return (
        "suspected_disconnect" if bool(inactivity.get("suspected_disconnect")) else str(role.get("status", "")),
        _compact_inline_text(str(role.get("latest_message", "")), limit=180),
        str(role.get("output_prefix", "")),
        f"{role.get('persona_hint', '')}|{int(inactivity.get('idle_ms', 0) or 0) // 60000}",
    )


def _event_card_signature(event: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(event.get("type", "")),
        str(event.get("status", "")),
        _compact_inline_text(str(event.get("message") or event.get("text") or ""), limit=220),
        str(event.get("created_at") or event.get("ts") or ""),
    )


def _detect_timeline_prepend_count(current_ids: list[str], desired_ids: list[str]) -> int:
    if not desired_ids:
        return 0
    max_prefix = len(desired_ids)
    for prefix in range(1, max_prefix + 1):
        overlap = len(desired_ids) - prefix
        if overlap < 0:
            continue
        if desired_ids[prefix:] == current_ids[:overlap]:
            return prefix
    return 0


def _role_document(role: dict[str, Any], key: str) -> dict[str, Any] | None:
    for document in role.get("documents", []):
        if isinstance(document, dict) and str(document.get("key", "")).strip() == key:
            return document
    return None


def run_dashboard_application(
    state_path: Path,
    *,
    title: str | None = None,
    topmost: bool = True,
    bring_to_front: bool = True,
    poll_interval_ms: int | None = None,
) -> int:
    return run_dashboard_qt(
        state_path,
        title=title,
        topmost=topmost,
        bring_to_front=bring_to_front,
        poll_interval_ms=poll_interval_ms,
    )
