"""Qt process controller for one supervised NeoCortex worker."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from ..protocol.messages import (
    MAX_MESSAGE_BYTES,
    MESSAGE_PREFIX,
    WorkerMessageValidator,
    WorkerProtocolError,
    command_record,
    decode_message,
    sanitize_text,
)
from .request import RunRequest


# region [01] Supervised worker lifecycle

MAX_PROCESS_LINE_BYTES = MAX_MESSAGE_BYTES + len(MESSAGE_PREFIX.encode("utf-8")) + 2


class WorkerController(QObject):
    """Own exactly one child process and never detach operational work."""

    message_received = Signal(dict)
    output_received = Signal(str)
    running_changed = Signal(bool)
    execution_finished = Signal(int, str)
    startup_failed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._process: QProcess | None = None
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._stdout_discarding_oversized_line = False
        self._stderr_discarding_oversized_line = False
        self._last_lifecycle = ""
        self._message_validator = WorkerMessageValidator()
        self._protocol_failure_reported = False

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.state() is not QProcess.ProcessState.NotRunning
        )

    @property
    def process_id(self) -> int:
        return 0 if self._process is None else int(self._process.processId())

    def start(self, request: RunRequest) -> None:
        if self.is_running:
            raise RuntimeError("Ya existe una ejecución supervisada por esta interfaz")
        validated = request.validated()
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONIOENCODING", "utf-8")
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("NEOCORTEX_UI_RUN_ID", validated.request_id)
        environment.insert("NEOCORTEX_UI_PROFILE", validated.profile)
        environment.insert("NEOCORTEX_UI_MAX_ITEMS", str(validated.max_items))
        environment.insert("NEOCORTEX_UI_DEADLINE_SECONDS", str(validated.deadline_seconds))
        process.setProcessEnvironment(environment)

        if getattr(sys, "frozen", False):
            program = sys.executable
            arguments = ["--gui-worker", *validated.cli_arguments()]
            working_directory = validated.root
        else:
            program = sys.executable
            arguments = [
                "-m",
                "neocortex.interface.protocol.worker",
                *validated.cli_arguments(),
            ]
            working_directory = Path(__file__).resolve().parents[3]

        process.setProgram(program)
        process.setArguments(arguments)
        process.setWorkingDirectory(os.fspath(working_directory))
        process.readyReadStandardOutput.connect(self._read_stdout)
        process.readyReadStandardError.connect(self._read_stderr)
        process.errorOccurred.connect(self._process_error)
        process.finished.connect(self._process_finished)
        self._stdout_buffer.clear()
        self._stderr_buffer.clear()
        self._stdout_discarding_oversized_line = False
        self._stderr_discarding_oversized_line = False
        self._last_lifecycle = ""
        self._message_validator.reset()
        self._protocol_failure_reported = False
        self._process = process
        process.start()
        if not process.waitForStarted(5_000):
            detail = process.errorString() or "No fue posible iniciar el worker"
            self._dispose_process()
            self.startup_failed.emit(detail)
            raise RuntimeError(detail)
        self.running_changed.emit(True)

    def request_cancellation(self) -> bool:
        if not self.is_running or self._process is None:
            return False
        written = self._process.write(command_record("cancel"))
        self._process.waitForBytesWritten(1_000)
        return written > 0

    def _read_stdout(self) -> None:
        if self._process is None:
            return
        self._ingest_output(
            self._stdout_buffer,
            self._process.readAllStandardOutput().data(),
            protocol=True,
        )

    def _read_stderr(self) -> None:
        if self._process is None:
            return
        self._ingest_output(
            self._stderr_buffer,
            self._process.readAllStandardError().data(),
            protocol=False,
        )

    def _ingest_output(
        self,
        buffer: bytearray,
        data: bytes | bytearray | memoryview,
        *,
        protocol: bool,
    ) -> None:
        """Consume complete lines while keeping an unterminated line strictly bounded."""

        payload = data if isinstance(data, bytes) else bytes(data)
        flag_name = self._discarding_flag(protocol)
        cursor = 0
        while cursor < len(payload):
            next_cursor = self._ingest_segment(
                buffer,
                payload,
                cursor,
                protocol=protocol,
                flag_name=flag_name,
            )
            if next_cursor is None:
                return
            cursor = next_cursor

    @staticmethod
    def _discarding_flag(protocol: bool) -> str:
        return (
            "_stdout_discarding_oversized_line" if protocol else "_stderr_discarding_oversized_line"
        )

    def _ingest_segment(
        self,
        buffer: bytearray,
        payload: bytes,
        cursor: int,
        *,
        protocol: bool,
        flag_name: str,
    ) -> int | None:
        if bool(getattr(self, flag_name)):
            return self._skip_discarded_line(payload, cursor, flag_name)
        newline = payload.find(b"\n", cursor)
        end = len(payload) if newline < 0 else newline + 1
        if len(buffer) + end - cursor > MAX_PROCESS_LINE_BYTES:
            return self._discard_oversized_line(
                buffer,
                end,
                newline,
                protocol=protocol,
                flag_name=flag_name,
            )
        buffer.extend(payload[cursor:end])
        if newline >= 0:
            self._consume_lines(buffer, protocol=protocol)
        return end

    def _skip_discarded_line(
        self,
        payload: bytes,
        cursor: int,
        flag_name: str,
    ) -> int | None:
        newline = payload.find(b"\n", cursor)
        if newline < 0:
            return None
        setattr(self, flag_name, False)
        return newline + 1

    def _discard_oversized_line(
        self,
        buffer: bytearray,
        end: int,
        newline: int,
        *,
        protocol: bool,
        flag_name: str,
    ) -> int | None:
        buffer.clear()
        stream_name = "stdout" if protocol else "stderr"
        self.output_received.emit(
            f"Línea de {stream_name} descartada por exceder el límite "
            f"de {MAX_PROCESS_LINE_BYTES} bytes"
        )
        if newline < 0:
            setattr(self, flag_name, True)
            return None
        return end

    def _consume_lines(self, buffer: bytearray, *, protocol: bool) -> None:
        raw = self._take_line(buffer)
        while raw is not None:
            self._consume_line(raw, protocol=protocol)
            raw = self._take_line(buffer)

    @staticmethod
    def _take_line(buffer: bytearray) -> bytes | None:
        newline = buffer.find(b"\n")
        if newline < 0:
            return None
        raw = bytes(buffer[: newline + 1])
        del buffer[: newline + 1]
        return raw

    def _consume_line(self, raw: bytes, *, protocol: bool) -> None:
        text = sanitize_text(raw.decode("utf-8", errors="replace"))
        if not text:
            return
        if protocol and self._consume_protocol_record(raw):
            return
        self.output_received.emit(text)

    def _consume_protocol_record(self, raw: bytes) -> bool:
        try:
            record = decode_message(raw)
            if record is None:
                return False
            record = dict(self._message_validator.accept(record))
        except (WorkerProtocolError, ValueError, TypeError) as exc:
            self._report_protocol_failure(str(exc))
            return True
        message_type = str(record.get("type", ""))
        if message_type != "progress":
            self._last_lifecycle = message_type
        self.message_received.emit(record)
        return True

    def _report_protocol_failure(self, detail: str) -> None:
        if self._protocol_failure_reported:
            return
        self._protocol_failure_reported = True
        safe_detail = sanitize_text(detail)
        self._last_lifecycle = "failed"
        self.output_received.emit(f"Registro de worker inválido: {safe_detail}")
        self.message_received.emit(
            self._message_validator.synthetic_failure(safe_detail, stage="transport")
        )

    def _process_error(self, _error: QProcess.ProcessError) -> None:
        if self._process is not None:
            self.output_received.emit(self._process.errorString())

    def _process_finished(
        self,
        exit_code: int,
        _exit_status: QProcess.ExitStatus,
    ) -> None:
        self._flush_remaining_output()
        if not self._message_validator.terminal and not self._protocol_failure_reported:
            self._report_protocol_failure("Worker finalizó sin registro terminal")
        lifecycle = self._last_lifecycle or "failed"
        self._dispose_process()
        self.running_changed.emit(False)
        self.execution_finished.emit(exit_code, lifecycle)

    def _flush_remaining_output(self) -> None:
        if self._process is not None:
            self._ingest_output(
                self._stdout_buffer,
                self._process.readAllStandardOutput().data(),
                protocol=True,
            )
            self._ingest_output(
                self._stderr_buffer,
                self._process.readAllStandardError().data(),
                protocol=False,
            )
        for buffer, protocol, flag_name in (
            (
                self._stdout_buffer,
                True,
                "_stdout_discarding_oversized_line",
            ),
            (
                self._stderr_buffer,
                False,
                "_stderr_discarding_oversized_line",
            ),
        ):
            if bool(getattr(self, flag_name)):
                buffer.clear()
                setattr(self, flag_name, False)
                continue
            if buffer:
                self._ingest_output(buffer, b"\n", protocol=protocol)

    def _dispose_process(self) -> None:
        if self._process is not None:
            self._process.deleteLater()
        self._process = None


# endregion [01]
