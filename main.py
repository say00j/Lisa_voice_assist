from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Optional

import speech_recognition as sr
from PySide6.QtCore import QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


APP_TITLE = "LISA \u2014 Voice App Launcher"
APPS_FILE_NAME = "apps.json"
APP_DATA_DIR_NAME = "LISA"
AUTO_START_LISTENING = False
MIC_CALIBRATION_SECONDS = 1.0
LISTEN_TIMEOUT_SECONDS = 1.0
PHRASE_TIME_LIMIT_SECONDS = 5.0
STOP_WAIT_TIMEOUT_MS = 7000
SUPPORTED_EXTENSIONS = {".exe", ".bat", ".cmd", ".com"}


def get_app_data_dir() -> Path:
    local_appdata = os.getenv("LOCALAPPDATA")
    base_dir = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    app_data_dir = base_dir / APP_DATA_DIR_NAME
    app_data_dir.mkdir(parents=True, exist_ok=True)
    return app_data_dir


APP_DATA_DIR = get_app_data_dir()
APPS_FILE = APP_DATA_DIR / APPS_FILE_NAME


def current_timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def normalize_speech(value: str) -> str:
    cleaned = re.sub(r"[^\w\s.\-]", " ", value)
    return re.sub(r"\s+", " ", cleaned.strip()).casefold()


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _split_argument_text(argument_text: str) -> list[str]:
    try:
        parts = shlex.split(argument_text, posix=False)
    except ValueError as exc:
        raise ValueError(f"Invalid launch command: {exc}") from exc

    return [_strip_wrapping_quotes(part) for part in parts]


def _resolve_executable_candidate(candidate_text: str) -> Optional[Path]:
    raw_value = _strip_wrapping_quotes(candidate_text.strip())
    if not raw_value:
        return None

    executable_path = Path(os.path.expandvars(raw_value)).expanduser()
    try:
        resolved_executable = executable_path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None

    if not resolved_executable.is_file():
        return None

    return resolved_executable


def _validate_supported_executable(executable_path: Path) -> None:
    if executable_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"Select a valid executable file ({allowed}).")


def split_launch_command(command_text: str) -> list[str]:
    expanded_text = os.path.expandvars(command_text).strip()
    if not expanded_text:
        raise ValueError("Launch command cannot be empty.")

    parsed_parts = _split_argument_text(expanded_text)
    if parsed_parts:
        resolved_executable = _resolve_executable_candidate(parsed_parts[0])
        if resolved_executable is not None:
            _validate_supported_executable(resolved_executable)
            return [str(resolved_executable), *parsed_parts[1:]]

    boundary_positions = [match.start() for match in re.finditer(r"\s+", expanded_text)]
    boundary_positions.append(len(expanded_text))

    for boundary in boundary_positions:
        executable_candidate = expanded_text[:boundary].strip()
        resolved_executable = _resolve_executable_candidate(executable_candidate)
        if resolved_executable is None:
            continue

        _validate_supported_executable(resolved_executable)
        remainder_text = expanded_text[boundary:].strip()
        remainder_parts = _split_argument_text(remainder_text) if remainder_text else []
        return [str(resolved_executable), *remainder_parts]

    raise ValueError("Executable file was not found.")


def normalize_launch_command(command_text: str) -> tuple[str, Path]:
    parts = split_launch_command(command_text)
    resolved_executable = Path(parts[0])

    normalized_command = subprocess.list2cmdline([str(resolved_executable), *parts[1:]])
    return normalized_command, resolved_executable


@dataclass(slots=True)
class AppEntry:
    name: str
    path: str


class AppRegistry:
    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.entries: list[AppEntry] = []

    def load(self) -> list[str]:
        self.entries = []
        messages = self._migrate_legacy_file()

        if not self.file_path.exists():
            return messages

        raw_text = self.file_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        if not isinstance(data, list):
            raise ValueError("apps.json must contain a list of applications.")

        seen_names: set[str] = set()
        for index, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                messages.append(f"Skipped invalid app entry at index {index}.")
                continue

            name = str(item.get("name", "")).strip()
            path = str(item.get("path", "")).strip()
            if not name or not path:
                messages.append(f"Skipped incomplete app entry at index {index}.")
                continue

            normalized = normalize_name(name)
            if normalized in seen_names:
                messages.append(f"Skipped duplicate app name '{name}' from apps.json.")
                continue

            seen_names.add(normalized)
            try:
                normalized_command, _ = normalize_launch_command(path)
            except ValueError as exc:
                self.entries.append(AppEntry(name=name, path=path))
                messages.append(f"Registered command for '{name}' is invalid: {exc}")
            else:
                self.entries.append(AppEntry(name=name, path=normalized_command))

        self._sort_entries()
        return messages

    def save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [{"name": entry.name, "path": entry.path} for entry in self.entries]
        self.file_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add_entry(self, entry: AppEntry) -> None:
        if self.find_by_name(entry.name) is not None:
            raise ValueError(f"An application named '{entry.name}' already exists.")

        self.entries.append(entry)
        self._sort_entries()
        self.save()

    def update_entry(self, original_name: str, updated_entry: AppEntry) -> None:
        original_normalized = normalize_name(original_name)
        updated_normalized = normalize_name(updated_entry.name)

        for existing in self.entries:
            if (
                normalize_name(existing.name) == updated_normalized
                and normalize_name(existing.name) != original_normalized
            ):
                raise ValueError(f"An application named '{updated_entry.name}' already exists.")

        for index, existing in enumerate(self.entries):
            if normalize_name(existing.name) == original_normalized:
                self.entries[index] = updated_entry
                self._sort_entries()
                self.save()
                return

        raise KeyError(f"Application '{original_name}' was not found.")

    def delete_entry(self, name: str) -> None:
        target = normalize_name(name)
        self.entries = [entry for entry in self.entries if normalize_name(entry.name) != target]
        self._sort_entries()
        self.save()

    def find_by_name(self, name: str) -> Optional[AppEntry]:
        target = normalize_name(name)
        for entry in self.entries:
            if normalize_name(entry.name) == target:
                return entry
        return None

    def find_command_target(self, speech_text: str) -> tuple[Optional[AppEntry], Optional[str]]:
        normalized_text = normalize_speech(speech_text)
        if "open" not in normalized_text:
            return None, None

        sorted_entries = sorted(self.entries, key=lambda entry: len(normalize_name(entry.name)), reverse=True)
        for entry in sorted_entries:
            candidate = normalize_name(entry.name)
            pattern = rf"(?:^|\b)open\s+{re.escape(candidate)}(?:\b|$)"
            if re.search(pattern, normalized_text):
                return entry, entry.name

        open_index = normalized_text.find("open")
        if open_index == -1:
            return None, None

        candidate_text = normalized_text[open_index + len("open") :].strip(" .,!?:;-")
        candidate_text = re.sub(
            r"\b(please|pls|now|thanks|thank you|for me|right now)\b$",
            "",
            candidate_text,
        ).strip(" .,!?:;-")

        if not candidate_text:
            return None, None

        direct_match = self.find_by_name(candidate_text)
        if direct_match is not None:
            return direct_match, direct_match.name

        for entry in sorted_entries:
            candidate = normalize_name(entry.name)
            if candidate_text.startswith(candidate):
                return entry, entry.name

        return None, candidate_text

    def _sort_entries(self) -> None:
        self.entries.sort(key=lambda entry: normalize_name(entry.name))

    def _legacy_file_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        seen_paths: set[Path] = set()

        base_candidates = [Path(sys.executable).resolve().parent / APPS_FILE_NAME]
        if "__file__" in globals():
            base_candidates.append(Path(__file__).resolve().parent / APPS_FILE_NAME)

        for candidate in base_candidates:
            if candidate == self.file_path or candidate in seen_paths:
                continue
            seen_paths.add(candidate)
            candidates.append(candidate)

        return candidates

    def _migrate_legacy_file(self) -> list[str]:
        messages: list[str] = []
        if self.file_path.exists():
            return messages

        for candidate in self._legacy_file_candidates():
            if not candidate.exists():
                continue

            try:
                self.file_path.parent.mkdir(parents=True, exist_ok=True)
                self.file_path.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception as exc:
                messages.append(f"Failed to migrate app list from '{candidate}': {exc}")
                continue

            messages.append(f"Migrated app list from '{candidate}'.")
            break

        return messages


class AppDialog(QDialog):
    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        title: str,
        existing_names: set[str],
        current_name: str = "",
        initial_name: str = "",
        initial_path: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(560, 160)

        self._existing_names = existing_names
        self._current_name = normalize_name(current_name) if current_name else ""
        self._result: Optional[AppEntry] = None

        self.name_edit = QLineEdit(initial_name)
        self.name_edit.setPlaceholderText("chrome")

        self.path_edit = QLineEdit(initial_path)
        self.path_edit.setPlaceholderText(
            r'"C:\Program Files\BraveSoftware\Brave-Browser\Application\chrome_proxy.exe" '
            r"--profile-directory=Default --app-id=agimnkijcaahngcdmfeangaknmldooml"
        )

        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse_for_executable)

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_edit)
        path_layout.addWidget(self.browse_button)

        path_widget = QWidget()
        path_widget.setLayout(path_layout)

        form_layout = QFormLayout()
        form_layout.addRow("Friendly Name", self.name_edit)
        form_layout.addRow("Launch Command", path_widget)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form_layout)
        layout.addWidget(button_box)

    def result_entry(self) -> AppEntry:
        if self._result is None:
            raise RuntimeError("Dialog result requested before acceptance.")
        return self._result

    @Slot()
    def _browse_for_executable(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Executable",
            str(Path.home()),
            "Executables (*.exe *.bat *.cmd *.com);;All Files (*)",
        )
        if not path:
            return

        self.path_edit.setText(path)
        if not self.name_edit.text().strip():
            self.name_edit.setText(Path(path).stem.lower())

    def accept(self) -> None:
        name = re.sub(r"\s+", " ", self.name_edit.text().strip())
        command_text = self.path_edit.text().strip()

        if not name:
            QMessageBox.warning(self, "Invalid Name", "Please enter a friendly application name.")
            return

        normalized = normalize_name(name)
        if normalized in self._existing_names and normalized != self._current_name:
            QMessageBox.warning(
                self,
                "Duplicate Name",
                f"An application named '{name}' is already registered.",
            )
            return

        try:
            normalized_command, _ = normalize_launch_command(command_text)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Launch Command", str(exc))
            return

        self._result = AppEntry(name=name, path=normalized_command)
        super().accept()


class SpeechWorker(QThread):
    log_event = Signal(str, str)
    recognized_text = Signal(str)
    status_change = Signal(str)
    mic_state_change = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        outcome = "stopped"
        recognizer = sr.Recognizer()
        recognizer.dynamic_energy_threshold = True
        recognizer.pause_threshold = 0.8
        recognizer.non_speaking_duration = 0.3

        try:
            self.mic_state_change.emit("initializing")
            with sr.Microphone() as source:
                try:
                    recognizer.adjust_for_ambient_noise(source, duration=MIC_CALIBRATION_SECONDS)
                    self.log_event.emit("INFO", "Microphone calibrated")
                except Exception as exc:
                    self.log_event.emit("WARNING", f"Microphone calibration issue: {exc}")

                self.status_change.emit("started")
                self.mic_state_change.emit("waiting")

                while not self._stop_event.is_set():
                    try:
                        self.mic_state_change.emit("active")
                        audio = recognizer.listen(
                            source,
                            timeout=LISTEN_TIMEOUT_SECONDS,
                            phrase_time_limit=PHRASE_TIME_LIMIT_SECONDS,
                        )

                        if self._stop_event.is_set():
                            break

                        self.mic_state_change.emit("processing")
                        recognized = recognizer.recognize_google(audio).strip()
                        if recognized:
                            self.log_event.emit("COMMAND", recognized)
                            self.recognized_text.emit(recognized)
                    except sr.WaitTimeoutError:
                        pass
                    except sr.UnknownValueError:
                        self.log_event.emit("WARNING", "Speech could not be understood")
                    except sr.RequestError as exc:
                        self.log_event.emit("ERROR", f"Speech recognition service error: {exc}")
                        time.sleep(1.0)
                    except OSError as exc:
                        outcome = "error"
                        self.log_event.emit("ERROR", f"Microphone error: {exc}")
                        break
                    except Exception as exc:
                        self.log_event.emit("ERROR", f"Unexpected recognition error: {exc}")
                    finally:
                        if not self._stop_event.is_set():
                            self.mic_state_change.emit("waiting")
        except OSError as exc:
            outcome = "error"
            self.log_event.emit("ERROR", f"Microphone not available: {exc}")
        except Exception as exc:
            outcome = "error"
            self.log_event.emit("ERROR", f"Failed to initialize speech recognition: {exc}")
        finally:
            self.mic_state_change.emit("inactive")
            self.status_change.emit(outcome)


class LisaMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.registry = AppRegistry(APPS_FILE)
        self.speech_worker: Optional[SpeechWorker] = None
        self.is_listening = False
        self._is_closing = False
        self._stop_requested = False

        self.setWindowTitle(APP_TITLE)
        self.resize(1000, 680)
        self.setMinimumSize(860, 560)

        self._build_ui()
        self._apply_styles()
        self._load_registry()
        self._refresh_table()
        self._update_action_buttons()
        self._set_status("Idle")
        self._set_mic_state("inactive")

        self.append_log("INFO", "App started")
        self.append_log("INFO", f"Using app registry file: {self.registry.file_path}")
        self.append_log("INFO", f"Loaded {len(self.registry.entries)} registered application(s)")

        self.toggle_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.toggle_shortcut.activated.connect(self.toggle_listening)

        if AUTO_START_LISTENING:
            QTimer.singleShot(300, self.start_listening)

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(14)

        title_label = QLabel(APP_TITLE)
        title_label.setObjectName("TitleLabel")

        subtitle_label = QLabel("Voice-controlled launcher for your registered Windows applications.")
        subtitle_label.setObjectName("SubtitleLabel")

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        title_layout.addWidget(title_label)
        title_layout.addWidget(subtitle_label)

        self.start_button = QPushButton("Start Listening")
        self.start_button.clicked.connect(self.start_listening)

        self.stop_button = QPushButton("Stop Listening")
        self.stop_button.clicked.connect(self.stop_listening)

        self.clear_log_button = QPushButton("Clear Log")
        self.clear_log_button.clicked.connect(self.clear_log)

        self.exit_button = QPushButton("Exit")
        self.exit_button.clicked.connect(self.close)

        controls_layout = QHBoxLayout()
        controls_layout.setSpacing(10)
        controls_layout.addLayout(title_layout)
        controls_layout.addStretch(1)
        controls_layout.addWidget(self.start_button)
        controls_layout.addWidget(self.stop_button)
        controls_layout.addWidget(self.clear_log_button)
        controls_layout.addWidget(self.exit_button)

        self.status_badge = QLabel("Idle")
        self.status_badge.setAlignment(Qt.AlignCenter)
        self.status_badge.setFixedWidth(110)

        self.mic_badge = QLabel("Mic: Inactive")
        self.mic_badge.setAlignment(Qt.AlignCenter)
        self.mic_badge.setFixedWidth(140)

        status_caption = QLabel("Status")
        status_caption.setObjectName("StatusCaption")
        mic_caption = QLabel("Microphone")
        mic_caption.setObjectName("StatusCaption")

        status_layout = QHBoxLayout()
        status_layout.setSpacing(10)
        status_layout.addWidget(status_caption)
        status_layout.addWidget(self.status_badge)
        status_layout.addSpacing(18)
        status_layout.addWidget(mic_caption)
        status_layout.addWidget(self.mic_badge)
        status_layout.addStretch(1)

        self.app_table = QTableWidget(0, 2)
        self.app_table.setHorizontalHeaderLabels(["Friendly Name", "Launch Command"])
        self.app_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.app_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.app_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.app_table.setAlternatingRowColors(True)
        self.app_table.verticalHeader().setVisible(False)
        self.app_table.horizontalHeader().setStretchLastSection(True)
        self.app_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.app_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.app_table.itemSelectionChanged.connect(self._update_action_buttons)

        self.add_button = QPushButton("Add App")
        self.add_button.clicked.connect(self.add_app)

        self.edit_button = QPushButton("Edit App")
        self.edit_button.clicked.connect(self.edit_selected_app)

        self.delete_button = QPushButton("Delete App")
        self.delete_button.clicked.connect(self.delete_selected_app)

        self.browse_button = QPushButton("Browse for Executable")
        self.browse_button.clicked.connect(self.browse_for_app)

        app_button_layout = QHBoxLayout()
        app_button_layout.setSpacing(10)
        app_button_layout.addWidget(self.add_button)
        app_button_layout.addWidget(self.edit_button)
        app_button_layout.addWidget(self.delete_button)
        app_button_layout.addWidget(self.browse_button)
        app_button_layout.addStretch(1)

        app_group = QGroupBox("Application Manager")
        app_group_layout = QVBoxLayout(app_group)
        app_group_layout.setSpacing(12)
        app_group_layout.addLayout(app_button_layout)
        app_group_layout.addWidget(self.app_table)

        self.log_panel = QPlainTextEdit()
        self.log_panel.setReadOnly(True)
        self.log_panel.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        log_group = QGroupBox("Activity Log")
        log_group_layout = QVBoxLayout(log_group)
        log_group_layout.addWidget(self.log_panel)

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(app_group)
        splitter.addWidget(log_group)
        splitter.setSizes([320, 280])

        main_layout.addLayout(controls_layout)
        main_layout.addLayout(status_layout)
        main_layout.addWidget(splitter)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background-color: #f4f7fb;
            }
            QLabel#TitleLabel {
                font-size: 24px;
                font-weight: 700;
                color: #1f2a37;
            }
            QLabel#SubtitleLabel {
                color: #52606d;
                font-size: 13px;
            }
            QLabel#StatusCaption {
                color: #52606d;
                font-size: 12px;
                font-weight: 600;
            }
            QGroupBox {
                background-color: #ffffff;
                border: 1px solid #d9e2ec;
                border-radius: 12px;
                margin-top: 10px;
                font-weight: 700;
                color: #1f2a37;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 14px;
                padding: 0 6px;
            }
            QPushButton {
                background-color: #0f6cbd;
                color: white;
                border: none;
                border-radius: 8px;
                padding: 10px 14px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #0c5da5;
            }
            QPushButton:disabled {
                background-color: #9fb3c8;
                color: #eef2f6;
            }
            QTableWidget, QPlainTextEdit, QLineEdit {
                background-color: #ffffff;
                border: 1px solid #cbd2d9;
                border-radius: 8px;
                color: #1f2a37;
                selection-background-color: #d6ebff;
                selection-color: #102a43;
            }
            QHeaderView::section {
                background-color: #eef4fb;
                color: #334e68;
                padding: 8px;
                border: none;
                border-bottom: 1px solid #d9e2ec;
                font-weight: 700;
            }
            """
        )

    def _load_registry(self) -> None:
        try:
            messages = self.registry.load()
        except FileNotFoundError:
            self.append_log("WARNING", "apps.json was not found. Starting with an empty app list.")
        except json.JSONDecodeError as exc:
            self.append_log("ERROR", f"Failed to parse apps.json: {exc}")
            QMessageBox.critical(self, "Invalid apps.json", f"Could not read apps.json:\n{exc}")
        except Exception as exc:
            self.append_log("ERROR", f"Failed to load app registry: {exc}")
            QMessageBox.critical(self, "Load Error", f"Could not load app registry:\n{exc}")
        else:
            for message in messages:
                self.append_log("WARNING", message)

    def append_log(self, level: str, message: str) -> None:
        line = f"[{current_timestamp()}] {level} \u2014 {message}"
        self.log_panel.appendPlainText(line)
        self.log_panel.moveCursor(QTextCursor.End)
        self.log_panel.ensureCursorVisible()

    def clear_log(self) -> None:
        self.log_panel.clear()
        self.append_log("INFO", "Log cleared")

    def _refresh_table(self, select_name: str = "") -> None:
        self.app_table.setRowCount(len(self.registry.entries))

        selected_row = -1
        target_name = normalize_name(select_name) if select_name else ""

        for row, entry in enumerate(self.registry.entries):
            name_item = QTableWidgetItem(entry.name)
            path_item = QTableWidgetItem(entry.path)
            name_item.setToolTip(entry.name)
            path_item.setToolTip(entry.path)
            self.app_table.setItem(row, 0, name_item)
            self.app_table.setItem(row, 1, path_item)

            if target_name and normalize_name(entry.name) == target_name:
                selected_row = row

        if selected_row >= 0:
            self.app_table.selectRow(selected_row)
        elif self.registry.entries:
            self.app_table.selectRow(0)
        else:
            self.app_table.clearSelection()

        self._update_action_buttons()

    def _selected_entry(self) -> Optional[AppEntry]:
        row = self.app_table.currentRow()
        if row < 0 or row >= len(self.registry.entries):
            return None
        return self.registry.entries[row]

    def _update_action_buttons(self) -> None:
        has_selection = self._selected_entry() is not None
        listening_thread_active = self.speech_worker is not None and self.speech_worker.isRunning()

        self.edit_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)
        self.start_button.setEnabled(not listening_thread_active)
        self.stop_button.setEnabled(listening_thread_active)

    def _existing_name_set(self) -> set[str]:
        return {normalize_name(entry.name) for entry in self.registry.entries}

    @Slot()
    def browse_for_app(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Executable",
            str(Path.home()),
            "Executables (*.exe *.bat *.cmd *.com);;All Files (*)",
        )
        if not path:
            return

        self.add_app(initial_path=path)

    @Slot()
    def add_app(self, checked: bool = False, initial_path: str = "") -> None:
        del checked
        inferred_name = Path(initial_path).stem.lower() if initial_path else ""
        dialog = AppDialog(
            self,
            title="Add Application",
            existing_names=self._existing_name_set(),
            initial_name=inferred_name,
            initial_path=initial_path,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        entry = dialog.result_entry()
        try:
            self.registry.add_entry(entry)
        except Exception as exc:
            self.append_log("ERROR", f"Failed to add app '{entry.name}': {exc}")
            QMessageBox.critical(self, "Save Error", f"Could not add app:\n{exc}")
            return

        self._refresh_table(select_name=entry.name)
        self.append_log("ACTION", f"Registered app '{entry.name}'")

    @Slot()
    def edit_selected_app(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.append_log("WARNING", "Edit requested without a selected app")
            return

        dialog = AppDialog(
            self,
            title="Edit Application",
            existing_names=self._existing_name_set(),
            current_name=entry.name,
            initial_name=entry.name,
            initial_path=entry.path,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        updated = dialog.result_entry()
        try:
            self.registry.update_entry(entry.name, updated)
        except Exception as exc:
            self.append_log("ERROR", f"Failed to update app '{entry.name}': {exc}")
            QMessageBox.critical(self, "Save Error", f"Could not update app:\n{exc}")
            return

        self._refresh_table(select_name=updated.name)
        self.append_log("ACTION", f"Updated app '{updated.name}'")

    @Slot()
    def delete_selected_app(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.append_log("WARNING", "Delete requested without a selected app")
            return

        response = QMessageBox.question(
            self,
            "Delete Application",
            f"Delete '{entry.name}' from the launcher list?",
        )
        if response != QMessageBox.Yes:
            return

        try:
            self.registry.delete_entry(entry.name)
        except Exception as exc:
            self.append_log("ERROR", f"Failed to delete app '{entry.name}': {exc}")
            QMessageBox.critical(self, "Delete Error", f"Could not delete app:\n{exc}")
            return

        self._refresh_table()
        self.append_log("ACTION", f"Deleted app '{entry.name}'")

    @Slot()
    def start_listening(self) -> None:
        if self.speech_worker is not None and self.speech_worker.isRunning():
            self.append_log("WARNING", "Listening is already active")
            return

        self._stop_requested = False
        self.speech_worker = SpeechWorker()
        self.speech_worker.log_event.connect(self.append_log)
        self.speech_worker.recognized_text.connect(self.handle_recognized_text)
        self.speech_worker.status_change.connect(self._handle_worker_status)
        self.speech_worker.mic_state_change.connect(self._set_mic_state)
        self.speech_worker.finished.connect(self._cleanup_worker)
        self.speech_worker.start()

        self.append_log("INFO", "Listening start requested")
        self._update_action_buttons()

    @Slot()
    def stop_listening(self) -> None:
        if self.speech_worker is None or not self.speech_worker.isRunning():
            self.append_log("WARNING", "Listening is not active")
            self._set_mic_state("inactive")
            self._update_action_buttons()
            return

        self._stop_requested = True
        self.is_listening = False
        self.append_log("INFO", "Stopping listening")
        self.speech_worker.stop()
        self._update_action_buttons()

    @Slot()
    def toggle_listening(self) -> None:
        if self.speech_worker is not None and self.speech_worker.isRunning():
            self.stop_listening()
        else:
            self.start_listening()

    @Slot(str)
    def handle_recognized_text(self, text: str) -> None:
        if not self.is_listening:
            self.append_log("WARNING", "Ignored recognized speech while listening was inactive")
            return

        app_entry, candidate_name = self.registry.find_command_target(text)
        if candidate_name is None:
            self.append_log("WARNING", f"Ignored unsupported command: {text}")
            return

        if app_entry is None:
            self.append_log("WARNING", f"App not registered: {candidate_name}")
            return

        self._launch_app(app_entry)

    def _launch_app(self, entry: AppEntry) -> None:
        try:
            command_parts = split_launch_command(entry.path)
            _, executable_path = normalize_launch_command(entry.path)
        except ValueError as exc:
            self.append_log("ERROR", f"Invalid launch command for '{entry.name}': {exc}")
            return

        self.append_log("ACTION", f"Launching {entry.name}")
        try:
            if executable_path.suffix.lower() in {".bat", ".cmd"}:
                process = subprocess.Popen(
                    ["cmd", "/c", command_parts[0], *command_parts[1:]],
                    cwd=str(executable_path.parent),
                )
            else:
                process = subprocess.Popen(command_parts, cwd=str(executable_path.parent))
        except FileNotFoundError:
            self.append_log("ERROR", f"Executable not found for '{entry.name}'")
        except PermissionError:
            self.append_log("ERROR", f"Permission denied while launching '{entry.name}'")
        except OSError as exc:
            self.append_log("ERROR", f"Failed to launch '{entry.name}': {exc}")
        else:
            self.append_log("INFO", f"Application launched successfully: {entry.name} (PID {process.pid})")

    @Slot(str)
    def _handle_worker_status(self, state: str) -> None:
        if state == "started":
            self._stop_requested = False
            self.is_listening = True
            self._set_status("Listening")
            self.append_log("INFO", "Listening started")
        elif state == "error":
            self._stop_requested = False
            self.is_listening = False
            self._set_status("Error")
            self._set_mic_state("inactive")
            self.append_log("ERROR", "Listening stopped due to an error")
        else:
            was_listening = self.is_listening
            self.is_listening = False
            self._set_status("Idle")
            self._set_mic_state("inactive")
            if was_listening or self._stop_requested or self._is_closing:
                self.append_log("INFO", "Listening stopped")
            self._stop_requested = False

        self._update_action_buttons()

    @Slot()
    def _cleanup_worker(self) -> None:
        if self.speech_worker is None:
            return

        if self.speech_worker.isRunning():
            return

        self.speech_worker.deleteLater()
        self.speech_worker = None
        self._update_action_buttons()

    @Slot(str)
    def _set_status(self, state: str) -> None:
        styles = {
            "Listening": ("#1f7a4f", "#e8f6ee", "#cdebd8"),
            "Idle": ("#a63d40", "#fdecec", "#f5c2c7"),
            "Error": ("#9a6700", "#fff6df", "#f3d693"),
        }
        text_color, background, border = styles.get(state, styles["Idle"])
        self.status_badge.setText(state)
        self.status_badge.setStyleSheet(
            f"""
            QLabel {{
                color: {text_color};
                background-color: {background};
                border: 1px solid {border};
                border-radius: 14px;
                padding: 6px 12px;
                font-weight: 700;
            }}
            """
        )

    @Slot(str)
    def _set_mic_state(self, state: str) -> None:
        label_map = {
            "initializing": ("Mic: Initializing", "#755f00", "#fff7d6", "#f0dd8a"),
            "active": ("Mic: Active", "#006e6d", "#def7f6", "#8ad9d6"),
            "processing": ("Mic: Processing", "#7a3e00", "#ffedd5", "#fdba74"),
            "waiting": ("Mic: Waiting", "#1d4ed8", "#dbeafe", "#93c5fd"),
            "inactive": ("Mic: Inactive", "#52606d", "#edf2f7", "#cbd5e0"),
        }
        text, text_color, background, border = label_map.get(state, label_map["inactive"])
        self.mic_badge.setText(text)
        self.mic_badge.setStyleSheet(
            f"""
            QLabel {{
                color: {text_color};
                background-color: {background};
                border: 1px solid {border};
                border-radius: 14px;
                padding: 6px 12px;
                font-weight: 700;
            }}
            """
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._is_closing = True
        self._stop_requested = True
        self.is_listening = False
        self.append_log("INFO", "Shutting down")

        if self.speech_worker is not None and self.speech_worker.isRunning():
            self.speech_worker.stop()
            self.speech_worker.wait(STOP_WAIT_TIMEOUT_MS)

        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = LisaMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
